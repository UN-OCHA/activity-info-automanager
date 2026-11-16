import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from nicegui import ui
from nicegui.client import Client

from activity_manager.api import DEFAULT_BASE_URL, ActivityInfoService
from activity_manager.combobox import FreeTextCombobox
from activity_manager.command_creator import CommandCreatorRenderer, field_codes_from_schema
from activity_manager.command_list import CommandListManager
from activity_manager.commands import (
    DELETE_FIELD,
    MOVE_FIELD,
    DeleteFieldCommand,
    MoveFieldCommand,
)
from activity_manager.listbox import DatabaseListbox
from activity_manager.page import connection_page
from activity_manager.schema_change import apply_form_schema_change, schemas_match
from activity_info_api_client.models.add_update_form_response import AddUpdateFormResponse
from activity_info_api_client.models.form_schema import FormSchema


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_form_schema_is_sent_to_generated_update_endpoint(self):
        payload = {
            'id': 'form-1',
            'label': 'Survey',
            'schemaVersion': 3,
            'databaseId': 'db',
            'elements': [],
        }
        service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
        response = AddUpdateFormResponse(database=object(), forms=[])
        try:
            with patch.object(
                service, '_request', AsyncMock(return_value=response),
            ) as request:
                await service.put_form_schema('form-1', payload)
            request.assert_awaited_once()
            kwargs = request.await_args.kwargs
            self.assertEqual(kwargs['form_id'], 'form-1')
            self.assertEqual(kwargs['body'].to_dict(), payload)
            self.assertEqual(kwargs['action'], 'update the form schema')
        finally:
            await service.aclose()

    async def test_service_reuses_and_closes_one_http_client(self):
        original = httpx.AsyncClient
        clients = []

        def handler(request):
            return httpx.Response(200, json=[])

        def client(**kwargs):
            instance = original(transport=httpx.MockTransport(handler), **kwargs)
            clients.append(instance)
            return instance

        with patch('activity_info_api_client.client.httpx.AsyncClient', side_effect=client) as factory:
            service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
            await service.get_databases()
            await service.get_databases()
            factory.assert_called_once()
            self.assertFalse(clients[0].is_closed)
            await service.aclose()
            self.assertTrue(clients[0].is_closed)

    async def fetch(self, status, payload, database_id=None):
        original = httpx.AsyncClient

        def handler(request):
            self.assertEqual(str(request.url), DEFAULT_BASE_URL + '/databases' + (f'/{database_id}' if database_id else ''))
            self.assertEqual(request.headers['authorization'], 'Bearer test-token')
            return httpx.Response(status, json=payload)

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), **kwargs)

        with patch('activity_info_api_client.client.httpx.AsyncClient', side_effect=client):
            service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
            try:
                if database_id:
                    return await service.get_forms(database_id)
                return await service.get_databases()
            finally:
                await service.aclose()

    async def test_forms_include_nested_forms_and_subforms_only(self):
        tree = dict(databaseId='db', userId='user', version='1', label='Database',
                    ownerRef=None, suspended=False, role={'id': 'owner'}, roles=[],
                    storage='', publishedTemplate=False, securityCategories=[],
                    billingAccountId=1, billingPlan='', resources=[
                        {'id': 'folder', 'type': 'FOLDER', 'label': 'Folder'},
                        {'id': 'b', 'type': 'SUB_FORM', 'label': 'Visits', 'parentId': 'a'},
                        {'id': 'a', 'type': 'FORM', 'label': 'Survey', 'parentId': 'folder'},
                        {'id': 'c', 'type': 'FORM', 'label': 'alpha', 'parentId': 'folder'},
                        {'id': 'report', 'type': 'REPORT', 'label': 'Report'},
                    ])
        self.assertEqual(await self.fetch(200, tree, 'db'),
                         {'c': 'alpha (c)', 'a': 'Survey (a)', 'b': 'Visits (b)'})
        tree['resources'] = []
        self.assertEqual(await self.fetch(200, tree, 'db'), {})

    async def test_form_schema_is_returned_as_json_ready_data(self):
        payload = {
            'id': 'form-1', 'label': 'Survey', 'schemaVersion': 3,
            'databaseId': 'db', 'elements': [],
        }
        original = httpx.AsyncClient

        def handler(request):
            self.assertEqual(str(request.url), DEFAULT_BASE_URL + '/form/form-1/schema')
            return httpx.Response(200, json=payload)

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), **kwargs)

        with patch('activity_info_api_client.client.httpx.AsyncClient', side_effect=client):
            service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
            self.assertEqual(await service.get_form_schema('form-1'), payload)
            await service.aclose()

    async def test_databases_and_empty_result(self):
        result = await self.fetch(200, [
            {'databaseId': 'z', 'label': 'Zulu'},
            {'databaseId': 'b', 'label': 'Same name'},
            {'databaseId': 'a', 'label': 'Same name'},
            {'databaseId': 'c', 'label': 'alpha'},
        ])
        self.assertEqual(result, {
            'c': 'alpha (c)',
            'a': 'Same name (a)',
            'b': 'Same name (b)',
            'z': 'Zulu (z)',
        })
        self.assertEqual(await self.fetch(200, []), {})

    async def test_errors(self):
        with self.assertRaisesRegex(ValueError, 'HTTP 403'):
            await self.fetch(403, {})
        with self.assertRaisesRegex(
            ValueError,
            r"invalid API response while trying to load databases.*KeyError.*databaseId",
        ):
            await self.fetch(200, [{'unexpected': 'payload'}])
        with self.assertRaisesRegex(ValueError, 'API token'):
            ActivityInfoService(DEFAULT_BASE_URL, '')


class SchemaChangeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def schema(*codes, version=1, label='Survey'):
        return {
            'id': 'form-1',
            'label': label,
            'schemaVersion': version,
            'databaseId': 'db',
            'elements': [
                {
                    'id': f'field-{code}',
                    'label': code,
                    'required': False,
                    'type': 'FREE_TEXT',
                    'code': code,
                }
                for code in codes
            ],
        }

    def test_schema_comparison_ignores_only_schema_version(self):
        first = FormSchema.from_dict(self.schema('A', version=1))
        newer = FormSchema.from_dict(self.schema('A', version=9))
        changed = FormSchema.from_dict(self.schema('A', version=9, label='Changed'))
        self.assertTrue(schemas_match(first, newer))
        self.assertFalse(schemas_match(first, changed))

    async def test_successful_change_is_reread_and_verified(self):
        loaded = self.schema('A', 'B', version=1)
        target = self.schema('B', 'A', version=1)
        service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[
                self.schema('A', 'B', version=2),
                self.schema('B', 'A', version=3),
            ]),
            put_form_schema=AsyncMock(),
        )
        choose = AsyncMock(return_value='leave')
        outcome = await apply_form_schema_change(
            service, 'form-1', loaded, target, choose,
        )
        self.assertEqual(outcome.status, 'success')
        self.assertEqual(outcome.actual_schema['schemaVersion'], 3)
        service.put_form_schema.assert_awaited_once_with('form-1', target)
        choose.assert_not_awaited()

    async def test_stale_schema_stops_before_write(self):
        loaded = self.schema('A')
        latest = self.schema('A', label='Changed')
        service = SimpleNamespace(
            get_form_schema=AsyncMock(return_value=latest),
            put_form_schema=AsyncMock(),
        )
        outcome = await apply_form_schema_change(
            service, 'form-1', loaded, self.schema('A', 'B'), AsyncMock(),
        )
        self.assertEqual(outcome.status, 'stale')
        self.assertEqual(outcome.actual_schema, latest)
        service.put_form_schema.assert_not_awaited()

    async def test_matching_target_skips_write_and_verification_read(self):
        loaded = self.schema('A', 'B', version=1)
        latest = self.schema('A', 'B', version=2)
        service = SimpleNamespace(
            get_form_schema=AsyncMock(return_value=latest),
            put_form_schema=AsyncMock(),
        )
        outcome = await apply_form_schema_change(
            service,
            'form-1',
            loaded,
            self.schema('A', 'B', version=1),
            AsyncMock(),
        )
        self.assertEqual(outcome.status, 'unchanged')
        self.assertEqual(outcome.actual_schema, latest)
        self.assertEqual(outcome.messages, ('no schema changes required',))
        service.get_form_schema.assert_awaited_once_with('form-1')
        service.put_form_schema.assert_not_awaited()

    async def test_api_error_and_unchanged_schema_are_both_reported(self):
        loaded = self.schema('A')
        service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[loaded, self.schema('A', version=2)]),
            put_form_schema=AsyncMock(side_effect=ValueError('HTTP 500')),
        )
        outcome = await apply_form_schema_change(
            service, 'form-1', loaded, self.schema('B'), AsyncMock(),
        )
        self.assertEqual(outcome.status, 'unchanged')
        self.assertEqual(len(outcome.messages), 2)
        self.assertIn('HTTP 500', outcome.messages[0])
        self.assertIn('Schema not changed', outcome.messages[1])

    async def test_partial_change_can_be_left_or_rolled_back(self):
        loaded = self.schema('A', 'B')
        target = self.schema('B', 'A')
        partial = self.schema('A', 'C', version=2)

        leave_service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[loaded, partial]),
            put_form_schema=AsyncMock(),
        )
        left = await apply_form_schema_change(
            leave_service,
            'form-1',
            loaded,
            target,
            AsyncMock(return_value='leave'),
        )
        self.assertEqual(left.status, 'partial_left')
        self.assertEqual(left.actual_schema, partial)

        rollback_actual = self.schema('A', 'B', version=3)
        rollback_service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[loaded, partial, rollback_actual]),
            put_form_schema=AsyncMock(),
        )
        rolled_back = await apply_form_schema_change(
            rollback_service,
            'form-1',
            loaded,
            target,
            AsyncMock(return_value='rollback'),
        )
        self.assertEqual(rolled_back.status, 'rolled_back')
        self.assertEqual(rolled_back.actual_schema, rollback_actual)
        self.assertEqual(rollback_service.put_form_schema.await_count, 2)

        failed_service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[loaded, partial, partial]),
            put_form_schema=AsyncMock(),
        )
        failed = await apply_form_schema_change(
            failed_service,
            'form-1',
            loaded,
            target,
            AsyncMock(return_value='rollback'),
        )
        self.assertEqual(failed.status, 'rollback_failed')
        self.assertEqual(failed.actual_schema, partial)
        self.assertIn('failed verification', failed.messages[-1])


