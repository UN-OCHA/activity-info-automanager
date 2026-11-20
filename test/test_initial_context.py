"""Optional initial context and atomic warning-only selection failures."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from activity_manager.applier import Applier
from activity_manager.command_execution import ExecutionResult
from activity_manager.command_parser import parse_command_text
from test.test_multi_form import MemoryService, form
from test.test_move_form import FolderService
from nicegui import ui
from nicegui.client import Client
from activity_manager.command_list import CommandListManager
from activity_manager.page import connection_page
from activity_manager.listbox import DatabaseListbox


class InitialContextTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, text, *, initial=None, database=None):
        service = MemoryService([form('a', 'A'), form('b', 'B', 'remote')])
        engine = Applier(initial, parse_command_text(text), initial_database_id=database,
                         form_service=service,
                         resolve_schema=AsyncMock(side_effect=lambda schema: ExecutionResult(schema)))
        await engine.evaluate()
        return engine, service

    async def test_no_context_warns_then_qualified_form_enables_later_edit(self):
        engine, _ = await self.evaluate('AddField(fieldCode="skip", label="Skip")\nWithForm(form="Remote/B")\nAddField(fieldCode="kept", label="Kept")')
        self.assertEqual([e.status for e in engine.executions], ['warning', 'applied', 'applied'])
        self.assertIsNone(engine.executions[0].schema)
        self.assertEqual(engine.workspace.state.active_database_id, 'remote')
        self.assertEqual([f['code'] for f in engine.workspace.active_schema['elements']], ['kept'])

    async def test_database_only_context_resolves_unqualified_form(self):
        engine, _ = await self.evaluate('WithForm(form="A")', database='db')
        self.assertEqual(engine.executions[0].status, 'applied')
        self.assertEqual(engine.workspace.active_schema['id'], 'a')

    async def test_failed_qualified_selection_leaves_database_only_context(self):
        engine, _ = await self.evaluate('WithForm(form="Remote/Missing")\nWithForm(form="A")', database='db')
        self.assertEqual([e.status for e in engine.executions], ['warning', 'applied'])
        self.assertIsNone(engine.executions[0].schema)
        self.assertEqual(engine.workspace.state.active_database_id, 'db')

    async def test_unqualified_selection_without_database_never_reads_forms(self):
        engine, service = await self.evaluate('WithForm(form="A")')
        self.assertEqual(engine.executions[0].status, 'warning')
        self.assertEqual(service.reads, [])
        self.assertIsNone(engine.workspace.state.active_database_id)
        self.assertIsNone(engine.workspace.state.active_form_key)
        self.assertEqual(engine.candidate_plan.actions, ())

    async def test_failed_selections_preserve_previous_form_and_do_not_modify_it(self):
        for selector in ('Missing', 'Remote/Missing', 'MissingDatabase/B'):
            with self.subTest(selector=selector):
                engine, _ = await self.evaluate(f'ModifyForm(currentForm=False, form="{selector}", name="Rejected")\nAddField(fieldCode="kept", label="Kept")', initial=form('a', 'A'))
                self.assertEqual([e.status for e in engine.executions], ['warning', 'applied'])
                self.assertEqual(engine.workspace.active_schema['label'], 'A')
                self.assertEqual(engine.workspace.state.active_database_id, 'db')
                self.assertEqual(len(engine.effects), 1)

    async def test_literal_variable_can_establish_selector_without_initial_context(self):
        engine, _ = await self.evaluate('SetVariable(name="target", method="literal", value="Remote/B")\nWithForm(form="${vars.target}")')
        self.assertEqual([e.status for e in engine.executions], ['applied', 'applied'])
        self.assertEqual(engine.workspace.active_schema['id'], 'b')

    async def test_form_pattern_without_database_warns_and_allows_recovery(self):
        engine, _ = await self.evaluate('WithForm(formPattern=".*")\nWithForm(form="Local/A")')
        self.assertEqual([e.status for e in engine.executions], ['warning', 'applied'])

    async def test_current_form_move_and_field_iteration_require_form(self):
        engine, _ = await self.evaluate('MoveForm(currentForm=True, folder="/")\nModifyForm(currentForm=True, name="New")\nDeleteField(fieldPattern=".*")')
        self.assertEqual([e.status for e in engine.executions], ['warning', 'warning', 'warning'])
        self.assertIsNone(engine.workspace.state.active_form_key)

    def test_local_literal_variables_work_without_schema(self):
        engine = Applier(commands=parse_command_text('SetVariable(name="x", method="literal", value="v")\nAddField(fieldCode="a", label="A")'))
        engine.evaluate_local()
        self.assertEqual([e.status for e in engine.executions], ['applied', 'warning'])
        self.assertEqual(engine.variables['vars.x'], 'v')

    async def test_qualified_modify_and_move_can_establish_context(self):
        for text in ('ModifyForm(currentForm=False, form="Local/A", recordLabelFieldId="")',
                     'MoveForm(currentForm=False, form="Local/A", folder="/North")'):
            with self.subTest(text=text):
                service = FolderService([form('a', 'A')])
                engine = Applier(commands=parse_command_text(text), form_service=service,
                                 resolve_schema=AsyncMock(side_effect=lambda schema: ExecutionResult(schema)))
                await engine.evaluate()
                self.assertEqual(engine.executions[0].status, 'applied', engine.executions[0].message)
                self.assertEqual(engine.workspace.active_schema['id'], 'a')
                self.assertEqual(engine.workspace.state.active_database_id, 'db')

    async def test_ui_can_confirm_execution_without_initial_selection(self):
        service = FolderService([form('a', 'A')])
        service.get_databases = AsyncMock(return_value={'db': 'Local'})
        service.aclose = AsyncMock()
        managers = []
        class Manager(CommandListManager):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                managers.append(self)
        store = Mock()
        store.load.return_value = (None, '')
        with patch('activity_manager.page.AppStateStore', return_value=store), patch('activity_manager.page.ActivityInfoService', return_value=service), patch('activity_manager.page.CommandListManager', Manager), patch('activity_manager.page.ui.timer'):
            with Client(ui.page('/')) as client, ui.column() as page:
                connection_page()
                connect_button = next(e for e in page.descendants() if isinstance(e, ui.button) and e.text == 'Connect')
                connect = next(iter(connect_button._event_listeners.values())).handler.__closure__[0].cell_contents
                await connect()
                manager = managers[0]
                manager.commands = parse_command_text('WithForm(form="Local/A")\nAddField(fieldCode="added", label="Added")')
                manager._evaluate_commands()
                await manager._evaluation_task
                self.assertEqual([e.status for e in manager.executions], ['applied', 'applied'])
                self.assertTrue(all(e.value is None for e in page.descendants() if isinstance(e, DatabaseListbox)))
                manager.on_execute()
                self.assertTrue(any(isinstance(e, ui.label) and 'Execute these updates?' in e.text for e in client.elements.values()))
                self.assertEqual(service.writes, [])

    async def test_qualified_lookup_variable_does_not_require_or_change_context(self):
        lookup = AsyncMock(return_value=[{'value': 'Kept'}])
        engine = Applier(commands=parse_command_text(
            'SetVariable(name="target", method="literal", value="Local/A")\n'
            'SetVariable(name="value", method="lookup", referencedForm="${vars.target}", filter="TRUE", returnFormula="x")'),
            resolve_schema=AsyncMock(), lookup_variable=lookup)
        await engine.evaluate()
        self.assertEqual([e.status for e in engine.executions], ['applied', 'applied'])
        self.assertEqual(engine.variables['vars.value'], 'Kept')
        self.assertIsNone(engine.workspace.state.active_database_id)
        self.assertIsNone(engine.workspace.state.active_form_key)
        self.assertEqual(lookup.await_args.args[0].referenced_form, 'Local/A')
