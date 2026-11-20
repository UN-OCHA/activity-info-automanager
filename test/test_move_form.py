import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from nicegui import ui
from nicegui.client import Client

from activity_manager.api import ActivityInfoService, DEFAULT_BASE_URL
from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import parse_command_text, CommandTextError
from activity_manager.commands.move_form import MOVE_FORM, MoveFormCommand
from activity_manager.form_folders import resolve_folder, folder_paths, folder_choices, form_location
from activity_manager.schema_resolution import CachedResolutionService, resolve_command_schema
from test.form_command_runtime import execute as execute_runtime, locations, install
from activity_manager.confirmed_plan import ConfirmationAttempt, AttemptBinding
from activity_manager.resource_state import ResourceKind, ResourceKey
from activity_manager.listbox import DatabaseListbox
from test import test_multi_form as fixtures


class FolderService(fixtures.MemoryService):
    def __init__(self, schemas):
        super().__init__(schemas)
        self.resources = {}
        self.moves, self.timeline = [], []
        self.reject_move = None
        self.lie_move = None
        self.error_after_move = None
        for schema in schemas:
            database = schema['databaseId']
            resources = self.resources.setdefault(database, {
                'north': dict(id='north', type='FOLDER', label='North', parentId=database),
                'archive': dict(id='archive', type='FOLDER', label='Archive', parentId='north'),
                'south': dict(id='south', type='FOLDER', label='South', parentId=database),
                'shared1': dict(id='shared1', type='FOLDER', label='Shared', parentId='north'),
                'shared2': dict(id='shared2', type='FOLDER', label='Shared', parentId='south'),
            })
            resources[schema['id']] = dict(id=schema['id'], label=schema['label'],
                type='SUB_FORM' if schema.get('parentFormId') else 'FORM',
                parentId=schema.get('parentFormId') or database)

    async def get_database_resources(self, database):
        return deepcopy(self.resources[database])

    async def move_form(self, database, identity, parent):
        self.moves.append((database, identity, parent))
        self.timeline.append(('move', identity, parent))
        if len(self.moves) == self.reject_move:
            raise ValueError('Move rejected')
        if len(self.moves) != self.lie_move:
            self.resources[database][identity]['parentId'] = parent
        if len(self.moves) == self.error_after_move:
            raise ValueError('Response lost after move')

    async def put_form_schema(self, identity, schema):
        self.timeline.append(('schema', identity))
        await super().put_form_schema(identity, schema)
        self.resources[schema['databaseId']][identity]['label'] = schema['label']


