import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text, CommandTextError
from activity_manager.commands.with_form import WITH_FORM
from activity_manager.commands.add_field import AddFieldCommand
from activity_manager.schema_change import apply_schema_sequence
from activity_manager.listbox import DatabaseListbox
from test import test_multi_form as fixtures

form = fixtures.form


class FormIterationTests(unittest.IsolatedAsyncioTestCase):
    evaluate = fixtures.PreviewTests.evaluate

    async def test_initial_form_selection_keeps_all_database_suggestions(self):
        service = fixtures.MemoryService([form('a', 'A'), form('b', 'B')])
        service.get_databases = AsyncMock(return_value={'db': 'Database (db)'})
        service.get_forms = AsyncMock(return_value={'a': 'A (a)', 'b': 'B (b)'})
        service.get_form_tree = AsyncMock(return_value={'a': form('a', 'A')})
        creators = []
        def create(*args, **kwargs):
            creator = CommandCreatorRenderer(*args, **kwargs)
            creators.append(creator)
            return creator
        with patch('activity_manager.page.ActivityInfoService', return_value=service), \
                patch('activity_manager.page.CommandCreatorRenderer', side_effect=create):
            with Client(ui.page('/')), ui.column() as page:
                fixtures.connection_page()
            connect_button = next(item for item in page.descendants()
                                  if isinstance(item, ui.button) and item.text == 'Connect')
            connect = next(iter(connect_button._event_listeners.values())).handler.__closure__[0].cell_contents
            await connect()
            databases, forms = [item for item in page.descendants() if isinstance(item, DatabaseListbox)]
            await next(iter(databases._event_listeners.values())).handler(SimpleNamespace(args='db'))
            creator = creators[0]
            creator.command_select.set_value('with_form')
            self.assertEqual(creator.controls['form'].options, ['A', 'B'])
            await next(iter(forms._event_listeners.values())).handler(SimpleNamespace(args='a'))
            self.assertEqual(creator.controls['form'].options, ['A', 'B'])
            service.get_forms.assert_awaited_once()
            self.assertEqual(service.reads, [])

    async def test_command_order_fixed_slots_history_and_writes(self):
        trace = []
        original = AddFieldCommand.execute
        def execute(command, schema, **kwargs):
            trace.append((schema['id'], command.field_code))
            return original(command, schema, **kwargs)
        with patch.object(AddFieldCommand, 'execute', execute):
            manager, service = await self.evaluate([form('root', 'Root'), form('b', 'B'), form('a', 'A')], '''
WithForm(formPattern="A|B")
AddField(fieldCode="one", label="${form.name}")
AddField(fieldCode="two", label="${form.index}")
AddField(fieldCode="three", label="Three")
''')
        self.assertEqual(trace, [('a', 'one'), ('b', 'one'), ('a', 'two'), ('b', 'two'), ('a', 'three'), ('b', 'three')])
        self.assertTrue(all(e.status == 'applied' for e in manager.executions), [e.message for e in manager.executions])
        self.assertEqual([s['id'] for s in manager.target_opschema()], ['root', 'a', 'b'])
        self.assertEqual([len(s['elements']) for s in manager.executions[1].opschema], [0, 1, 1])
        self.assertEqual([len(s['elements']) for s in manager.executions[2].opschema], [0, 2, 2])
        self.assertTrue(manager.executions[1].form_iterations)
        self.assertEqual(len(manager.executions[1].iterations), 2)
        outcome = await apply_schema_sequence(service, manager.workspace.originals, manager.target_opschema(), AsyncMock())
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual([key for key, _ in service.writes], ['a', 'b'])

    async def test_inherited_and_local_variables_captures_and_field_iteration(self):
        fields = [{'id': 'x', 'code': 'old', 'type': 'text', 'label': 'Old'}]
        manager, _ = await self.evaluate([form('a', 'Site A', elements=fields), form('b', 'Site B', elements=fields)], '''
SetVariable(name="shared", method="literal", value="Inherited")
WithForm(formPattern="Site (?P<letter>.)")
SetVariable(name="local", method="literal", value="${form.letter}-${form.1}-${form.index}")
ModifyField(fieldPattern="old", fieldType="FREE_TEXT", fieldVariant="text", label="${vars.shared}: ${vars.local}")
WithForm(form="Site A")
AddField(fieldCode="after", label="${vars.shared}")
AddField(fieldCode="bad", label="${vars.local}")
''')
        self.assertEqual([e.status for e in manager.executions], ['applied'] * 6 + ['error'])
        snapshots = manager.executions[3].opschema
        self.assertEqual(snapshots[-2]['elements'][0]['label'], 'Inherited: A-A-1')
        self.assertEqual(snapshots[-1]['elements'][0]['label'], 'Inherited: B-B-2')
        self.assertIn('undefined', manager.executions[-1].message)

    async def test_reassignment_discards_inherited_name_on_exit(self):
        manager, _ = await self.evaluate([form('a', 'A')], '''
SetVariable(name="v", method="literal", value="Before")
WithForm(formPattern="A")
SetVariable(name="v", method="literal", value="Inside")
WithForm(form="A")
AddField(fieldCode="new", label="${vars.v}")
''')
        self.assertEqual(manager.executions[-1].status, 'error')
        self.assertIn('undefined', manager.executions[-1].message)

    async def test_failure_stops_other_forms_and_later_commands(self):
        fields = [{'id': 'x', 'code': 'exists', 'type': 'text', 'label': 'Exists'}]
        manager, _ = await self.evaluate([form('a', 'A', elements=fields), form('b', 'B')], '''
WithForm(formPattern="A|B")
AddField(fieldCode="exists", label="New")
AddField(fieldCode="later", label="Later")
''')
        execution = manager.executions[1]
        self.assertEqual([child.status for child in execution.iterations], ['error', 'not_executed'])
        self.assertEqual(execution.opschema[-1]['elements'], [])
        self.assertEqual(manager.executions[2].status, 'not_executed')

    async def test_zero_matches_skip_scope_then_single_switch_resumes(self):
        manager, _ = await self.evaluate([form('a', 'A')], '''
WithForm(formPattern="Missing")
AddField(fieldCode="skip", label="Skip")
WithForm(form="A")
AddField(fieldCode="new", label="New")
''')
        self.assertEqual([e.status for e in manager.executions], ['warning', 'warning', 'applied', 'applied'])
        self.assertEqual([f['code'] for f in manager.target_schema()['elements']], ['new'])

    async def test_scope_replacement_active_database_duplicate_names_and_escape(self):
        manager, _ = await self.evaluate([form('root', 'Root'), form('a', 'A'), form('b', 'A'), form('other', 'A', 'remote')], '''
SetVariable(name="p", method="literal", value="A")
WithForm(formPattern="${vars.p}")
AddField(fieldCode="first", label="$${form.name}")
WithForm(formPattern="Root")
AddField(fieldCode="second", label="${form.name}")
''')
        self.assertEqual([child.field_code for child in manager.executions[1].iterations], ['A', 'A'])
        self.assertNotIn('other', manager.workspace.originals)
        self.assertEqual(manager.executions[2].opschema[-1]['elements'][0]['label'], '${form.name}')
        self.assertEqual(manager.target_schema()['id'], 'root')
        self.assertEqual([f['code'] for f in manager.target_schema()['elements']], ['second'])

    async def test_new_scope_cannot_use_previous_form_context(self):
        manager, _ = await self.evaluate([form('a', 'A')], '''
WithForm(formPattern="A")
WithForm(formPattern="${form.name}")
''')
        self.assertEqual(manager.executions[-1].status, 'error')

    async def test_form_context_is_available_in_field_selectors(self):
        fields = [{'id': 'a', 'code': 'A', 'type': 'text', 'label': 'A'},
                  {'id': 'b', 'code': 'B', 'type': 'text', 'label': 'B'}]
        manager, _ = await self.evaluate([form('a', 'A', elements=fields), form('b', 'B', elements=fields)], '''
WithForm(formPattern="A|B")
DeleteField(fieldPattern="${form.name}")
''')
        self.assertEqual([e.status for e in manager.executions], ['applied', 'applied'])
        self.assertEqual([f['code'] for f in manager.target_opschema()[-2]['elements']], ['B'])
        self.assertEqual([f['code'] for f in manager.target_opschema()[-1]['elements']], ['A'])

    async def test_optional_capture_undefined_stops_at_first_form(self):
        manager, _ = await self.evaluate([form('a', 'A'), form('b', 'AB')], '''
WithForm(formPattern="A(?P<suffix>B)?")
AddField(fieldCode="new", label="${form.suffix}")
''')
        self.assertEqual([e.status for e in manager.executions[1].iterations], ['error', 'not_executed'])
        self.assertIn('capture group did not match', manager.executions[1].message)

    async def test_re_evaluation_resets_scope_variables_and_slots(self):
        manager, _ = await self.evaluate([form('a', 'A'), form('b', 'B')], '''
WithForm(formPattern="A|B")
SetVariable(name="local", method="literal", value="${form.name}")
AddField(fieldCode="new", label="${vars.local}")
''')
        manager.commands = parse_command_text('WithForm(form="B")\nAddField(fieldCode="bad", label="${vars.local}")')
        manager._evaluate_commands()
        await manager._evaluation_task
        self.assertEqual([s['id'] for s in manager.workspace.opschema], ['a', 'b'])
        self.assertEqual(manager.workspace.opschema[-1]['elements'], [])
        self.assertEqual(manager.executions[-1].status, 'error')

    async def test_load_failure_keeps_reached_forms_and_stops(self):
        original = fixtures.MemoryService.get_form_schema
        async def load(service, identity):
            if identity == 'b':
                raise ValueError('Form B unavailable')
            return await original(service, identity)
        with patch.object(fixtures.MemoryService, 'get_form_schema', load):
            manager, _ = await self.evaluate([form('root', 'Root'), form('a', 'A'), form('b', 'B')], '''
WithForm(formPattern="A|B")
AddField(fieldCode="later", label="Later")
''')
        self.assertEqual([e.status for e in manager.executions[0].iterations], ['applied', 'error'])
        self.assertEqual([s['id'] for s in manager.executions[0].opschema], ['root', 'a'])
        self.assertEqual(manager.executions[1].status, 'not_executed')

    async def test_zero_field_matches_keep_parent_warnings_without_applications(self):
        manager, _ = await self.evaluate([form('a', 'A'), form('b', 'B')], '''
WithForm(formPattern="A|B")
DeleteField(fieldPattern="missing")
''')
        badges = [item.text for item in manager.simple_content.descendants() if isinstance(item, ui.badge)]
        self.assertEqual(badges, ['2x', '0x'])
        buttons = [item for item in manager.simple_content.descendants() if isinstance(item, ui.button)
                   and item.props.get('aria-label') == 'Show iterations']
        self.assertEqual([item.enabled for item in buttons], [True, False])
        self.assertEqual(manager.executions[1].iterations, ())
        self.assertEqual(manager.executions[1].status, 'warning')
        icons = [item.props['name'] for item in manager.simple_content.descendants() if isinstance(item, ui.icon)]
        self.assertIn('warning', icons)
        self.assertIn('(A)', manager.executions[1].message)
        self.assertIn('(B)', manager.executions[1].message)

    @staticmethod
    def repeated_forms():
        def fields(codes):
            return [dict(id=code, code=code, type='text', label='Old') for code in codes]
        return [form('root', 'Root'), form('a', 'A', elements=fields(['f_i', 'f_ii'])),
                form('b', 'B', elements=fields(['f_i', 'f_ii', 'f_iii']))]

    async def test_double_iteration_flattens_five_applications_and_full_snapshots(self):
        manager, _ = await self.evaluate(self.repeated_forms(), '''
WithForm(formPattern="A|B")
SetVariable(name="label", method="literal", value="${form.name}")
ModifyField(fieldPattern="f_.*", fieldType="FREE_TEXT", fieldVariant="text", label="${vars.label}/${field.code}")
''')
        self.assertEqual([len(e.iterations) for e in manager.executions], [2, 2, 5])
        applications = manager.executions[2].iterations
        labels = ['A / f_i', 'A / f_ii', 'B / f_i', 'B / f_ii', 'B / f_iii']
        self.assertEqual([e.application_label for e in applications], labels)
        self.assertEqual([e.schema['id'] for e in applications], ['a', 'a', 'b', 'b', 'b'])
        for index, child in enumerate(applications):
            self.assertEqual([s['id'] for s in child.opschema], ['root', 'a', 'b'])
            changed = [f['label'] for s in child.opschema for f in s['elements'] if f['label'] != 'Old']
            self.assertEqual(len(changed), index + 1)
        self.assertEqual([f['label'] for f in applications[1].opschema[-1]['elements']], ['Old'] * 3)
        self.assertEqual([f['label'] for f in applications[2].opschema[-1]['elements']], ['B/f_i', 'Old', 'Old'])
        selected = []
        manager.on_selection_change = lambda index, execution: selected.append(execution)
        manager.toggle_iterations(2)
        manager.select_iteration(2, 1)
        self.assertIs(selected[-1], applications[1])
        ui_labels = [item.text for item in manager.simple_content.descendants() if isinstance(item, ui.label)]
        for index, label in enumerate(labels, 1):
            self.assertIn(f'Iteration {index} ({label})', ui_labels)
        badges = [item.text for item in manager.simple_content.descendants() if isinstance(item, ui.badge)]
        self.assertEqual(badges, ['2x', '2x', '5x'])
        self.assertEqual(len(manager.commands), 3)
        manager._regenerate_advanced_draft()
        self.assertEqual(len(parse_command_text(manager.advanced_draft)), 3)
        manager.toggle_iterations(2)
        self.assertIsNone(manager.selected_iteration)
        self.assertIs(selected[-1], manager.executions[2])

    async def test_double_iteration_failure_retains_all_planned_fields(self):
        manager, _ = await self.evaluate(self.repeated_forms(), '''
WithForm(formPattern="A|B")
ModifyField(fieldPattern="f_.*", fieldType="FREE_TEXT", fieldVariant="text", newCode="collision")
DeleteField(fieldCode="later")
''')
        applications = manager.executions[1].iterations
        self.assertEqual([e.application_label for e in applications],
                         ['A / f_i', 'A / f_ii', 'B / f_i', 'B / f_ii', 'B / f_iii'])
        self.assertEqual([e.status for e in applications], ['applied', 'error'] + ['not_executed'] * 3)
        self.assertEqual([f['code'] for f in applications[1].opschema[-2]['elements']], ['collision', 'f_ii'])
        self.assertEqual([f['code'] for f in applications[1].opschema[-1]['elements']], ['f_i', 'f_ii', 'f_iii'])
        self.assertEqual(manager.executions[2].status, 'not_executed')

    async def test_single_commands_in_scope_expand_with_form_and_field_context(self):
        manager, _ = await self.evaluate(self.repeated_forms(), '''
WithForm(formPattern="A|B")
SetVariable(name="v", method="literal", value="${form.name}")
MoveField(fieldCode="f_i", position="End")
''')
        self.assertEqual([c.application_label for c in manager.executions[0].iterations], ['A', 'B'])
        self.assertEqual([c.application_label for c in manager.executions[1].iterations], ['A', 'B'])
        self.assertEqual([c.application_label for c in manager.executions[2].iterations], ['A / f_i', 'B / f_i'])
        selected = []
        manager.on_selection_change = lambda index, execution: selected.append(execution)
        manager.select_iteration(0, 0)
        self.assertEqual([s['id'] for s in selected[-1].opschema], ['root', 'a'])
        manager.select_iteration(2, 0)
        self.assertEqual([f['code'] for f in selected[-1].opschema[-2]['elements']], ['f_ii', 'f_i'])
        self.assertEqual([f['code'] for f in selected[-1].opschema[-1]['elements']], ['f_i', 'f_ii', 'f_iii'])

    async def test_mixed_zero_match_and_success_count_only_real_applications(self):
        manager, _ = await self.evaluate(self.repeated_forms(), '''
WithForm(formPattern="A|B")
DeleteField(fieldPattern="f_iii")
''')
        execution = manager.executions[1]
        self.assertEqual(execution.status, 'warning')
        self.assertEqual([e.application_label for e in execution.iterations], ['B / f_iii'])
        self.assertIn('No field codes matched', execution.message)

    async def test_failed_parameter_preparation_retains_the_full_snapshot(self):
        manager, _ = await self.evaluate(self.repeated_forms(), '''
WithForm(formPattern="A|B")
ModifyField(fieldPattern="f_(?P<optional>x)?.*", fieldType="FREE_TEXT", fieldVariant="text", label="${field.optional}")
''')
        applications = manager.executions[1].iterations
        self.assertEqual([e.status for e in applications], ['error'] + ['not_executed'] * 4)
        self.assertEqual([s['id'] for s in applications[0].opschema], ['root', 'a', 'b'])
        self.assertEqual([f['label'] for f in applications[0].opschema[-2]['elements']], ['Old', 'Old'])

    async def test_selecting_flat_child_displays_its_historical_form_tabs(self):
        managers = []
        def create(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager
        with patch('activity_manager.page.CommandListManager', side_effect=create):
            with Client(ui.page('/')), ui.column() as page:
                fixtures.connection_page()
        manager = managers[0]
        forms = self.repeated_forms()
        manager.form_service = fixtures.MemoryService(forms)
        async def resolve(data):
            return await fixtures.resolve_command_schema(manager.form_service, data, manager.related_schemas,
                                                         {}, {}, {}, source_schema=manager.workspace.originals[data['id']])
        manager.resolve_schema = resolve
        manager.set_schema(forms[0])
        manager.commands = parse_command_text('''
WithForm(formPattern="A|B")
ModifyField(fieldPattern="f_.*", fieldType="FREE_TEXT", fieldVariant="text", label="${form.name}/${field.code}")
''')
        manager._evaluate_commands()
        await manager._evaluation_task
        tabs = next(item for item in page.descendants() if isinstance(item, ui.tabs))
        code = next(item for item in page.descendants() if isinstance(item, ui.code))
        manager.select_iteration(1, 1)
        self.assertEqual(tabs.value, 'a')
        self.assertIn('A/f_ii', code.content)
        tabs.set_value('b')
        self.assertNotIn('B/f_i', code.content)
        self.assertIn('Old', code.content)
        manager.select_iteration(1, 2)
        self.assertEqual(tabs.value, 'b')
        self.assertIn('B/f_i', code.content)
        self.assertNotIn('B/f_ii', code.content)
        manager.toggle_iterations(1)
        self.assertIsNone(manager.selected_iteration)
        self.assertIn('B/f_iii', code.content)


class FormSelectorTests(unittest.TestCase):
    def test_parser_roundtrip_and_invalid_selectors(self):
        command = parse_command_text('WithForm(formPattern=r"Site (?P<region>.*)")')[0]
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        for arguments in ('', 'form="A", formPattern=".*"', 'formPattern="["',
                          'formPattern="(?P<name>.*)"', 'formPattern="(?P<index>.*)"'):
            with self.subTest(arguments=arguments), self.assertRaises(CommandTextError):
                parse_command_text(f'WithForm({arguments})')

    def test_creator_radio_pattern_and_single_mode(self):
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((WITH_FORM,), on_add=added.append)
            self.assertIsInstance(creator.target_mode, ui.radio)
            self.assertEqual(creator.target_mode.options, ['Single', 'Multiple'])
            creator.target_mode.set_value('Multiple')
            creator.controls['form'].set_value('Site (?P<region>.*)')
            creator.set_suggestions('form_names', ['Site A'])
            self.assertEqual(creator.controls['form'].options, [])
            creator._add_command()
            self.assertEqual(added[0].form_pattern, 'Site (?P<region>.*)')
            creator.controls['form'].set_value('[')
            self.assertFalse(creator.add_button.enabled)
            creator.target_mode.set_value('Single')
            creator.controls['form'].set_value('Site A')
            creator._add_command()
            self.assertEqual(added[-1].form, 'Site A')
