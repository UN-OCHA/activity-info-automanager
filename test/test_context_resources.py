"""Initial context resources, cumulative snapshots and nonfatal database reads."""
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from nicegui import ui
from nicegui.client import Client
from activity_manager.applier import Applier
from activity_manager.command_execution import ExecutionResult
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text
from activity_manager.listbox import DatabaseListbox
from activity_manager.page import connection_page
from activity_manager.resource_state import ResourceKey, LoadStatus
from test.test_move_form import FolderService
from test.test_multi_form import form


class ContextResourceTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, text='', *, database=None, initial=True, service=None):
        a, b = form('a', 'A'), form('b', 'B', 'remote')
        service = service or FolderService([a, b])
        engine = Applier(a if initial else None, parse_command_text(text), initial_database_id=database,
                         database_names={'db': 'Local', 'remote': 'Remote'}, form_service=service,
                         resolve_schema=AsyncMock(side_effect=lambda data: ExecutionResult(data)))
        return engine, service

    async def test_initial_form_and_database_are_loaded_before_commands(self):
        engine, service = self.engine('AddField(fieldCode="added", label="Added")')
        service.get_database_resources = AsyncMock(wraps=service.get_database_resources)
        await engine.evaluate()
        initial = engine.initial_display_state
        self.assertEqual(set(initial.tab_payloads()), {'a', 'database:db'})
        self.assertEqual(initial.form_payloads()['a']['elements'], [])
        self.assertEqual(engine.executions[0].schema['elements'][0]['code'], 'added')
        self.assertEqual(len(engine.candidate_plan.actions), 1)
        self.assertEqual({item.key for item in engine.candidate_plan.baselines}, {ResourceKey.form('a')})
        service.get_database_resources.assert_awaited_once_with('db')

    async def test_initial_database_without_form_or_commands(self):
        engine, _ = self.engine(database='db', initial=False)
        await engine.evaluate()
        self.assertEqual(set(engine.initial_display_state.tab_payloads()), {'database:db'})
        self.assertEqual(engine.candidate_plan.actions, ())
        self.assertIsNone(engine.workspace.state.active_form_key)

    async def test_switches_accumulate_resources_and_revisits_keep_local_moves(self):
        engine, service = self.engine('WithForm(form="Remote/B")\nMoveForm(currentForm=True, folder="North")\nWithForm(form="Local/A")\nWithForm(form="Remote/B")')
        service.get_database_resources = AsyncMock(wraps=service.get_database_resources)
        await engine.evaluate()
        first = engine.executions[0].display_state
        last = engine.executions[-1].display_state
        self.assertEqual(set(first.tab_payloads()), {'a', 'b', 'database:db', 'database:remote'})
        self.assertEqual(next(item for item in first.tab_payloads()['database:remote'] if item['id'] == 'b')['parentId'], 'remote')
        self.assertEqual(next(item for item in last.tab_payloads()['database:remote'] if item['id'] == 'b')['parentId'], 'north')
        self.assertEqual(service.get_database_resources.await_count, 2)
        self.assertEqual(len(engine.candidate_plan.actions), 1)

    async def test_missing_form_does_not_add_database_resources(self):
        engine, service = self.engine('WithForm(form="Remote/Missing")')
        service.get_database_resources = AsyncMock(wraps=service.get_database_resources)
        await engine.evaluate()
        self.assertEqual(engine.executions[0].status, 'warning')
        self.assertNotIn(ResourceKey.database('remote'), engine.workspace.state.resources)
        service.get_database_resources.assert_awaited_once_with('db')

    async def test_initial_database_failure_warns_and_does_not_block_schema_edit(self):
        engine, service = self.engine('AddField(fieldCode="added", label="Added")')
        service.get_database_resources = AsyncMock(side_effect=ValueError('Unavailable'))
        await engine.evaluate()
        self.assertEqual(engine.initial_diagnostics[0].severity, 'warning')
        self.assertEqual(engine.executions[0].status, 'applied')
        self.assertEqual(engine.workspace.state.resources[ResourceKey.database('db')].load_status, LoadStatus.UNAVAILABLE)
        self.assertNotIn(ResourceKey.database('db'), engine.workspace.state.baselines.entries)
        self.assertEqual(set(engine.initial_display_state.tab_payloads()), {'a'})

    async def test_new_database_failure_warns_but_selects_form_and_allows_schema_edit(self):
        engine, service = self.engine('WithForm(form="Remote/B")\nAddField(fieldCode="added", label="Added")')
        original = service.get_database_resources
        async def read(database):
            if database == 'remote':
                raise ValueError('Unavailable')
            return await original(database)
        service.get_database_resources = AsyncMock(side_effect=read)
        await engine.evaluate()
        self.assertEqual([e.status for e in engine.executions], ['warning', 'applied'])
        self.assertEqual(engine.executions[0].diagnostics[0].code, 'database_context_unavailable')
        self.assertEqual(engine.workspace.active_schema['id'], 'b')
        self.assertNotIn(ResourceKey.database('remote'), engine.workspace.state.baselines.entries)

    async def test_failed_initial_read_can_be_retried_without_replacing_schema(self):
        engine, service = self.engine('WithForm(form="A")')
        resources = await service.get_database_resources('db')
        service.get_database_resources = AsyncMock(side_effect=[ValueError('Temporary failure'), resources])
        await engine.evaluate()
        self.assertTrue(engine.initial_diagnostics)
        self.assertEqual(engine.executions[0].status, 'applied')
        self.assertEqual(engine.workspace.state.resources[ResourceKey.database('db')].load_status, LoadStatus.LOADED)
        self.assertNotIn('db', engine.workspace.issues)
        self.assertEqual(engine.initial_display_state.resources[-1].state.load_status, LoadStatus.UNAVAILABLE)

    async def test_database_only_initial_display_in_page_and_warning(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                service = FolderService([form('a', 'A')])
                service.get_databases = AsyncMock(return_value={'db': 'Local'})
                service.get_forms = AsyncMock(return_value={'a': 'A'})
                service.aclose = AsyncMock()
                if fail:
                    service.get_database_resources = AsyncMock(side_effect=ValueError('Unavailable'))
                managers = []
                def manager(**kwargs):
                    result = CommandListManager(**kwargs)
                    managers.append(result)
                    return result
                store = Mock()
                store.load.return_value = (None, '')
                with patch('activity_manager.page.AppStateStore', return_value=store), patch('activity_manager.page.ui.timer'), patch('activity_manager.page.ActivityInfoService', return_value=service), patch('activity_manager.page.CommandListManager', side_effect=manager):
                    with Client(ui.page('/')), ui.column() as page:
                        connection_page()
                        button = next(e for e in page.descendants() if isinstance(e, ui.button) and e.text == 'Connect')
                        connect = next(iter(button._event_listeners.values())).handler.__closure__[0].cell_contents
                        await connect()
                        database = next(e for e in page.descendants() if isinstance(e, DatabaseListbox))
                        await next(iter(database._event_listeners.values())).handler(SimpleNamespace(args='db'))
                        await managers[0]._evaluation_task
                        tabs = next(e for e in page.descendants() if isinstance(e, ui.tabs))
                        if fail:
                            self.assertTrue(any(isinstance(e, ui.label) and 'resources could not be loaded' in e.text for e in page.descendants()))
                        else:
                            self.assertEqual(tabs.value, 'database:db')
                            self.assertEqual(next(iter(tabs.default_slot.children))._props['label'], 'Local')

    async def test_move_requiring_unavailable_database_warns_without_effects(self):
        engine, service = self.engine('MoveForm(currentForm=True, folder="North")\nAddField(fieldCode="after", label="After")')
        service.get_database_resources = AsyncMock(side_effect=ValueError('Unavailable'))
        await engine.evaluate()
        self.assertEqual([e.status for e in engine.executions], ['warning', 'applied'])
        self.assertEqual(engine.executions[0].diagnostics[0].code, 'database_context_unavailable')
        self.assertEqual(len(engine.effects), 1)
        self.assertEqual(engine.workspace.active_schema['id'], 'a')
        self.assertNotIn(ResourceKey.database('db'), engine.workspace.state.baselines.entries)
