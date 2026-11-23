from copy import deepcopy
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_parser import parse_command_text
from activity_manager.command_list import CommandListManager
from activity_manager.commands.add_field import AddFieldCommand
from activity_manager.commands.add_form import AddFormCommand
from activity_manager.commands.delete_field import DeleteFieldCommand
from activity_manager.commands.modify_field import ModifyFieldCommand
from activity_manager.commands.modify_form import ModifyFormCommand
from activity_manager.commands.move_field import MoveFieldCommand
from activity_manager.form_comparison import (ComparisonOptions, generate_commands,
    mapping_choices, field_caption)
from activity_manager.form_comparison_ui import FormComparisonRenderer


def field(code, kind='FREE_TEXT', **properties):
    return dict(id='id_' + (code or 'uncoded'), code=code, label=code or 'Uncoded',
                type=kind, **properties)


def form(identity, fields, **properties):
    return dict(id=identity, label=identity.title(), databaseId='db', elements=fields, **properties)


class ComparisonTests(unittest.TestCase):
    def test_pre_mapping_code_case_type_variant_and_uncoded(self):
        model = form('model', [field('A'), field('B', 'quantity'), field('C'), field(None)])
        initial = form('initial', [field('a'), field('B'),
            field('C', typeParameters={'barcode': True}), field(None)])
        choices, defaults = mapping_choices(model, initial, {})
        self.assertEqual(defaults, [0, None, None, None])
        self.assertEqual(choices[1], [])
        self.assertIn(3, choices[0])
        self.assertNotIn(2, choices[0])
        self.assertEqual(field_caption(model['elements'][0]), 'A (A)')

    def test_reference_matching_local_names_external_identity_and_mixed_scope(self):
        def reference(code, target):
            return field(code, 'reference', typeParameters={'range': [{'formId': target}]})
        model = form('model', [reference('A', 'local'), reference('B', 'remote')])
        initial = form('initial', [reference('A', 'other_local'), reference('B', 'remote'),
                                   reference('C', 'other_remote')])
        related = {'local': form('local', []), 'other_local': form('other_local', []),
                   'remote': form('remote', []), 'other_remote': form('other_remote', [])}
        for schema in related.values():
            schema['label'] = 'Same Name'
        for identity in ('remote', 'other_remote'):
            related[identity]['databaseId'] = 'remote_db'
        choices, defaults = mapping_choices(model, initial, related)
        self.assertEqual(defaults, [0, 1])
        self.assertEqual(choices, [[0], [1]])

    def test_new_form_and_no_initial_fallback_end_with_record_label(self):
        model = form('model', [field('A'), field('B', 'quantity')], recordLabelFieldId='id_B')
        original = deepcopy(model)
        commands = generate_commands(model, folder='/Parent/Child')
        self.assertEqual(commands[0], AddFormCommand('Model', '/Parent/Child'))
        self.assertEqual(commands[-1], ModifyFormCommand(True, record_label_field_id='B'))
        self.assertEqual(commands[1].position, 'Beginning')
        self.assertEqual(commands[2].before_after_field, 'A')
        self.assertEqual(commands, parse_command_text('\n'.join(c.to_syntax() for c in commands)))
        self.assertEqual(model, original)

    def test_explicit_new_allows_same_initial_model(self):
        model = form('model', [field('A')])
        with self.assertRaises(ValueError):
            generate_commands(model, model)
        self.assertIsInstance(generate_commands(model, model,
            options=ComparisonOptions(create_new=True))[0], AddFormCommand)

    def test_preserve_order_inserts_new_fields_after_previous_model_field(self):
        model = form('model', [field('A'), field('B'), field('C')])
        initial = form('initial', [field('C'), field('A'), field('extra')])
        commands = generate_commands(model, initial,
            options=ComparisonOptions(preserve_order=True, delete_unmatched=False))
        self.assertFalse(any(isinstance(c, MoveFieldCommand) for c in commands))
        self.assertFalse(any(isinstance(c, DeleteFieldCommand) for c in commands))
        added = next(c for c in commands if isinstance(c, AddFieldCommand))
        self.assertEqual((added.field_code, added.position, added.before_after_field), ('B', 'After field', 'A'))
        self.assertEqual(commands[-1].new_name, 'Initial')

    def test_ordering_and_code_renames_apply_to_result(self):
        model = form('model', [field('A'), field('B'), field('C')], recordLabelFieldId='id_C')
        initial = form('initial', [field('C'), field('old'), field('A')])
        commands = generate_commands(model, initial, mapping=[2, 1, 0])
        result = initial
        for command in commands:
            applied = command.execute(result)
            self.assertFalse(applied.application_failed, applied.message)
            result = applied.schema
        self.assertEqual([f['code'] for f in result['elements']], ['A', 'B', 'C'])
        self.assertEqual(result['recordLabelFieldId'], 'id_C')
        self.assertEqual(result['label'], 'Initial')

    def test_unmatched_first_field_added_at_beginning_even_when_preserving(self):
        commands = generate_commands(form('model', [field('A')]), form('initial', []),
                                     options=ComparisonOptions(preserve_order=True))
        self.assertEqual(commands[0].position, 'Beginning')

    def test_conflict_renamed_before_add_and_deleted_using_new_code(self):
        commands = generate_commands(form('model', [field('A', 'quantity')]),
                                     form('initial', [field('A'), field('A_OLD')]))
        self.assertEqual(commands[0].changes, {'new_code': 'A_OLD'})
        self.assertIsInstance(commands[1], AddFieldCommand)
        self.assertEqual([c.field_code for c in commands if isinstance(c, DeleteFieldCommand)],
                         ['A_OLD', 'A_OLD'])  # Collisions intentionally remain for Apply to flag.

    def test_equal_blank_null_missing_and_overwrite(self):
        model = form('model', [field('A', description='', required=False, readOnly=None)])
        initial = form('initial', [field('A', description='old', required=False, readOnly=True,
                                         validationMessage='old', typeParameters={'inputMask': ''})])
        commands = generate_commands(model, initial)
        changes = commands[0].changes
        self.assertIsNone(changes['description'])
        self.assertIsNone(changes['read_only'])
        self.assertIsNone(changes['input_mask'])
        self.assertFalse(changes['required'])
        self.assertEqual(changes['label'], 'A')
        self.assertNotIn('default_formula', changes)
        self.assertIn('description=""', commands[0].to_syntax())
        retained = generate_commands(model, initial, options=ComparisonOptions(overwrite_missing=False))[0].changes
        self.assertNotIn('read_only', retained)
        self.assertNotIn('input_mask', retained)
        self.assertIn('description', retained)

    def test_manual_mapping_uncoded_selector_and_one_to_one_validation(self):
        model = form('model', [field('A'), field('B')])
        initial = form('initial', [field(None)])
        with self.assertRaises(ValueError):
            generate_commands(model, initial, mapping=[0, 0])
        commands = generate_commands(model, initial, mapping=[0, None])
        self.assertEqual(commands[0].field_code, 'Uncoded')
        self.assertEqual(commands[0].changes['new_code'], 'A')

    def test_subforms_left_untouched_and_not_created_or_deleted(self):
        model = form('model', [field('newsub', 'subform'), field('A')])
        initial = form('initial', [field('oldsub', 'subform'), field('A')])
        commands = generate_commands(model, initial)
        self.assertFalse(any(isinstance(c, (AddFieldCommand, DeleteFieldCommand)) for c in commands))

    def test_names_formulas_options_lookups_reverse_and_self_references(self):
        model = form('model', [field('A'), field('Calc', 'calculated', typeParameters={'formula': 'id_A'}),
            field('Enum', 'enumerated', typeParameters={'values': [{'id': 'opt', 'label': 'One'}]}),
            field('Ref', 'reference', typeParameters={'range': [{'formId': 'remote'}],
                'lookupConfigs': [{'id': 'lookup', 'lookupLabel': 'Value', 'formula': 'id_R'}]}),
            field('Local', 'reference', typeParameters={'range': [{'formId': 'model'}]}),
            field('Reverse', 'reversereference', typeParameters={'formId': 'remote', 'fieldId': 'id_R'})])
        remote = form('remote', [field('R')])
        remote['databaseId'] = 'remote_db'
        commands = generate_commands(model, related={'remote': remote}, database_names={'remote_db': 'Other DB'})
        added = {c.field_code: c for c in commands if isinstance(c, AddFieldCommand)}
        self.assertEqual(added['Calc'].calculation_formula, 'A')
        self.assertEqual(added['Enum'].option_list['values'], [{'label': 'One'}])
        self.assertEqual(added['Ref'].referenced_form, 'Other DB/Remote')
        self.assertEqual(added['Ref'].lookup_fields, [{'label': 'Value', 'formula': 'R'}])
        self.assertEqual(added['Local'].referenced_form, 'Model')
        self.assertEqual(added['Reverse'].referenced_form, 'Other DB/Remote')
        self.assertEqual(added['Reverse'].reverse_reference_field, 'R')


