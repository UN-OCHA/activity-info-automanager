import unittest
from unittest.mock import AsyncMock

from nicegui import ui
from nicegui.client import Client

from activity_manager.applier import Applier
from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands import ADD_MODIFY_FORM, AddModifyFormCommand
from activity_manager.planning import ActionKind
from activity_manager.resource_state import ResourceKey
from activity_manager.schema_resolution import resolve_command_schema
from test.test_add_form import CreationService
from test.test_multi_form import form


class AddModifyFormTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, text, service=None, *, initial=True):
        service = CreationService() if service is None else service

        async def resolve(schema):
            return await resolve_command_schema(service, schema, app.related_schemas, {}, {}, {},
                source_schema=app.workspace.baseline_forms.get(schema['id']))

        app = Applier(form('a', 'A') if initial else None, parse_command_text(text),
                      initial_database_id='db', form_service=service, resolve_schema=resolve)
        await app.evaluate()
        self.assertTrue(all(result.status != 'error' for result in app.executions),
                        [result.message for result in app.executions])
        return app

    def test_creator_and_text_accept_optional_folder_only(self):
        for text in ('AddModifyForm(name="New")', 'AddModifyForm(name="Existing", folder="/")'):
            command = parse_command_text(text)[0]
            self.assertEqual(parse_command_text(command.to_syntax()), [command])
        for argument in ('formPattern=".*"', 'currentForm=True', 'newName="Renamed"', 'recordLabelFieldId="CODE"'):
            with self.subTest(argument=argument), self.assertRaises(CommandTextError):
                parse_command_text(f'AddModifyForm(name="Existing", {argument})')

        authored = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((ADD_MODIFY_FORM,), on_add=authored.append)
        self.assertFalse(creator.target_mode.visible)
        creator.controls['form_name'].set_value('New')
        creator._add_command()
        self.assertEqual(authored, [AddModifyFormCommand('New')])

    async def test_new_form_defaults_to_root_and_pending_match_reuses_id(self):
        app = await self.apply('''AddModifyForm(name="New")
AddField(fieldCode="CODE", label="Code")
AddModifyForm(name="New")''', initial=False)
        added, _, matched = app.executions
        identity = added.schema['id']
        self.assertEqual(matched.schema['id'], identity)
        self.assertEqual(matched.schema['elements'][0]['code'], 'CODE')
        self.assertEqual([item.kind for item in app.candidate_plan.actions], [ActionKind.FORM_CREATE])
        entry = app.workspace.state.resources[ResourceKey.database('db')].payload.entries[identity]
        self.assertEqual(entry['parentId'], 'db')
        self.assertEqual(added.prepared_syntax, 'AddModifyForm(name="New")')

    async def test_omitted_folder_selects_existing_form_and_explicit_root_moves_it(self):
        service = CreationService()
        service.resources['db']['b']['parentId'] = 'north'
        app = await self.apply('AddModifyForm(name="B")', service)
        self.assertEqual(app.executions[0].schema['id'], 'b')
        self.assertFalse(app.candidate_plan.actions)
        self.assertEqual(app.workspace.state.resources[ResourceKey.database('db')].payload.entries['b']['parentId'],
                         'north')

        app = await self.apply('AddModifyForm(name="B", folder="/")', service)
        self.assertEqual(app.executions[0].schema['id'], 'b')
        self.assertEqual([item.kind for item in app.candidate_plan.actions], [ActionKind.DATABASE_RESOURCE_PATCH])
        self.assertEqual(app.workspace.state.resources[ResourceKey.database('db')].payload.entries['b']['parentId'],
                         'db')
        self.assertEqual(app.executions[0].prepared_syntax, 'AddModifyForm(name="B", folder="/")')

    async def test_matches_local_rename_and_keeps_selection_when_move_warns(self):
        app = await self.apply('''ModifyForm(currentForm=True, name="Renamed")
WithForm(form="B")
AddModifyForm(name="Renamed", folder="missing")
AddField(fieldCode="AFTER", label="After")''')
        result = app.executions[2]
        self.assertEqual(result.status, 'warning')
        self.assertEqual(result.schema['id'], 'a')
        self.assertEqual(app.executions[-1].schema['id'], 'a')
        self.assertFalse(app.workspace.latest['b']['elements'])
        self.assertTrue(all(action.kind == ActionKind.FORM_SCHEMA_UPDATE for action in app.candidate_plan.actions))

    async def test_ambiguity_ends_form_iteration_even_when_skipped(self):
        service = CreationService()
        service.schemas['duplicate'] = form('duplicate', 'A')
        service.resources['db']['duplicate'] = dict(id='duplicate', type='FORM', label='A', parentId='north')
        app = await self.apply('''WithForm(formPattern="A|B")
AddField(fieldCode="IN_SCOPE", label="In scope")
AddModifyForm(name="A", folder="/North")
AddField(fieldCode="AFTER", label="After")''', service)
        warning, following = app.executions[-2:]
        self.assertEqual(warning.status, 'warning')
        self.assertEqual(warning.diagnostics[0].code, 'ambiguous_form_name')
        self.assertFalse(warning.effect_ids)
        self.assertIsNone(app.scope)
        self.assertIsNone(app.workspace.active_schema)
        self.assertFalse(app.workspace.state.active_scope)
        self.assertFalse(following.form_iterations)
        self.assertEqual([field['code'] for field in app.workspace.latest['a']['elements']], ['IN_SCOPE'])
        self.assertEqual([field['code'] for field in app.workspace.latest['b']['elements']], ['IN_SCOPE'])
        self.assertEqual(following.diagnostics[0].code, 'missing_form')
        self.assertIsNone(following.schema)

    async def test_matches_different_case_and_excludes_subforms_and_other_databases(self):
        service = CreationService()
        service.resources['db']['child'] = dict(id='child', type='SUB_FORM', label='a', parentId='a')
        service.resources['remote'] = {'remote_form': dict(id='remote_form', type='FORM', label='a')}
        app = await self.apply('AddModifyForm(name="a", folder="North")', service)
        result = app.executions[0]
        self.assertEqual(result.status, 'applied')
        self.assertEqual([item.kind for item in app.candidate_plan.actions], [ActionKind.DATABASE_RESOURCE_PATCH])
        self.assertEqual(result.schema['id'], 'a')
        entry = app.workspace.state.resources[ResourceKey.database('db')].payload.entries[result.schema['id']]
        self.assertEqual(entry['parentId'], 'north')

    async def test_unavailable_lookup_does_not_create(self):
        service = CreationService()
        service.get_database_resources = AsyncMock(side_effect=ValueError('Unavailable'))
        app = await self.apply('AddModifyForm(name="New")', service, initial=False)
        self.assertEqual(app.executions[0].status, 'warning')
        self.assertFalse(app.candidate_plan.actions)

    async def test_failed_context_switches_clear_previous_iteration(self):
        for command in ('WithForm(form="Missing")',
                        'WithForm(formPattern="Missing")',
                        'ModifyForm(currentForm=False, form="Missing", name="New")',
                        'MoveForm(currentForm=False, form="Missing", folder="/")',
                        'DeleteForm(currentForm=False, form="Missing")'):
            with self.subTest(command=command):
                app = await self.apply(
                    f'WithForm(formPattern="[AB]")\n{command}\nAddField(fieldCode="X", label="X")')
                self.assertEqual(app.executions[1].status, 'warning')
                self.assertIsNone(app.workspace.active_schema)
                self.assertIsNone(app.scope)
                self.assertFalse(app.workspace.state.active_scope)
                self.assertEqual(app.workspace.state.active_database_id, 'db')
                self.assertEqual(app.executions[2].diagnostics[0].code, 'missing_form')
                self.assertFalse(app.candidate_plan.actions)

    async def test_move_warning_retains_the_new_iteration_and_current_move_preserves_it(self):
        app = await self.apply('''MoveForm(currentForm=False, formPattern="[AB]", folder="Missing")
MoveForm(currentForm=True, folder="Missing")
AddField(fieldCode="X", label="X")''')
        for result in app.executions[:2]:
            self.assertEqual([child.status for child in result.iterations], ['warning', 'warning'])
        self.assertEqual([field['code'] for field in app.workspace.latest['a']['elements']], ['X'])
        self.assertEqual([field['code'] for field in app.workspace.latest['b']['elements']], ['X'])
        self.assertEqual([target['identity'] for target in app.scope], ['a', 'b'])