class FolderTests(unittest.TestCase):
    def test_resolve_root_ids_paths_unique_names_and_ambiguity(self):
        resources = FolderService([fixtures.form('a', 'A')]).resources['db']
        for value, expected in [('/', 'db'), ('db', 'db'), ('north', 'north'), ('North', 'north'),
                                ('North/Archive', 'archive'), ('Archive', 'archive'), ('North/Shared', 'shared1')]:
            with self.subTest(value=value):
                self.assertEqual(resolve_folder('db', resources, value)[0], expected)
        for value in ('Shared', 'Missing', 'a'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_folder('db', resources, value)
        resources['north']['parentId'] = 'archive'
        with self.assertRaises(ValueError):
            folder_paths('db', resources)

    def test_absolute_paths_disambiguate_root_folder_from_nested_name(self):
        resources = FolderService([fixtures.form('a', 'A')]).resources['db']
        resources['root-archive'] = dict(id='root-archive', type='FOLDER', label='Archive', parentId='db')
        with self.assertRaises(ValueError):
            resolve_folder('db', resources, 'Archive')
        self.assertEqual(resolve_folder('db', resources, '/Archive')[0], 'root-archive')
        for choice in folder_choices('db', resources):
            resolve_folder('db', resources, choice)

    def test_command_parser_and_creator(self):
        for text in ('MoveForm(currentForm=False, form="A", folder="/")',
                     'moveform(currentForm=False, FORMPATTERN="(?P<region>.*)", FOLDER="${form.region}/Archive")'):
            command = parse_command_text(text)[0]
            self.assertEqual(parse_command_text(command.to_syntax()), [command])
        for args in ('form="A"', 'folder="North"', 'form="A", folder=" "',
                     'form="A", formPattern=".*", folder="North"', 'formPattern="[", folder="North"'):
            with self.subTest(args=args), self.assertRaises(CommandTextError):
                parse_command_text(f'MoveForm(currentForm=False, {args})')
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((MOVE_FORM,), on_add=added.append)
            self.assertEqual(creator.editors[0].definition.key, 'current_form')
            self.assertFalse(creator.activations['current_form'].visible)
            self.assertFalse(creator.parameter_containers['form'].visible)
            creator.controls['folder'].set_value('North')
            creator._add_command()
            self.assertEqual(added[-1], MoveFormCommand(current_form=True, folder='North'))
            creator.controls['current_form'].set_value(False)
            self.assertTrue(creator.parameter_containers['form'].visible)
            creator.controls['form'].set_value('A')
            creator.set_suggestions('folder_paths', ['/', 'North', 'North/Archive'])
            creator.controls['folder'].set_value('North')
            creator._add_command()
            self.assertEqual(added[-1], MoveFormCommand(current_form=False, form='A', folder='North'))
            creator.target_mode.set_value('Multiple')
            creator.controls['form'].set_value('.*')
            creator._add_command()
            self.assertEqual(added[-1].form_pattern, '.*')
            self.assertIn('North/Archive', creator.controls['folder'].options)
            creator.controls['current_form'].set_value(True)
            self.assertFalse(creator.target_mode.visible)
            self.assertFalse(creator.parameter_containers['form'].visible)
            creator._add_command()
            self.assertTrue(added[-1].current_form)
            self.assertIsNone(added[-1].form_pattern)

    def test_current_form_is_required_boolean_and_excludes_selectors(self):
        text = 'MoveForm(currentForm=True, folder="North")'
        command = parse_command_text(text)[0]
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        self.assertTrue(command.to_syntax().startswith('MoveForm(currentForm=True,'))
        for args in ('folder="North"', 'currentForm="true", folder="North"',
                     'currentForm=1, folder="North"', 'currentForm=False, folder="North"',
                     'currentForm=True, form="A", folder="North"',
                     'currentForm=True, formPattern=".*", folder="North"'):
            with self.subTest(args=args), self.assertRaises(CommandTextError):
                parse_command_text(f'MoveForm({args})')


class MoveFormTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, text, schemas=None):
        if schemas is None:
            schemas = [fixtures.form('a', 'A'), fixtures.form('b', 'B')]
        with patch.object(fixtures, 'MemoryService', FolderService):
            return await fixtures.PreviewTests.evaluate(self, schemas, text)

    async def execute(self, manager, service, choice='rollback_all'):
        return await execute_runtime(manager, service, choice)

    async def test_current_form_follows_command_position_and_preserves_schema(self):
        manager, service = await self.evaluate('''
WithForm(form="B")
MoveForm(currentForm=True, folder="North")
WithForm(form="A")
MoveForm(currentForm=True, folder="South")
''')
        self.assertTrue(all(item.status == 'applied' for item in manager.executions))
        self.assertEqual(locations(manager.executions[1])['b']['parentId'], 'north')
        self.assertEqual(locations(manager.executions[3])['a']['parentId'], 'south')
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual(service.moves, [('db', 'b', 'north'), ('db', 'a', 'south')])
        self.assertEqual(service.writes, [])

    async def test_current_form_uses_initial_form_and_retains_variables(self):
        manager, service = await self.evaluate('''
SetVariable(name="destination", method="literal", value="North")
MoveForm(currentForm=True, folder="${vars.destination}")
AddField(fieldCode="new", label="${vars.destination}")
''')
        self.assertTrue(all(item.status == 'applied' for item in manager.executions),
                        [item.message for item in manager.executions])
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual(service.moves, [('db', 'a', 'north')])
        self.assertEqual(service.schemas['a']['elements'][0]['label'], 'North')

    async def test_current_form_preserves_multiple_form_scope_and_captures(self):
        manager, service = await self.evaluate('''
WithForm(formPattern="(?P<side>North|South)")
MoveForm(currentForm=True, folder="${form.side}/Shared")
AddField(fieldCode="new", label="${form.name}")
''', [fixtures.form('a', 'North'), fixtures.form('b', 'South')])
        self.assertTrue(all(item.status == 'applied' for item in manager.executions),
                        [item.message for item in manager.executions])
        self.assertEqual(len(manager.executions[1].iterations), 2)
        self.assertEqual(len(manager.executions[2].iterations), 2)
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual(service.moves, [('db', 'a', 'shared1'), ('db', 'b', 'shared2')])

    async def test_single_move_switches_context_and_keeps_schema_clean(self):
        manager, service = await self.evaluate('''
MoveForm(currentForm=False, form="B", folder="North/Archive")
AddField(fieldCode="new", label="New")
''')
        self.assertEqual([e.status for e in manager.executions], ['applied', 'applied'])
        self.assertEqual(manager.workspace.active_schema['id'], 'b')
        self.assertEqual(locations(manager.executions[0])['b']['parentId'], 'archive')
        self.assertEqual(manager.executions[0].schema['elements'], [])
        self.assertEqual(manager.executions[1].schema['elements'][0]['code'], 'new')
        self.assertEqual(service.moves, [])
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual(service.timeline, [('move', 'b', 'archive'), ('schema', 'b')])
        self.assertEqual(form_location('db', service.resources['db'], 'b')['path'], 'North/Archive')
        self.assertFalse(any(key.startswith('__') for key in service.writes[0][1]))
        self.assertFalse(any(key.startswith('__') for key in outcome.actual_schemas['b']))

    async def test_multiple_moves_expand_and_establish_scope_with_capture_destinations(self):
        manager, service = await self.evaluate('''
MoveForm(currentForm=False, formPattern="(?P<side>North|South)", folder="${form.side}/Shared")
AddField(fieldCode="new", label="${form.name}")
''', [fixtures.form('a', 'North'), fixtures.form('b', 'South')])
        self.assertEqual([e.status for e in manager.executions], ['applied', 'applied'],
                         [e.message for e in manager.executions])
        self.assertEqual([e.application_label for e in manager.executions[0].iterations], ['North', 'South'])
        self.assertEqual(locations(manager.executions[0].iterations[0])['a']['parentId'], 'shared1')
        self.assertEqual(locations(manager.executions[0].iterations[0])['b']['parentId'], 'db')
        self.assertEqual(locations(manager.executions[0].iterations[1])['b']['parentId'], 'shared2')
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual(service.moves, [('db', 'a', 'shared1'), ('db', 'b', 'shared2')])

    async def test_revisits_preserve_locations_and_return_to_root(self):
        manager, service = await self.evaluate('''
MoveForm(currentForm=False, form="A", folder="North")
WithForm(form="B")
WithForm(form="A")
MoveForm(currentForm=False, form="A", folder="/")
''')
        self.assertEqual(locations(manager.executions[0])['a']['path'], 'North')
        self.assertEqual(locations(manager.executions[2])['a']['path'], 'North')
        self.assertEqual(locations(manager.executions[3])['a']['path'], '/')
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual(service.moves, [('db', 'a', 'north'), ('db', 'a', 'db')])

    async def test_root_noop_does_not_write(self):
        manager, service = await self.evaluate('MoveForm(currentForm=False, form="A", folder="/")')
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'success', outcome.messages)
        self.assertEqual(service.timeline, [])

    async def test_ambiguous_folder_and_subform_stop_without_moves(self):
        for target, schemas in [('Shared', None), ('North', [dict(fixtures.form('a', 'A'), parentFormId='parent')])]:
            manager, service = await self.evaluate(f'MoveForm(currentForm=False, formPattern="A|B", folder="{target}")\nAddField(fieldCode="later", label="Later")', schemas)
            self.assertEqual(manager.executions[0].status, 'warning' if target == 'Shared' else 'error')
            self.assertEqual(manager.executions[1].status, 'applied' if target == 'Shared' else 'not_executed')
            self.assertEqual({key for key in manager.workspace.state.baselines.entries if key.kind == ResourceKind.DATABASE_RESOURCES}, {ResourceKey.database('db')})
            self.assertEqual(service.moves, [])

    async def test_remote_single_target_and_scope_replacement(self):
        manager, service = await self.evaluate('''
WithForm(formPattern="A")
SetVariable(name="local", method="literal", value="Inside")
MoveForm(currentForm=False, form="Remote/B", folder="North")
AddField(fieldCode="bad", label="${vars.local}")
''', [fixtures.form('a', 'A'), fixtures.form('b', 'B', 'remote')])
        self.assertEqual(manager.executions[2].status, 'applied')
        self.assertEqual(locations(manager.executions[2])['b']['databaseId'], 'remote')
        self.assertEqual(manager.executions[3].status, 'error')
        self.assertEqual(service.moves, [])

    async def test_external_move_is_stale_before_any_writes(self):
        manager, service = await self.evaluate('MoveForm(currentForm=False, form="A", folder="North")')
        service.resources['db']['a']['parentId'] = 'south'
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'preflight_failed', outcome.messages)
        self.assertEqual(service.timeline, [])

    async def test_removed_destination_stops_with_no_move(self):
        manager, service = await self.evaluate('MoveForm(currentForm=False, form="A", folder="North")')
        # Remove the whole subtree so the remaining hierarchy is valid.
        for identity in ('north', 'archive', 'shared1'):
            del service.resources['db'][identity]
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'preflight_failed', outcome.messages)
        self.assertEqual(service.moves, [])

    async def test_mixed_failure_rolls_back_schema_and_move_in_reverse_order(self):
        manager, service = await self.evaluate('''
MoveForm(currentForm=False, formPattern="A|B", folder="North")
ModifyField(fieldPattern="missing", fieldType="quantity", label="Ignored")
AddField(fieldCode="new", label="New")
''')
        original = service.put_form_schema
        async def lose_response(identity, schema):
            await original(identity, schema)
            if len(service.writes) == 2:
                raise ValueError('Response lost after committed schema')
        service.put_form_schema = lose_response
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'rolled_back_all', outcome.messages)
        self.assertEqual(service.resources['db']['a']['parentId'], 'db')
        self.assertEqual(service.resources['db']['b']['parentId'], 'db')
        self.assertEqual(service.schemas['a']['elements'], [])
        self.assertEqual(service.schemas['b']['elements'], [])
        self.assertEqual(service.timeline, [('move', 'a', 'north'), ('move', 'b', 'north'),
            ('schema', 'a'), ('schema', 'b'), ('schema', 'b'), ('schema', 'a'),
            ('move', 'a', 'db'), ('move', 'b', 'db')])

    async def test_rollback_last_restores_move_before_subsequent_schema_edit_starts(self):
        manager, service = await self.evaluate('''
MoveForm(currentForm=False, form="A", folder="North")
AddField(fieldCode="new", label="New")
''')
        service.error_after_move = 1
        outcome = await self.execute(manager, service, 'rollback_last')
        self.assertEqual(outcome.status, 'rolled_back_last', outcome.messages)
        self.assertEqual(service.resources['db']['a']['parentId'], 'db')
        # Stage 6: move executes before the later schema edit, which never starts.
        self.assertEqual(service.schemas['a']['elements'], [])

    async def test_move_verification_failure_and_leave(self):
        manager, service = await self.evaluate('MoveForm(currentForm=False, form="A", folder="North")')
        service.lie_move = 1
        outcome = await self.execute(manager, service, 'leave')
        self.assertEqual(outcome.status, 'left', outcome.messages)
        self.assertEqual(service.resources['db']['a']['parentId'], 'db')

    async def test_concurrent_move_of_later_form_is_preserved_during_rollback(self):
        manager, service = await self.evaluate('MoveForm(currentForm=False, form="A", folder="North")\nAddField(fieldCode="new", label="New")\nMoveForm(currentForm=False, form="B", folder="North")')
        original = service.move_form
        async def move(database, identity, parent):
            await original(database, identity, parent)
            if len(service.moves) == 1:
                service.resources['db']['b']['parentId'] = 'south'
        service.move_form = move
        outcome = await self.execute(manager, service)
        self.assertEqual(outcome.status, 'rolled_back_all', outcome.messages)
        self.assertEqual(service.resources['db']['a']['parentId'], 'db')
        self.assertEqual(service.resources['db']['b']['parentId'], 'south')

    async def test_zero_match_scope_skips_moves_and_following_applications(self):
        manager, service = await self.evaluate('''
MoveForm(currentForm=False, formPattern="Missing", folder="North")
AddField(fieldCode="skip", label="Skip")
WithForm(form="A")
''')
        self.assertEqual([e.status for e in manager.executions], ['warning', 'warning', 'applied'])
        self.assertEqual({key for key in manager.workspace.state.baselines.entries if key.kind == ResourceKind.DATABASE_RESOURCES}, {ResourceKey.database('db')})
        self.assertEqual(service.timeline, [])


class MoveFormPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_executes_folder_updates_and_preserves_result_report(self):
        managers = []
        service = FolderService([fixtures.form('a', 'A')])
        install(service)
        service.get_databases = AsyncMock(return_value={'db': 'Local (db)'})
        service.get_forms = AsyncMock(return_value={'a': 'A (a)'})
        service.get_form_tree = AsyncMock(return_value={'a': service.schemas['a']})
        service.aclose = AsyncMock()
        def manager(**kwargs):
            item = CommandListManager(**kwargs)
            managers.append(item)
            return item
        with patch('activity_manager.page.ActivityInfoService', return_value=service), \
                patch('activity_manager.page.CommandListManager', side_effect=manager):
            with Client(ui.page('/')), ui.column() as page:
                fixtures.connection_page()
            connect_button = next(item for item in page.descendants() if isinstance(item, ui.button) and item.text == 'Connect')
            connect = next(iter(connect_button._event_listeners.values())).handler.__closure__[0].cell_contents
            await connect()
            databases, forms = [item for item in page.descendants() if isinstance(item, DatabaseListbox)]
            await next(iter(databases._event_listeners.values())).handler(SimpleNamespace(args='db'))
            await next(iter(forms._event_listeners.values())).handler(SimpleNamespace(args='a'))
            commands = managers[0]
            commands.commands = parse_command_text('MoveForm(currentForm=False, form="A", folder="North")')
            commands._evaluate_commands()
            await commands._evaluation_task
            commands.on_execute()
            self.assertTrue(any(isinstance(item, ui.label) and 'Move forms — A → North' in item.text
                                for item in page.client.elements.values()))
            ok = [item for item in page.client.elements.values() if isinstance(item, ui.button) and item.text == 'Execute'][-1]
            execute = next(iter(ok._event_listeners.values())).handler.__closure__[0].cell_contents
            await execute()
            await commands._evaluation_task
            self.assertEqual(service.moves, [('db', 'a', 'north')])
            self.assertEqual(commands.execution_report.status, 'success')
            tabs = next(item for item in page.descendants() if isinstance(item, ui.tabs))
            self.assertEqual(tabs.value, 'database:db')
            code = next(item for item in page.descendants() if isinstance(item, ui.code))
            shown = json.loads(code.content)
            self.assertIsInstance(shown, list)
            self.assertEqual(next(entry for entry in shown if entry['id'] == 'a')['parentId'], 'north')
            self.assertEqual(next(tab for tab in tabs.default_slot.children
                                  if tab._props['name'] == 'database:db')._props['label'], 'Local')
            toggle = next(item for item in page.descendants() if isinstance(item, ui.toggle)
                          and 'names' in item.options)
            toggle.set_value('names')
            self.assertEqual(next(entry for entry in json.loads(code.content) if entry['id'] == 'a')['parentId'], 'North')
            toggle.set_value('ids')
            self.assertEqual(json.loads(code.content), shown)

    async def make_page(self):
        managers, creators = [], []
        def manager(**kwargs):
            item = CommandListManager(**kwargs)
            managers.append(item)
            return item
        def creator(*args, **kwargs):
            item = CommandCreatorRenderer(*args, **kwargs)
            creators.append(item)
            return item
        with patch('activity_manager.page.CommandListManager', side_effect=manager), \
                patch('activity_manager.page.CommandCreatorRenderer', side_effect=creator):
            with Client(ui.page('/')), ui.column() as page:
                fixtures.connection_page()
        manager, creator = managers[0], creators[0]
        service = FolderService([fixtures.form('a', 'A'), fixtures.form('b', 'B'), fixtures.form('c', 'C', 'remote')])
        service.resources['remote']['north']['label'] = 'Remote North'
        manager.form_service = CachedResolutionService(service, manager.resolution_cache)
        manager.database_names = {'db': 'Local', 'remote': 'Remote'}
        async def resolve(data):
            return await resolve_command_schema(service, data, manager.related_schemas, {}, {}, {},
                                                source_schema=manager.workspace.baseline_forms[data['id']])
        manager.resolve_schema = resolve
        manager.set_schema(service.schemas['a'])
        creator.command_select.set_value('move_form')
        creator.controls['current_form'].set_value(False)
        await asyncio.sleep(0)
        return manager, creator, service, page

    @staticmethod
    def set_target(creator, name):
        creator._updating = True
        creator.controls['form'].set_value(name)
        creator._updating = False

    async def test_folder_selector_uses_qualified_database_or_active_database(self):
        manager, creator, service, _ = await self.make_page()
        await creator.on_parameter_change(creator)
        self.assertIn('/North/Archive', creator.controls['folder'].options)
        self.set_target(creator, 'Remote/C')
        await creator.on_parameter_change(creator)
        self.assertIn('/Remote North/Archive', creator.controls['folder'].options)
        self.assertNotIn('/North/Archive', creator.controls['folder'].options)
        creator.target_mode.set_value('Multiple')
        await creator.on_parameter_change(creator)
        self.assertIn('/North/Archive', creator.controls['folder'].options)
        self.assertEqual([s['id'] for s in manager.workspace.form_snapshots], ['a'])
        self.assertEqual(service.timeline, [])

    async def test_current_form_folder_choices_use_active_database_after_remote_selection(self):
        manager, creator, service, _ = await self.make_page()
        self.set_target(creator, 'Remote/C')
        await creator.on_parameter_change(creator)
        self.assertIn('/Remote North/Archive', creator.controls['folder'].options)
        creator.controls['current_form'].set_value(True)
        await creator.on_parameter_change(creator)
        self.assertFalse(creator.parameter_containers['form'].visible)
        self.assertIn('/North/Archive', creator.controls['folder'].options)
        self.assertNotIn('/Remote North/Archive', creator.controls['folder'].options)
        self.assertEqual(manager.workspace.active_schema['id'], 'a')
        self.assertEqual(service.moves, [])

    async def test_folder_preview_tracks_selected_form_and_leaf_history(self):
        manager, _, service, page = await self.make_page()
        manager.commands = parse_command_text('''
MoveForm(currentForm=False, formPattern="A|B", folder="North")
AddField(fieldCode="new", label="New")
ModifyField(fieldPattern="new", fieldType="FREE_TEXT", fieldVariant="text", label="Modified")
MoveForm(currentForm=False, form="A", folder="South")
''')
        manager._evaluate_commands()
        await manager._evaluation_task
        self.assertTrue(all(e.status == 'applied' for e in manager.executions), [e.message for e in manager.executions])
        tabs = next(item for item in page.descendants() if isinstance(item, ui.tabs))
        code = next(item for item in page.descendants() if isinstance(item, ui.code))
        manager.select_iteration(0, 0)
        self.assertEqual(tabs.value, 'database:db')
        self.assertEqual(locations(manager.executions[0].iterations[0])['a']['parentId'], 'north')
        self.assertEqual(locations(manager.executions[0].iterations[0])['b']['parentId'], 'db')
        manager.select_iteration(2, 0)
        self.assertEqual(tabs.value, 'a')
        tabs.set_value('database:db')
        shown = json.loads(code.content)
        self.assertIsInstance(shown, list)
        self.assertEqual(next(entry for entry in shown if entry['id'] == 'a')['parentId'], 'north')
        self.assertEqual(next(tab for tab in tabs.default_slot.children
                              if tab._props['name'] == 'database:db')._props['label'], 'Local')
        toggle = next(item for item in page.descendants() if isinstance(item, ui.toggle)
                      and 'names' in item.options)
        toggle.set_value('names')
        self.assertEqual(next(entry for entry in json.loads(code.content) if entry['id'] == 'a')['parentId'], 'North')
        toggle.set_value('ids')
        self.assertEqual(json.loads(code.content), shown)
        tabs.set_value('a')
        self.assertEqual(json.loads(code.content)['label'], 'A')
        manager.select_command(3)
        self.assertEqual(tabs.value, 'database:db')
        self.assertEqual(locations(manager.executions[3])['a']['parentId'], 'south')
        toggle.set_value('names')
        self.assertEqual(next(entry for entry in json.loads(code.content) if entry['id'] == 'a')['parentId'], 'South')
        manager.select_iteration(2, 1)
        self.assertEqual(locations(manager.executions[2].iterations[1])['a']['parentId'], 'north')
        tabs.set_value('database:db')
        self.assertEqual(next(entry for entry in json.loads(code.content) if entry['id'] == 'a')['parentId'], 'North')
        self.assertEqual(service.moves, [])

    async def test_late_folder_response_does_not_replace_new_database_choices(self):
        manager, creator, service, _ = await self.make_page()
        started, release = asyncio.Event(), asyncio.Event()
        original = service.get_database_resources
        async def delayed(database):
            if database == 'remote':
                started.set()
                await release.wait()
            return await original(database)
        service.get_database_resources = delayed
        manager.workspace.state.load_form(service.schemas['c'])
        manager.workspace.state.activate(manager.workspace.key_for(service.schemas['c']))
        previous = asyncio.create_task(creator.on_parameter_change(creator))
        await asyncio.wait_for(started.wait(), timeout=2)
        manager.workspace.state.activate(manager.workspace.key_for(service.schemas['a']))
        await creator.on_parameter_change(creator)
        release.set()
        await previous
        self.assertIn('/North/Archive', creator.controls['folder'].options)
        self.assertNotIn('/Remote North/Archive', creator.controls['folder'].options)


class MoveFormApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_database_update_sends_only_resource_parent(self):
        tree = dict(databaseId='db', userId='user', version='1', label='Database', ownerRef=None,
            suspended=False, role={'id': 'owner'}, roles=[], storage='', publishedTemplate=False,
            securityCategories=[], billingAccountId=1, billingPlan='',
            resources=[dict(id='a', type='FORM', parentId='north', label='A'),
                       dict(id='north', type='FOLDER', parentId='db', label='North')])
        calls = []
        def handle(request):
            calls.append(request)
            self.assertEqual(str(request.url), DEFAULT_BASE_URL + '/databases/db')
            if request.method == 'POST':
                self.assertEqual(json.loads(request.content), {'resourceUpdates': [{'id': 'a', 'parentId': 'north'}]})
            return httpx.Response(200, json=tree)
        original = httpx.AsyncClient
        with patch('activity_info_api_client.client.httpx.AsyncClient',
                   side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs)):
            service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
            try:
                resources = await service.get_database_resources('db')
                self.assertEqual(resources['north']['type'], 'FOLDER')
                await service.write_resources_evidence('db', {'resourceUpdates': [{'id': 'a', 'parentId': 'north'}]})
            finally:
                await service.aclose()
        self.assertEqual([call.method for call in calls], ['GET', 'POST'])


class FolderSuggestionContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_destination_options_follow_active_database_without_requiring_a_form(self):
        from unittest.mock import Mock
        from activity_manager.command_execution import ExecutionResult
        from activity_manager.page import connection_page
        service = AsyncMock(spec=ActivityInfoService)
        service.get_databases.return_value = {'db': 'Local', 'remote': 'Remote'}
        service.get_database_names.return_value = {'db': 'Local', 'remote': 'Remote'}
        service.get_forms.return_value = {'a': 'A'}
        service.get_form_names.return_value = {'b': 'B'}
        service.get_form_schema.return_value = fixtures.form('b', 'B', 'remote')
        service.get_database_resources.side_effect = lambda db: {
            'folder': dict(id='folder', type='FOLDER', label='LocalFolder' if db == 'db' else 'RemoteFolder', parentId=db)}
        creators, managers = [], []
        def create_renderer(*args, **kwargs):
            creator = CommandCreatorRenderer(*args, **kwargs)
            creators.append(creator)
            return creator
        def create_manager(*args, **kwargs):
            manager = CommandListManager(*args, **kwargs)
            managers.append(manager)
            return manager
        store = Mock()
        store.load.return_value = (None, '')
        with patch('activity_manager.page.ActivityInfoService', return_value=service), \
             patch('activity_manager.page.AppStateStore', return_value=store), \
             patch('activity_manager.page.ui.timer'), \
             patch('activity_manager.page.CommandCreatorRenderer', side_effect=create_renderer), \
             patch('activity_manager.page.CommandListManager', side_effect=create_manager), \
             patch('activity_manager.command_creator.events.handle_event'):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
                button = next(e for e in page.descendants() if isinstance(e, ui.button) and e.text == 'Connect')
                connect = next(iter(button._event_listeners.values())).handler.__closure__[0].cell_contents
                await connect()
                database = next(e for e in page.descendants() if isinstance(e, DatabaseListbox))
                await next(iter(database._event_listeners.values())).handler(SimpleNamespace(args='db'))
                creator, manager = creators[0], managers[0]
                self.assertIsNone(manager.workspace.active_schema)
                creator.command_select.set_value('move_form')
                creator._command_changed(None)
                await creator.on_parameter_change(creator)
                self.assertIn('/LocalFolder', creator.controls['folder'].options)
                creator.controls['current_form'].set_value(False)
                creator.controls['form'].set_value('Remote/B')
                await creator.on_parameter_change(creator)
                self.assertIn('/RemoteFolder', creator.controls['folder'].options)
                self.assertNotIn('/LocalFolder', creator.controls['folder'].options)
                for selector in ('Missing', '${vars.target}'):
                    creator.controls['form'].set_value(selector)
                    await creator.on_parameter_change(creator)
                    self.assertIn('/LocalFolder', creator.controls['folder'].options)
                    self.assertNotIn('/RemoteFolder', creator.controls['folder'].options)
                service.get_form_schema.assert_not_awaited()
                manager.resolve_schema = AsyncMock(side_effect=lambda data: ExecutionResult(data))
                manager.commands = parse_command_text('WithForm(form="Remote/B")')
                manager._evaluate_commands()
                await manager._evaluation_task
                await creator.on_parameter_change(creator)
                self.assertIn('/RemoteFolder', creator.controls['folder'].options)
                self.assertNotIn('/LocalFolder', creator.controls['folder'].options)
                manager.commands = []
                await next(iter(database._event_listeners.values())).handler(SimpleNamespace(args=''))
                await creator.on_parameter_change(creator)
                self.assertEqual(creator.controls['folder'].options, [])
                creator.controls['form'].set_value('Remote/B')
                await creator.on_parameter_change(creator)
                self.assertIn('/RemoteFolder', creator.controls['folder'].options)
                creator.controls['form'].set_value('Unknown/B')
                await creator.on_parameter_change(creator)
                self.assertEqual(creator.controls['folder'].options, [])


class MoveFormDestinationScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_destination_is_resolved_only_within_selected_forms_database(self):
        from activity_manager.applier import Applier
        from activity_manager.command_execution import ExecutionResult
        for selector, destination, expected in [('Remote/B', 'remoteonly', 'applied'),
                                                 ('Remote/B', 'localonly', 'warning'),
                                                 ('A', 'remoteonly', 'warning'),
                                                 ('Remote/B', 'Missing', 'warning')]:
            with self.subTest(selector=selector, destination=destination):
                a, b = fixtures.form('a', 'A'), fixtures.form('b', 'B', 'remote')
                service = FolderService([a, b])
                service.resources['db']['localonly'] = dict(id='localonly', type='FOLDER', label='Local Only', parentId='db')
                service.resources['remote']['remoteonly'] = dict(id='remoteonly', type='FOLDER', label='Remote Only', parentId='remote')
                engine = Applier(a, parse_command_text(f'MoveForm(currentForm=False, form="{selector}", folder="{destination}")\nAddField(fieldCode="after", label="After")'),
                                 form_service=service, resolve_schema=AsyncMock(side_effect=lambda data: ExecutionResult(data)))
                await engine.evaluate()
                self.assertEqual([e.status for e in engine.executions], [expected, 'applied'])
                active = engine.workspace.active_schema
                self.assertEqual(active['id'], 'b' if expected == 'applied' else 'a')
                self.assertEqual(engine.workspace.state.active_database_id, 'remote' if expected == 'applied' else 'db')
                self.assertEqual(len(engine.effects), 2 if expected == 'applied' else 1)
                self.assertEqual(service.moves, [])