class ComparisonUiTests(unittest.IsolatedAsyncioTestCase):
    def renderer(self):
        return FormComparisonRenderer(load_comparison=AsyncMock(), replace_commands=Mock(), context_key=lambda: 'context')

    async def test_defaults_identical_form_guard_new_override_and_busy(self):
        with Client(ui.page('/')), ui.column():
            renderer = self.renderer()
            renderer.set_forms({'a': 'Alpha'})
            renderer.model.set_value('a')
            renderer.set_initial('a')
            self.assertFalse(renderer.generate.enabled)
            self.assertEqual(renderer.mode.value, 'initial')
            self.assertTrue(renderer.delete.value)
            self.assertFalse(renderer.preserve.value)
            self.assertTrue(renderer.overwrite.value)
            renderer.mode.set_value('new')
            renderer.refresh()
            self.assertTrue(renderer.generate.enabled)
            self.assertFalse(renderer.comparison_settings.visible)
            renderer.set_enabled(False)
            self.assertFalse(renderer.generate.enabled)
            renderer.set_enabled(True)
            renderer.set_initial(None)
            renderer.mode.set_value('initial')
            renderer.refresh()
            self.assertTrue(renderer.generate.enabled)
            renderer.set_forms({})
            self.assertEqual(renderer.model.value, 'a')
            self.assertTrue(renderer.generate.enabled)
            renderer.set_forms({'b': 'Beta'})
            self.assertEqual(renderer.model.value, 'a')
            self.assertEqual(renderer.model.options, {'a': 'Alpha', 'b': 'Beta'})
            renderer.model.set_value(None)
            renderer.refresh()
            self.assertFalse(renderer.generate.enabled)

    async def test_load_failure_and_stale_context_preserve_commands(self):
        with Client(ui.page('/')), ui.column():
            renderer = self.renderer()
            renderer.set_forms({'model': 'Model'})
            renderer.model.set_value('model')
            renderer.refresh()
            renderer.load_comparison.side_effect = ValueError('unavailable')
            await renderer._generate()
            renderer.replace_commands.assert_not_called()
            self.assertIn('unavailable', renderer.status.text)
            renderer.load_comparison.side_effect = None
            renderer.context_key = Mock(side_effect=['before', 'after'])
            await renderer._generate()
            renderer.replace_commands.assert_not_called()
            self.assertIn('changed', renderer.status.text)

    async def test_replacement_applies_once_without_execution(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(on_execute=Mock())
            commands = generate_commands(form('model', [field('A')]))
            with patch.object(manager, '_evaluate_commands') as apply:
                manager.replace_commands(commands)
            apply.assert_called_once()
            manager.on_execute.assert_not_called()
            self.assertEqual(manager.commands, commands)
            self.assertEqual(manager.stored_text, manager.advanced_draft)
            self.assertIn('AddForm', manager.advanced_draft)

    async def test_mapping_dialog_disables_selected_fields_elsewhere_and_cancel(self):
        with Client(ui.page('/')) as client, ui.column():
            renderer = self.renderer()
            model, initial = form('model', [field('A'), field('B')]), form('initial', [field('A')])
            task = asyncio.create_task(renderer._map_fields(model, initial, {}))
            await asyncio.sleep(0)
            dialog = next(e for e in client.elements.values() if isinstance(e, ui.dialog))
            selectors = [e for e in dialog.descendants() if isinstance(e, ui.select)]
            self.assertEqual(selectors[0].value, 0)
            self.assertEqual(selectors[1].options, {-1: 'Create New'})
            selectors[0].set_value(-1)
            await asyncio.sleep(0)
            self.assertIn(0, selectors[1].options)
            dialog.submit(None)
            self.assertIsNone(await task)

    async def test_generation_confirmation_and_cancellation(self):
        for confirm in (False, True):
            with self.subTest(confirm=confirm), Client(ui.page('/')) as client, ui.column():
                renderer = self.renderer()
                renderer.set_forms({'model': 'Model'})
                renderer.model.set_value('model')
                renderer.refresh()
                renderer.load_comparison.return_value = dict(model=form('model', [field('A')]),
                                                             initial=None, related={})
                task = asyncio.create_task(renderer._generate())
                await asyncio.sleep(0)
                dialog = next(e for e in client.elements.values() if isinstance(e, ui.dialog))
                renderer.replace_commands.assert_not_called()
                dialog.submit(confirm)
                await task
                self.assertEqual(renderer.replace_commands.call_count, int(confirm))

    async def test_page_loads_saved_schemas_and_model_folder_and_applies_to_initial(self):
        from activity_manager.page import connection_page
        from activity_manager.listbox import DatabaseListbox
        from test.test_move_form import FolderService
        from test.test_executor_page import callback

        remote_initial = form('remote_initial', [field('A')])
        remote_initial['databaseId'] = 'remote'
        service = FolderService([form('initial', [field('A')]), form('model', [field('A'), field('B')]), remote_initial])
        service.get_databases = AsyncMock(return_value={'db': 'Database', 'remote': 'Remote'})
        service.get_forms = AsyncMock(side_effect=lambda database: {
            identity: schema['label'] for identity, schema in service.schemas.items()
            if schema['databaseId'] == database})
        service.get_form_tree = AsyncMock(side_effect=lambda identity: {identity: deepcopy(service.schemas[identity])})
        service.aclose = AsyncMock()
        service.resources['db']['model']['parentId'] = 'archive'
        renderers, managers = [], []
        def make_renderer(**kwargs):
            result = FormComparisonRenderer(**kwargs)
            renderers.append(result)
            return result
        def make_manager(**kwargs):
            result = CommandListManager(**kwargs)
            managers.append(result)
            return result
        store = Mock()
        store.load.return_value = (None, '')
        with patch('activity_manager.page.AppStateStore', return_value=store), \
                patch('activity_manager.page.ActivityInfoService', return_value=service), \
                patch('activity_manager.page.FormComparisonRenderer', side_effect=make_renderer), \
                patch('activity_manager.page.CommandListManager', side_effect=make_manager), \
                patch('activity_manager.page.ui.timer'):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
                await callback(next(e for e in page.descendants() if isinstance(e, ui.button) and e.text == 'Connect'))()
                databases, forms = [e for e in page.descendants() if isinstance(e, DatabaseListbox)]
                await callback(databases)(SimpleNamespace(args='db'))
                await callback(forms)(SimpleNamespace(args='initial'))
                renderer, manager = renderers[0], managers[0]
                self.assertEqual(renderer.initial_id, 'initial')
                self.assertEqual(set(renderer.model.options), {'initial', 'model'})
                manager.add_command(AddFieldCommand(field_code='LocalOnly', label='Local only'))
                await manager._evaluation_task
                self.assertIn('LocalOnly', [f['code'] for f in manager.workspace.active_schema['elements']])
                saved = await renderer.load_comparison('model', False)
                self.assertEqual([f['code'] for f in saved['initial']['elements']], ['A'])
                renderer.replace_commands(generate_commands(**saved))
                await manager._evaluation_task
                self.assertEqual([f['code'] for f in manager.workspace.active_schema['elements']], ['A', 'B'])
                self.assertTrue(all(e.status == 'applied' for e in manager.executions))
                new = await renderer.load_comparison('model', True)
                self.assertIsNone(new['initial'])
                self.assertEqual(new['folder'], '/North/Archive')
                await callback(databases)(SimpleNamespace(args='db'))
                self.assertIsNone(renderer.initial_id)
                renderer.model.set_value('model')
                await callback(databases)(SimpleNamespace(args='remote'))
                self.assertEqual(renderer.model.value, 'model')
                self.assertEqual(set(renderer.model.options), {'model', 'remote_initial'})
                await callback(forms)(SimpleNamespace(args='remote_initial'))
                cross_database = await renderer.load_comparison('model', False)
                self.assertEqual(cross_database['model']['databaseId'], 'db')
                self.assertEqual(cross_database['initial']['databaseId'], 'remote')
                renderer.replace_commands(generate_commands(**cross_database))
                await manager._evaluation_task
                self.assertEqual(manager.workspace.active_schema['id'], 'remote_initial')
                self.assertEqual([f['code'] for f in manager.workspace.active_schema['elements']], ['A', 'B'])
                new = await renderer.load_comparison('model', True)
                self.assertEqual(new['folder'], '/North/Archive')
                self.assertEqual(service.writes, [])


if __name__ == '__main__':
    unittest.main()
