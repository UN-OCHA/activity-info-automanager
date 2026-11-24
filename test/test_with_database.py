from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.applier import Applier
from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_list import CommandListManager
from activity_manager.command_execution import ExecutionResult
from activity_manager.command_parser import parse_command_text
from activity_manager.planning import ActionKind
from activity_manager.page import connection_page
from test.form_command_runtime import execute
from test.test_multi_form import MemoryService, form


async def evaluate(text, schemas=(), *, names=None, initial=None, service=None):
    if service is None:
        service = MemoryService(schemas)
    if names is not None:
        async def get_database_names():
            return names
        service.get_database_names = get_database_names

    async def resolve(schema):
        return ExecutionResult(schema)

    app = Applier(initial, parse_command_text(text), form_service=service, resolve_schema=resolve)
    await app.evaluate()
    return app


class WithDatabaseTests(IsolatedAsyncioTestCase):
    async def test_single_switch_matches_names_and_exact_ids_and_clears_active_form(self):
        first, second = form('a', 'One'), form('B-id', 'Two', 'remote')
        app = await evaluate('''
WithDatabase(database="REMOTE")
AddField(fieldCode="SKIP", label="Skip")
WithForm(form="B-id")
WithDatabase(database="db")
''', [first, second], initial=first)
        self.assertEqual([result.status for result in app.executions], ['applied', 'warning', 'applied', 'applied'])
        self.assertEqual(app.executions[1].diagnostics[0].code, 'missing_form')
        self.assertEqual(app.active_database_ids, ('db',))
        self.assertEqual(app.active_form_schemas, ())

    async def test_no_match_and_ambiguous_names_are_errors_that_clear_context(self):
        initial = form('a', 'One')
        for selector in ('database="local"', 'databasePattern="NotHere"', 'database="DB"'):
            with self.subTest(selector=selector):
                app = await evaluate(f'WithDatabase({selector})\nWithForm(form="One")', [initial],
                                     initial=initial, names={'db': 'Local', 'remote': 'LOCAL'})
                self.assertEqual([result.status for result in app.executions], ['error', 'not_executed'])
                self.assertEqual(app.active_database_ids, ())
                self.assertEqual(app.active_form_schemas, ())

    async def test_single_regex_match_binds_database_captures(self):
        app = await evaluate('''
WithDatabase(databasePattern="(?P<region>Rem)ote")
WithForm(formPattern="${database.region}")
AddField(fieldCode="TAG", label="${database.1}-${database.index}")
''', [form('b', 'Rem', 'remote')])
        self.assertEqual([result.status for result in app.executions], ['applied'] * 3)
        self.assertEqual(app.workspace.active_schema['elements'][0]['label'], 'Rem-1')

    async def test_nested_iterations_flatten_with_empty_field_warning_and_form_first_plan(self):
        fields = [{'id': f'id{i}', 'code': f'F{i}', 'type': 'text', 'label': f'Field {i}'} for i in range(3)]
        schemas = [form(f'a{i}', f'Form {i}', elements=fields) for i in range(2)]
        schemas += [form(f'b{i}', f'Form {i}', 'remote', fields if i < 2 else ()) for i in range(3)]
        service = MemoryService(schemas)
        app = await evaluate('''
WithDatabase(databasePattern="Local|Remote")
WithForm(formPattern="Form .*")
ModifyField(fieldPattern="F[0-2]", fieldType="FREE_TEXT", fieldVariant="text", label="${database.name} ${form.index} ${field.code}")
AddField(fieldCode="NEW", label="New")
''', service=service)
        switch, forms, changes, adds = app.executions
        self.assertEqual([leaf.application_label for leaf in switch.iterations], ['Local', 'Remote'])
        self.assertEqual([leaf.application_label for leaf in forms.iterations],
                         ['Local / Form 0', 'Local / Form 1', 'Remote / Form 0', 'Remote / Form 1', 'Remote / Form 2'])
        self.assertEqual(len(changes.iterations), 12, changes.message)
        self.assertEqual(changes.iterations[0].application_label, 'Local / Form 0 / F0')
        self.assertEqual(changes.iterations[-1].application_label, 'Remote / Form 1 / F2')
        self.assertEqual(changes.status, 'warning')
        lines = changes.message.splitlines()
        self.assertEqual(len(lines), 13)
        for number, child in enumerate(changes.iterations, 1):
            self.assertIn(f' — Iteration {number} — ', lines[number - 1])
            self.assertEqual(child.console_message, lines[number - 1])
        self.assertTrue(lines[0].startswith('Database 1 (Local) / Form 1 (Form 0) / Field 1 (F0) — Iteration 1'))
        self.assertTrue(lines[6].startswith('Database 2 (Remote) / Form 1 (Form 0) / Field 1 (F0) — Iteration 7'))
        self.assertTrue(lines[-1].startswith('Database 2 (Remote) / Form 3 (Form 2) — Warning: No field codes matched'))
        self.assertEqual(len(adds.iterations), 5)
        self.assertEqual([action.key.identity for action in app.candidate_plan.actions],
                         ['a0', 'a1', 'b0', 'b1', 'b2'])
        self.assertEqual([len(action.application_ids) for action in app.candidate_plan.actions], [4, 4, 4, 4, 1])
        report = await execute(app, service)
        self.assertEqual(report.status, 'success', report.messages)
        self.assertEqual([identity for identity, _ in service.writes], ['a0', 'a1', 'b0', 'b1', 'b2'])

    async def test_database_variables_survive_form_scope_and_qualified_selection_exits(self):
        schemas = [form('a', 'Shared'), form('b', 'shared', 'remote'), form('c', 'Other', 'remote')]
        app = await evaluate('''
WithDatabase(databasePattern=".*")
SetVariable(name="tag", method="literal", value="${database.name}")
WithForm(formPattern="(?i)shared")
SetVariable(name="tag", method="literal", value="${form.name}")
AddField(fieldCode="INNER", label="${vars.tag}")
WithForm(form="SHARED")
AddField(fieldCode="OUTER", label="${vars.tag}")
WithForm(form="remote/oThEr")
AddField(fieldCode="FINAL", label="Final")
''', schemas)
        self.assertTrue(all(result.status == 'applied' for result in app.executions),
                        [result.message for result in app.executions])
        self.assertEqual([field['label'] for field in app.workspace.latest['a']['elements']], ['Shared', 'Local'])
        self.assertEqual([field['label'] for field in app.workspace.latest['b']['elements']], ['shared', 'Remote'])
        self.assertEqual(app.active_database_ids, ('remote',))
        self.assertFalse(app.database_scope)
        self.assertEqual([schema['id'] for schema in app.active_form_schemas], ['c'])

    async def test_no_form_branch_is_retained_for_later_selectors(self):
        schemas = [form('a', 'Only'), form('b', 'Other', 'remote')]
        app = await evaluate('''
WithDatabase(databasePattern=".*")
WithForm(formPattern="Only")
AddField(fieldCode="ONE", label="One")
WithForm(formPattern="Other")
AddField(fieldCode="TWO", label="Two")
''', schemas)
        self.assertEqual([leaf.application_label for leaf in app.executions[1].iterations], ['Local / Only'])
        self.assertIn('Database 2 (Remote) — Warning: No form names matched', app.executions[1].message)
        first, second = app.executions[3].message.splitlines()
        self.assertEqual(first, 'Database 1 (Local) — Warning: No form names matched the pattern.')
        self.assertIn('Database 2 (Remote) / Form 1 (Other) — Iteration 1 — ', second)
        self.assertEqual([leaf.application_label for leaf in app.executions[3].iterations], ['Remote / Other'])
        self.assertEqual(app.active_database_ids, ('db', 'remote'))

    async def test_creation_fans_out_and_edits_fold_into_both_creations(self):
        app = await evaluate('''
WithDatabase(databasePattern=".*")
AddForm(name="New")
AddField(fieldCode="ONE", label="One")
AddField(fieldCode="TWO", label="Two")
''')
        self.assertTrue(all(result.status == 'applied' for result in app.executions),
                        [result.message for result in app.executions])
        actions = app.candidate_plan.actions
        self.assertEqual([action.kind for action in actions], [ActionKind.FORM_CREATE] * 2)
        self.assertEqual([action.after.payload.schema['databaseId'] for action in actions], ['db', 'remote'])
        self.assertEqual([len(action.after.payload.schema['elements']) for action in actions], [2, 2])

    async def test_qualified_creation_and_failed_qualified_operations(self):
        app = await evaluate('''
WithDatabase(databasePattern=".*")
AddForm(name="REMOTE/New")
AddModifyForm(name="rEmOtE/nEw")
''')
        self.assertEqual([result.status for result in app.executions], ['applied'] * 3)
        self.assertEqual(app.active_database_ids, ('remote',))
        self.assertEqual(app.workspace.active_schema['label'], 'New')
        self.assertEqual(len(app.candidate_plan.actions), 1)

        for command in ('AddForm(name="Unknown/New")', 'AddModifyForm(name="Unknown/New")',
                        'WithForm(form="Remote/Missing")'):
            with self.subTest(command=command):
                app = await evaluate('WithDatabase(databasePattern=".*")\n' + command)
                self.assertIn(app.executions[-1].status, ('warning', 'error'))
                self.assertEqual(app.active_database_ids, ())
                self.assertIsNone(app.workspace.active_schema)
                self.assertFalse(app.candidate_plan.actions)

    async def test_qualified_template_with_multiple_destinations_errors(self):
        app = await evaluate('''
WithDatabase(databasePattern=".*")
AddForm(name="${database.name}/New")
''')
        self.assertEqual(app.executions[-1].status, 'error')
        self.assertIn('multiple destinations', app.executions[-1].message)
        self.assertEqual(app.active_database_ids, ())
        self.assertFalse(app.candidate_plan.actions)

    async def test_casefolded_form_ambiguity_can_be_resolved_by_exact_id(self):
        schemas = [form('First-id', 'Duplicate'), form('Second-id', 'duplicate')]
        app = await evaluate('''
WithDatabase(database="local")
WithForm(form="DUPLICATE")
ModifyForm(currentForm=False, form="Second-id", name="Renamed")
WithForm(form="rEnAmEd")
''', schemas)
        self.assertEqual([result.status for result in app.executions], ['applied', 'warning', 'applied', 'applied'])
        self.assertIn('multiple forms', app.executions[1].message)
        self.assertEqual(app.workspace.active_schema['id'], 'Second-id')

    async def test_delete_single_fans_out_and_keeps_database_scope(self):
        schemas = [form('a', 'Shared'), form('b', 'shared', 'remote')]
        app = await evaluate('''
WithDatabase(databasePattern=".*")
DeleteForm(currentForm=False, form="SHARED")
''', schemas)
        self.assertEqual([leaf.application_label for leaf in app.executions[-1].iterations],
                         ['Local / Shared', 'Remote / shared'])
        self.assertEqual(app.active_form_schemas, ())
        self.assertEqual(app.active_database_ids, ('db', 'remote'))
        self.assertEqual([action.key.identity for action in app.candidate_plan.actions], ['db', 'remote'])