class UITests(unittest.IsolatedAsyncioTestCase):
    async def test_form_list_errors_are_reported_in_console(self):
        service = AsyncMock(spec=ActivityInfoService)
        service.get_databases.return_value = {'db': 'Database (db)'}
        service.get_forms.side_effect = ValueError(
            "The server returned an invalid API response. ValueError: 'NEW_TYPE'"
        )
        with patch('activity_manager.page.ActivityInfoService', return_value=service):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
            console = next(
                element for element in page.descendants()
                if isinstance(element, ui.label) and element.text == 'No messages.'
            )
            token = next(
                element for element in page.descendants()
                if isinstance(element, ui.input) and element.label == 'API token'
            )
            token.value = 'test-token'
            await asyncio.sleep(0.01)
            button = next(
                element for element in page.descendants()
                if isinstance(element, ui.button) and element.text == 'Connect'
            )
            connect = next(iter(button._event_listeners.values())).handler.__closure__[0].cell_contents
            await connect()
            databases = next(
                element for element in page.descendants()
                if isinstance(element, DatabaseListbox)
            )
            change = next(iter(databases._event_listeners.values())).handler
            await change(SimpleNamespace(args='db'))
            self.assertIn('Form list error:', console.text)
            self.assertIn('NEW_TYPE', console.text)

    def test_move_field_execution_and_fatal_command_trace(self):
        source = {
            'id': 'form-1',
            'elements': [
                {'code': 'A', 'label': 'Alpha'},
                {'code': 'B', 'label': 'Beta'},
                {'code': 'C', 'label': 'Gamma'},
            ],
        }
        move_c = MoveFieldCommand('C', 'Beginning')
        moved = move_c.execute(source)
        self.assertEqual([field['code'] for field in moved['elements']], ['C', 'A', 'B'])
        self.assertEqual([field['code'] for field in source['elements']], ['A', 'B', 'C'])
        self.assertEqual(
            [
                field['code']
                for field in MoveFieldCommand(
                    'A', 'After field', 'C',
                ).execute(source)['elements']
            ],
            ['B', 'C', 'A'],
        )
        with self.assertRaisesRegex(ValueError, "could not find field code 'missing'"):
            MoveFieldCommand('missing', 'End').execute(source)
        with self.assertRaisesRegex(ValueError, 'relative to itself'):
            MoveFieldCommand('A', 'Before field', 'A').execute(source)

        selections = []
        with Client(ui.page('/')), ui.column():
            command_list = CommandListManager(
                on_selection_change=lambda index, result: selections.append((index, result)),
                on_execute=lambda: None,
            )
        command_list.add_command(move_c)
        command_list.add_command(MoveFieldCommand('missing', 'End'))
        command_list.add_command(MoveFieldCommand('A', 'End'))
        command_list.set_schema(source)
        self.assertEqual(
            [execution.status for execution in command_list.executions],
            ['applied', 'error', 'not_executed'],
        )
        self.assertTrue(command_list.execute_button.props['disable'])
        self.assertEqual(
            [field['code'] for field in command_list.executions[0].schema['elements']],
            ['C', 'A', 'B'],
        )
        self.assertIsNone(command_list.executions[1].schema)
        self.assertIn('missing', command_list.executions[1].message)
        self.assertEqual(command_list.executions[2].message, 'Did not execute.')

        command_list.select_command(0)
        self.assertEqual(command_list.selected_index, 0)
        self.assertEqual(selections[-1], (0, command_list.executions[0]))
        command_list.select_command(0)
        self.assertIsNone(command_list.selected_index)
        self.assertEqual(selections[-1], (None, None))
        command_list.select_command(1)
        self.assertIsNone(selections[-1][1].schema)
        original_lines = [command.to_syntax() for command in command_list.commands]
        command_list.move_command(1, 0)
        self.assertEqual(command_list.selected_index, 0)
        self.assertEqual(
            [execution.status for execution in command_list.executions],
            ['error', 'not_executed', 'not_executed'],
        )
        self.assertEqual(
            command_list.advanced_draft,
            '\n'.join([original_lines[1], original_lines[0], original_lines[2]]),
        )
        command_list.delete_command(0)
        self.assertIsNone(command_list.selected_index)
        self.assertEqual(
            [execution.status for execution in command_list.executions],
            ['applied', 'applied'],
        )
        self.assertFalse(command_list.execute_button.props['disable'])
        self.assertEqual(
            command_list.advanced_draft,
            '\n'.join([original_lines[0], original_lines[2]]),
        )

    def test_delete_field_execution_syntax_and_creator_definition(self):
        source = {
            'id': 'form-1',
            'elements': [
                {'code': 'A', 'label': 'Alpha'},
                {'code': 'B', 'label': 'Beta'},
            ],
        }
        command = DeleteFieldCommand('A')

        result = command.execute(source)

        self.assertEqual(result['elements'], [{'code': 'B', 'label': 'Beta'}])
        self.assertEqual(len(source['elements']), 2)
        self.assertEqual(
            DeleteFieldCommand('FIELD "A"').to_syntax(),
            'DeleteField(fieldCode="FIELD \\"A\\"")',
        )
        with self.assertRaisesRegex(ValueError, "could not find field code 'missing'"):
            DeleteFieldCommand('missing').execute(source)

        self.assertEqual(DELETE_FIELD.key, 'delete_field')
        self.assertEqual(DELETE_FIELD.label, 'Delete field')
        self.assertEqual(len(DELETE_FIELD.parameters), 1)
        self.assertEqual(DELETE_FIELD.parameters[0], MOVE_FIELD.parameters[0])
        self.assertEqual(
            DELETE_FIELD.create_command({'field_code': 'B'}),
            DeleteFieldCommand('B'),
        )

    def test_field_commands_created_through_ui_modify_schema(self):
        source = {
            'id': 'form-1',
            'elements': [
                {'code': 'A', 'label': 'Alpha'},
                {'code': 'B', 'label': 'Beta'},
                {'code': 'C', 'label': 'Gamma'},
            ],
        }
        cases = (
            {
                'command_key': 'move_field',
                'values': {'field_code': 'C', 'position': 'Beginning'},
                'expected_command': MoveFieldCommand('C', 'Beginning'),
                'expected_codes': ['C', 'A', 'B'],
            },
            {
                'command_key': 'delete_field',
                'values': {'field_code': 'B'},
                'expected_command': DeleteFieldCommand('B'),
                'expected_codes': ['A', 'C'],
            },
        )

        for case in cases:
            with self.subTest(command=case['command_key']):
                added = []
                with Client(ui.page('/')), ui.column():
                    creator = CommandCreatorRenderer(
                        (MOVE_FIELD, DELETE_FIELD), on_add=added.append,
                    )
                creator.set_schema(source)
                creator.command_select.set_value(case['command_key'])
                creator._command_changed(None)

                for parameter, value in case['values'].items():
                    creator.controls[parameter].set_value(value)
                    creator._parameter_changed(None)

                self.assertFalse(creator.add_button.props['disable'])
                creator._add_command()
                self.assertEqual(added, [case['expected_command']])

                result = added[0].execute(source)
                self.assertEqual(
                    [field['code'] for field in result['elements']],
                    case['expected_codes'],
                )
                self.assertEqual(
                    [field['code'] for field in source['elements']],
                    ['A', 'B', 'C'],
                )

    def test_move_field_syntax_and_command_list_views(self):
        beginning = MoveFieldCommand('FIELD "A"', 'Beginning')
        before = MoveFieldCommand('FIELD_B', 'Before field', 'FIELD_A')
        self.assertEqual(
            beginning.to_syntax(),
            'MoveField(fieldCode="FIELD \\"A\\"", position="Beginning")',
        )
        self.assertEqual(
            before.to_syntax(),
            'MoveField(fieldCode="FIELD_B", position="Before field", '
            'beforeAfterField="FIELD_A")',
        )

        with Client(ui.page('/')), ui.column():
            command_list = CommandListManager()
        command_list.add_command(beginning)
        self.assertEqual(command_list.commands, [beginning])
        self.assertEqual(command_list.count_badge.text, '1 command')
        self.assertEqual(command_list.advanced_editor.value, beginning.to_syntax())

        edited = 'ImportedCommand(value="draft")'
        command_list.advanced_editor.set_value(edited)
        command_list._advanced_changed(SimpleNamespace(value=edited))
        command_list.add_command(before)
        self.assertEqual(
            command_list.advanced_editor.value,
            f'{beginning.to_syntax()}\n{before.to_syntax()}',
        )
        command_list._view_changed(SimpleNamespace(value='advanced'))
        self.assertFalse(command_list.simple_view.visible)
        self.assertTrue(command_list.advanced_view.visible)
        command_list._view_changed(SimpleNamespace(value='simple'))
        self.assertTrue(command_list.simple_view.visible)
        self.assertFalse(command_list.advanced_view.visible)
        self.assertEqual(
            command_list.advanced_draft,
            f'{beginning.to_syntax()}\n{before.to_syntax()}',
        )
        command_list.select_command(1)
        command_list.move_command(1, 0)
        self.assertEqual(command_list.commands, [before, beginning])
        self.assertEqual(command_list.selected_index, 0)
        self.assertEqual(
            command_list.advanced_draft,
            f'{before.to_syntax()}\n{beginning.to_syntax()}',
        )
        command_list.delete_command(0)
        self.assertEqual(command_list.commands, [beginning])
        self.assertIsNone(command_list.selected_index)
        self.assertEqual(command_list.count_badge.text, '1 command')
        self.assertEqual(command_list.advanced_draft, beginning.to_syntax())

    def test_move_field_creator_controls_and_visibility(self):
        schema = {
            'elements': [
                {'code': 'name'},
                {'code': 'address'},
                {'code': 'city'},
                {'typeParameters': {'code': 'not_a_field'}},
                {'code': 'name'},
            ],
        }
        self.assertEqual(field_codes_from_schema(schema), ['name', 'address', 'city'])

        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((MOVE_FIELD,))
        creator.set_schema(schema)

        field_code = creator.controls['field_code']
        relative_code = creator.controls['relative_field_code']
        position = creator.controls['position']
        self.assertIsInstance(field_code, FreeTextCombobox)
        self.assertEqual(field_code.options, ['name', 'address', 'city'])
        self.assertFalse(creator.parameter_containers['relative_field_code'].visible)
        self.assertTrue(creator.add_button.props['disable'])

        field_code.set_value('name')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])

        position.set_value('Beginning')
        creator._parameter_changed(None)
        self.assertFalse(creator.add_button.props['disable'])

        position.set_value('Before field')
        creator._parameter_changed(None)
        self.assertTrue(creator.parameter_containers['relative_field_code'].visible)
        self.assertTrue(creator.add_button.props['disable'])
        relative_code.set_value('custom_code')
        creator._parameter_changed(None)
        self.assertFalse(creator.add_button.props['disable'])
        self.assertEqual(relative_code.entered_value, 'custom_code')
        self.assertNotIn('custom_code', relative_code.options)
        added = []
        creator.on_add = added.append
        creator._add_command()
        self.assertEqual(
            added,
            [MoveFieldCommand('name', 'Before field', 'custom_code')],
        )
        creator.set_schema({'elements': [{'code': 'other'}]})
        self.assertEqual(relative_code.entered_value, 'custom_code')
        self.assertEqual(relative_code.options, ['other'])

        position.set_value('End')
        creator._parameter_changed(None)
        self.assertFalse(creator.parameter_containers['relative_field_code'].visible)
        self.assertIsNone(relative_code.value)
        self.assertFalse(creator.add_button.props['disable'])

    def test_page_and_single_select_listbox(self):
        with Client(ui.page('/')), ui.column() as page:
            connection_page()
        selectors = [element for element in page.descendants() if isinstance(element, DatabaseListbox)]
        self.assertEqual(len(selectors), 2)
        self.assertTrue(selectors[1].props['disabled'])
        selector = selectors[0]
        self.assertNotIn('multiple', selector.props)
        selector.set_options({'a': 'Alpha', 'b': 'Beta'})
        self.assertFalse(selector.props['disabled'])
        selector.disable()
        self.assertTrue(selector.props['disabled'])
        selector.enable()
        self.assertFalse(selector.props['disabled'])
        self.assertEqual([option.text for option in selector.default_slot.children],
                         ['Select a database', 'Alpha', 'Beta'])
        selector.value = 'a'
        selector.set_options({})
        self.assertIsNone(selector.value)
        self.assertTrue(selector.props['disabled'])

    async def test_selection_loads_forms_and_ignores_stale_requests(self):
        service = AsyncMock(spec=ActivityInfoService)
        service.get_databases.return_value = {'old': 'Old (old)', 'new': 'New (new)'}
        pending = asyncio.get_running_loop().create_future()

        async def fetch_forms(database_id):
            return await pending if database_id == 'old' else {'new-form': 'New (new-form)'}

        service.get_forms.side_effect = fetch_forms
        with patch('activity_manager.page.ActivityInfoService', return_value=service):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
            token = next(e for e in page.descendants() if isinstance(e, ui.input) and e.label == 'API token')
            token.value = 'test-token'
            await asyncio.sleep(0.01)
            button = next(e for e in page.descendants() if isinstance(e, ui.button) and e.text == 'Connect')
            connect = next(iter(button._event_listeners.values())).handler.__closure__[0].cell_contents
            await connect()
            databases, forms = [e for e in page.descendants() if isinstance(e, DatabaseListbox)]
            change = next(iter(databases._event_listeners.values())).handler
            old = asyncio.create_task(change(SimpleNamespace(args='old')))
            await asyncio.sleep(0)
            self.assertTrue(forms.props['disabled'])
            await change(SimpleNamespace(args='new'))
            self.assertIsNone(forms.value)
            self.assertEqual(forms.default_slot.children[1].text, 'New (new-form)')
            pending.set_result({'old-form': 'Old (old-form)'})
            await old
            self.assertEqual(forms.default_slot.children[1].text, 'New (new-form)')
            token.set_value('changed')
            await token._change_handlers[0]()
            self.assertIsNone(forms.value)
            self.assertTrue(forms.props['disabled'])
            service.aclose.assert_awaited_once()

    async def test_form_selection_loads_and_clears_pretty_schema(self):
        service = AsyncMock(spec=ActivityInfoService)
        service.get_databases.return_value = {'db': 'Database (db)'}
        service.get_forms.return_value = {}
        payload = {'id': 'form-1', 'elements': [{'id': 'field-1'}]}
        service.get_form_schema.return_value = payload
        with patch('activity_manager.page.ActivityInfoService', return_value=service):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
            token = next(e for e in page.descendants() if isinstance(e, ui.input) and e.label == 'API token')
            token.value = 'test-token'
            await asyncio.sleep(0.01)
            button = next(e for e in page.descendants() if isinstance(e, ui.button) and e.text == 'Connect')
            connect = next(iter(button._event_listeners.values())).handler.__closure__[0].cell_contents
            await connect()
            databases, forms = [e for e in page.descendants() if isinstance(e, DatabaseListbox)]
            schema = next(e for e in page.descendants() if isinstance(e, ui.code))
            form_change = next(iter(forms._event_listeners.values())).handler
            database_change = next(iter(databases._event_listeners.values())).handler
            await form_change(SimpleNamespace(args='form-1'))
            self.assertTrue(schema.visible)
            self.assertEqual(schema.content, '{\n  "id": "form-1",\n  "elements": [\n    {\n      "id": "field-1"\n    }\n  ]\n}')
            await database_change(SimpleNamespace(args='db-2'))
            self.assertFalse(schema.visible)
            self.assertEqual(schema.content, '')


if __name__ == '__main__':
    unittest.main()