class WithDatabaseCreatorTests(IsolatedAsyncioTestCase):
    async def test_database_modes_and_unqualified_union_suggestions(self):
        def field(code):
            return {'id': f'id{code}', 'code': code, 'type': 'text', 'label': code}

        local = form('a', 'Shared', elements=[field('Z'), field('Common')])
        remote = form('b', 'shared', 'remote', [field('Alpha'), field('Common')])
        service = MemoryService([local, remote, form('c', 'Before', 'remote')])
        service.get_databases = AsyncMock(return_value={'db': 'Local (db)', 'remote': 'Remote (remote)'})
        service.get_form_tree = AsyncMock(side_effect=lambda identity: {identity: service.schemas[identity]})
        creators, managers = [], []

        def create_renderer(*args, **kwargs):
            creator = CommandCreatorRenderer(*args, **kwargs)
            creators.append(creator)
            return creator

        def create_manager(*args, **kwargs):
            manager = CommandListManager(*args, **kwargs)
            managers.append(manager)
            return manager

        with patch('activity_manager.page.ActivityInfoService', return_value=service), \
                patch('activity_manager.page.CommandCreatorRenderer', side_effect=create_renderer), \
                patch('activity_manager.page.CommandListManager', side_effect=create_manager):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
            connect_button = next(item for item in page.descendants()
                                  if isinstance(item, ui.button) and item.text == 'Connect')
            connect = next(iter(connect_button._event_listeners.values())).handler.__closure__[0].cell_contents
            await connect()
            creator, manager = creators[0], managers[0]
            creator.command_select.set_value('with_database')
            creator.target_mode.set_value('Single')
            control = creator.controls['database']
            self.assertEqual(control.options, ['Local', 'Remote'])
            self.assertEqual(control._option_labels, {'Local': 'Local (db)', 'Remote': 'Remote (remote)'})

            creator.target_mode.set_value('Multiple')
            self.assertEqual(control.options, [])
            self.assertEqual(control.props.get('label'), 'Database Name Pattern')
            control.set_value('.*')
            creator._add_command()
            await manager._evaluation_task
            self.assertEqual(manager.commands[0].to_syntax(), 'WithDatabase(databasePattern=".*")')

            creator.target_mode.set_value('Single')
            creator.command_select.set_value('with_form')
            self.assertEqual(creator.controls['form'].options, ['Before', 'Shared'])
            creator.controls['form'].set_value('Shared')
            creator._add_command()
            await manager._evaluation_task

            creator.command_select.set_value('delete_field')
            self.assertEqual(creator.controls['field_code'].options, ['Alpha', 'Common', 'Z'])
            manager.select_iteration(1, 0)
            self.assertEqual(creator.controls['field_code'].options, ['Alpha', 'Common', 'Z'])
