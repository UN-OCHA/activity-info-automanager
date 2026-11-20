import json
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
from activity_manager.command_execution import ExecutionResult
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands import (
    DELETE_FIELD,
    MOVE_FIELD,
    DeleteFieldCommand,
    MoveFieldCommand,
)
from activity_manager.listbox import DatabaseListbox
from activity_manager.page import connection_page
from activity_manager.schema_change import compare_schemas, schemas_match
from test.execution_fixtures import execute_schema_fixture
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
        self.assertEqual(compare_schemas(first, newer).summary, '')
        self.assertEqual(
            compare_schemas(first, changed).summary,
            '$["label"]: expected "Survey"; actual "Changed"',
        )

    def test_schema_diff_reports_nested_missing_extra_and_reordered_values(self):
        expected = self.schema('A', 'B')
        actual = self.schema('B', 'A', 'C', version=5)
        expected['elements'][0]['description'] = 'Expected description'
        actual['elements'][0]['unexpected'] = None
        comparison = compare_schemas(FormSchema.from_dict(expected), FormSchema.from_dict(actual))
        self.assertFalse(comparison.matches)
        self.assertIn('$["elements"][0]["code"]: expected "A"; actual "B"', comparison.summary)
        self.assertIn('["description"]: expected "Expected description"; actual <missing>', comparison.summary)
        self.assertNotIn('unexpected', comparison.summary)
        self.assertIn('$["elements"][2]["code"]: expected <missing>; actual "C"', comparison.summary)
        self.assertNotIn('schemaVersion', comparison.summary)

    def test_schema_diff_ignores_missing_null_and_false_defaults_in_both_directions(self):
        original = self.schema('A')
        original['elements'][0]['key'] = False
        defaults = self.schema('A')
        defaults['elements'][0].update(description=None, typeParameters={'barcode': False})
        for expected, actual in ((original, defaults), (defaults, original)):
            comparison = compare_schemas(FormSchema.from_dict(expected), FormSchema.from_dict(actual))
            self.assertTrue(comparison.matches, comparison.summary)
            self.assertEqual(comparison.summary, '')
        for value in (True, 0, '', [], {'barcode': True}):
            actual = self.schema('A')
            actual['elements'][0]['extra'] = value
            comparison = compare_schemas(FormSchema.from_dict(original), FormSchema.from_dict(actual))
            self.assertFalse(comparison.matches, repr(value))

    async def test_successful_change_is_reread_and_verified(self):
        loaded = self.schema('A', 'B', version=1)
        target = self.schema('B', 'A', version=1)
        service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[
                self.schema('A', 'B', version=2),
                self.schema('A', 'B', version=2),
                self.schema('B', 'A', version=3),
            ]),
            put_form_schema=AsyncMock(),
        )
        choose = AsyncMock(return_value='leave')
        outcome = await execute_schema_fixture(
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
        outcome = await execute_schema_fixture(
            service, 'form-1', loaded, self.schema('A', 'B'), AsyncMock(),
        )
        self.assertEqual(outcome.status, 'preflight_failed')
        self.assertEqual(outcome.actual_schema, latest)
        service.put_form_schema.assert_not_awaited()

    async def test_matching_target_skips_write_and_verification_read(self):
        loaded = self.schema('A', 'B', version=1)
        latest = self.schema('A', 'B', version=2)
        service = SimpleNamespace(
            get_form_schema=AsyncMock(return_value=latest),
            put_form_schema=AsyncMock(),
        )
        outcome = await execute_schema_fixture(
            service,
            'form-1',
            loaded,
            self.schema('A', 'B', version=1),
            AsyncMock(),
        )
        self.assertEqual(outcome.status, 'success')
        self.assertIsNone(outcome.actual_schema)
        self.assertEqual(outcome.report.journal, ())
        service.get_form_schema.assert_not_awaited()
        service.put_form_schema.assert_not_awaited()

    async def test_api_error_and_unchanged_schema_are_both_reported(self):
        loaded = self.schema('A')
        service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[loaded, loaded, self.schema('A', version=2)]),
            put_form_schema=AsyncMock(side_effect=ValueError('HTTP 500')),
        )
        outcome = await execute_schema_fixture(
            service, 'form-1', loaded, self.schema('A', 'B'), AsyncMock(),
        )
        self.assertEqual(outcome.status, 'left')
        self.assertIn('HTTP 500', outcome.messages[0])
        self.assertEqual(outcome.report.journal[0].outcome, 'unchanged')

    async def test_partial_change_can_be_left_but_divergent_recovery_is_blocked(self):
        loaded = self.schema('A', 'B')
        target = self.schema('B', 'A')
        partial = self.schema('A', 'C', version=2)

        leave_service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[loaded, loaded, partial]),
            put_form_schema=AsyncMock(),
        )
        choose_leave = AsyncMock(return_value='leave')
        left = await execute_schema_fixture(
            leave_service,
            'form-1',
            loaded,
            target,
            choose_leave,
        )
        self.assertEqual(left.status, 'left')
        self.assertEqual(left.actual_schema, partial)
        summary = compare_schemas(FormSchema.from_dict(target), FormSchema.from_dict(partial)).summary
        choose_leave.assert_awaited_once()
        self.assertIn(summary, choose_leave.await_args.args[0])
        self.assertIn(summary, '\n'.join(left.messages))

        rollback_actual = self.schema('A', 'B', version=3)
        rollback_service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[loaded, loaded, partial, rollback_actual]),
            put_form_schema=AsyncMock(),
        )
        rolled_back = await execute_schema_fixture(
            rollback_service,
            'form-1',
            loaded,
            target,
            AsyncMock(return_value='rollback'),
        )
        self.assertEqual(rolled_back.status, 'recovery_blocked')
        self.assertEqual(rolled_back.actual_schema, rollback_actual)
        self.assertEqual(rollback_service.put_form_schema.await_count, 1)

        failed_service = SimpleNamespace(
            get_form_schema=AsyncMock(side_effect=[loaded, loaded, partial, partial]),
            put_form_schema=AsyncMock(),
        )
        failed = await execute_schema_fixture(
            failed_service,
            'form-1',
            loaded,
            target,
            AsyncMock(return_value='rollback'),
        )
        self.assertEqual(failed.status, 'recovery_blocked')
        self.assertEqual(failed.actual_schema, partial)
        self.assertIn('Divergent', failed.messages[-1])


class CommandParserTests(unittest.TestCase):
    def test_blank_lines_comments_multiline_calls_and_strings(self):
        text = '''# Commands may include comments and blank lines.

MoveField(
    fieldCode="""FIELD
A""",
    position="Before field",  # inline comments are allowed
    beforeAfterField="FIELD_B",
)

DeleteField(
    fieldCode="FIELD_C",
)
'''

        self.assertEqual(
            parse_command_text(text),
            [
                MoveFieldCommand('FIELD\nA', 'Before field', 'FIELD_B'),
                DeleteFieldCommand('FIELD_C'),
            ],
        )
        self.assertEqual(parse_command_text('# only a comment\n\n'), [])

    def test_command_names_and_parameter_keys_are_case_insensitive(self):
        commands = parse_command_text(
            'movefield(Position="aFtEr FiElD", FIELDCODE="A", '
            'beforeafterFIELD="B")\nDELETEFIELD(Fieldcode="C")'
        )

        self.assertEqual(
            commands,
            [
                MoveFieldCommand('A', 'After field', 'B'),
                DeleteFieldCommand('C'),
            ],
        )
        self.assertEqual(
            [command.to_syntax() for command in commands],
            [
                'MoveField(fieldCode="A", position="After field", beforeAfterField="B")',
                'DeleteField(fieldCode="C")',
            ],
        )

        with self.assertRaisesRegex(CommandTextError, 'specified more than once'):
            parse_command_text('DeleteField(fieldCode="A", FIELDCODE="B")')

        for entered, canonical in (
            ('BEGINNING', 'Beginning'),
            ('end', 'End'),
            ('BEFORE FIELD', 'Before field'),
            ('after field', 'After field'),
        ):
            relative = (
                ', beforeAfterField="B"'
                if canonical in ('Before field', 'After field')
                else ''
            )
            with self.subTest(position=entered):
                parsed = parse_command_text(
                    f'MoveField(fieldCode="A", position="{entered}"{relative})'
                )
                self.assertEqual(parsed[0].position, canonical)

    def test_document_structure_errors_include_source_location(self):
        cases = (
            (
                'DeleteField(fieldCode="A")\nUnknown(fieldCode="B")',
                "Unknown command 'Unknown'.",
                2,
                'Unknown(fieldCode="B")',
            ),
            (
                'DeleteField(fieldCode="A"); DeleteField(fieldCode="B")',
                'line break, not a semicolon',
                1,
                'DeleteField(fieldCode="A"); DeleteField(fieldCode="B")',
            ),
            (
                'DeleteField(fieldCode="A"',
                "'(' was never closed",
                1,
                'DeleteField(fieldCode="A"',
            ),
            (
                'DeleteField("A")',
                'accepts named parameters only',
                1,
                'DeleteField("A")',
            ),
        )
        for text, message, line, source_line in cases:
            with self.subTest(text=text):
                with self.assertRaises(CommandTextError) as raised:
                    parse_command_text(text)
                self.assertIn(message, raised.exception.message)
                self.assertEqual(raised.exception.line, line)
                self.assertEqual(raised.exception.source_line, source_line)

    def test_multiline_ordinary_string_error_suggests_triple_quotes(self):
        text = 'MoveField(fieldCode="A", position="before\nfield")'

        with self.assertRaises(CommandTextError) as raised:
            parse_command_text(text)

        self.assertEqual(raised.exception.line, 1)
        self.assertIn('unterminated string literal', raised.exception.message)
        self.assertIn(
            'Multiline values must use triple quotes (""" or \'\'\').',
            raised.exception.message,
        )

    def test_command_specific_parameter_validation(self):
        cases = (
            ('DeleteField()', "requires parameter 'fieldCode'"),
            ('DeleteField(fieldCode=123)', 'must be a string literal'),
            ('DeleteField(fieldCode="")', 'cannot be empty'),
            (
                'DeleteField(fieldCode="A", other="B")',
                "unknown parameter 'other'",
            ),
            (
                'DeleteField(fieldCode="A", fieldCode="B")',
                'specified more than once',
            ),
            (
                'MoveField(fieldCode="A", position="Somewhere")',
                "invalid position 'Somewhere'",
            ),
            (
                'MoveField(fieldCode="A", position="Before field")',
                "requires parameter 'beforeAfterField'",
            ),
            (
                'MoveField(fieldCode="A", position="End", beforeAfterField="B")',
                "does not use parameter 'beforeAfterField'",
            ),
        )
        for text, message in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(CommandTextError, message):
                    parse_command_text(text)


class UITests(unittest.IsolatedAsyncioTestCase):
    async def test_unselected_reevaluation_preserves_console_and_saves_command_messages(self):
        managers = []

        def make_manager(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager

        async def resolve(candidate):
            await asyncio.sleep(0)
            return ExecutionResult(candidate)

        with patch('activity_manager.page.CommandListManager', side_effect=make_manager):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
        manager = managers[0]
        manager.resolve_schema = resolve
        console = next(element for element in page.descendants()
                       if isinstance(element, ui.label) and element.text == 'No messages.')
        manager.set_schema({'elements': []})
        manager.add_command(DeleteFieldCommand('missing'))
        await manager._evaluation_task

        # Match the write-completion order: refresh source, clear selection,
        # display the run report, then let background evaluation complete.
        manager.set_schema({'elements': []})
        manager.clear_selection()
        report = 'Schema successfully updated and verified.'
        console.set_text(report)
        await manager._evaluation_task
        self.assertEqual(console.text, report)
        self.assertEqual(manager.executions[0].status, 'warning')
        self.assertIn('missing', manager.executions[0].message)

        manager.select_command(0)
        self.assertEqual(console.text, manager.executions[0].message)
        manager.set_schema({'elements': []})
        await manager._evaluation_task
        self.assertEqual(console.text, manager.executions[0].message)

    async def test_form_list_errors_are_reported_in_console(self):
        service = AsyncMock(spec=ActivityInfoService)
        service.get_form_tree.return_value = {}
        service.get_database_names.return_value = {}
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

    def test_move_field_execution_and_warning_command_trace(self):
        source = {
            'id': 'form-1',
            'elements': [
                {'code': 'A', 'label': 'Alpha'},
                {'code': 'B', 'label': 'Beta'},
                {'code': 'C', 'label': 'Gamma'},
            ],
        }
        move_c = MoveFieldCommand('C', 'Beginning')
        moved = move_c.execute(source).schema
        self.assertEqual([field['code'] for field in moved['elements']], ['C', 'A', 'B'])
        self.assertEqual([field['code'] for field in source['elements']], ['A', 'B', 'C'])
        self.assertEqual(
            [
                field['code']
                for field in MoveFieldCommand(
                    'A', 'After field', 'C',
                ).execute(source).schema['elements']
            ],
            ['B', 'C', 'A'],
        )
        execution = MoveFieldCommand('missing', 'End').execute(source)
        self.assertRegex(execution.message, "could not find field code 'missing'")
        self.assertTrue(execution.diagnostics)
        execution = MoveFieldCommand('A', 'Before field', 'A').execute(source)
        self.assertRegex(execution.message, 'relative to itself')
        self.assertTrue(execution.diagnostics)

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
            ['applied', 'warning', 'applied'],
        )
        self.assertFalse(command_list.execute_button.props['disable'])
        self.assertEqual(
            [field['code'] for field in command_list.executions[0].schema['elements']],
            ['C', 'A', 'B'],
        )
        self.assertEqual(command_list.executions[1].schema, command_list.executions[0].schema)
        self.assertIn('missing', command_list.executions[1].message)
        self.assertEqual(command_list.executions[2].message, 'Command successfully applied to schema.')

        command_list.select_command(0)
        self.assertEqual(command_list.selected_index, 0)
        self.assertEqual(selections[-1], (0, command_list.executions[0]))
        command_list.select_command(0)
        self.assertIsNone(command_list.selected_index)
        self.assertEqual(selections[-1], (None, None))
        command_list.select_command(1)
        self.assertEqual(selections[-1][1].schema, command_list.executions[0].schema)
        original_lines = [command.to_syntax() for command in command_list.commands]
        command_list.move_command(1, 0)
        self.assertEqual(command_list.selected_index, 0)
        self.assertEqual(
            [execution.status for execution in command_list.executions],
            ['warning', 'applied', 'applied'],
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

        result = command.execute(source).schema

        self.assertEqual(result['elements'], [{'code': 'B', 'label': 'Beta'}])
        self.assertEqual(len(source['elements']), 2)
        self.assertEqual(
            DeleteFieldCommand('FIELD "A"').to_syntax(),
            'DeleteField(fieldCode="FIELD \\"A\\"")',
        )
        execution = DeleteFieldCommand('missing').execute(source)
        self.assertRegex(execution.message, "could not find field code 'missing'")
        self.assertTrue(execution.diagnostics)

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

                result = added[0].execute(source).schema
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

        command_list.add_command(before)
        self.assertEqual(
            command_list.advanced_editor.value,
            f'{beginning.to_syntax()}\n{before.to_syntax()}',
        )
        command_list._view_changed(SimpleNamespace(value='text'))
        self.assertFalse(command_list.simple_view.visible)
        self.assertTrue(command_list.advanced_view.visible)
        self.assertFalse(command_list.execute_button.visible)
        self.assertTrue(command_list.text_actions.visible)
        command_list._view_changed(SimpleNamespace(value='list'))
        self.assertTrue(command_list.simple_view.visible)
        self.assertFalse(command_list.advanced_view.visible)
        self.assertTrue(command_list.execute_button.visible)
        self.assertFalse(command_list.text_actions.visible)
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

    def test_text_view_editing_and_discard_workflow(self):
        command = MoveFieldCommand('A', 'Beginning')
        text_mode_changes = []
        with Client(ui.page('/')), ui.column():
            command_list = CommandListManager(
                on_text_mode_change=text_mode_changes.append,
            )
        command_list.add_command(command)

        command_list._view_changed(SimpleNamespace(value='text'))
        original = command.to_syntax()
        self.assertEqual(command_list.stored_text, original)
        self.assertEqual(command_list.advanced_editor.value, original)
        self.assertEqual(text_mode_changes, [True])
        self.assertFalse(command_list.apply_button.props['disable'])
        self.assertFalse(command_list.discard_button.props['disable'])

        edited = f'{original}\nDeleteField(fieldCode="B")'
        command_list._advanced_changed(SimpleNamespace(value=edited))
        self.assertFalse(command_list.apply_button.props['disable'])
        self.assertFalse(command_list.discard_button.props['disable'])

        command_list._view_changed(SimpleNamespace(value='list'))
        self.assertEqual(command_list.current_view, 'text')
        self.assertTrue(command_list.discard_dialog.value)
        command_list.discard_dialog.close()
        self.assertEqual(command_list.current_view, 'text')

        command_list._discard_text()
        self.assertEqual(command_list.current_view, 'list')
        self.assertEqual(command_list.advanced_draft, original)
        self.assertEqual(command_list.commands, [command])
        self.assertEqual(text_mode_changes, [True, False])

    def test_text_view_buttons_remain_enabled_when_edit_returns_to_snapshot(self):
        with Client(ui.page('/')), ui.column():
            command_list = CommandListManager()
        command_list._view_changed(SimpleNamespace(value='text'))
        command_list._advanced_changed(SimpleNamespace(value='changed'))
        self.assertFalse(command_list.apply_button.props['disable'])
        command_list._advanced_changed(SimpleNamespace(value=''))
        self.assertFalse(command_list.apply_button.props['disable'])
        self.assertFalse(command_list.discard_button.props['disable'])

    def test_unchanged_text_actions_return_to_list_and_preserve_selection(self):
        for action in ('_apply_text', '_open_discard_dialog'):
            with self.subTest(action=action), Client(ui.page('/')), ui.column():
                manager = CommandListManager()
                command = MoveFieldCommand('A', 'Beginning')
                manager.add_command(command)
                manager.select_command(0)
                manager._enter_text_view()
                self.assertFalse(manager.count_badge.visible)
                getattr(manager, action)()
                self.assertEqual(manager.current_view, 'list')
                self.assertFalse(manager.discard_dialog.value)
                self.assertTrue(manager.count_badge.visible)
                self.assertIs(manager.commands[0], command)
                self.assertEqual(manager.selected_index, 0)

    def test_clear_commands_resets_selection_text_and_evaluation(self):
        with Client(ui.page('/')), ui.column():
            changes = []
            manager = CommandListManager(on_commands_change=lambda: changes.append(True))
            manager.add_command(MoveFieldCommand('A', 'Beginning'))
            manager.select_command(0)
            manager.clear_commands()
            self.assertEqual(manager.commands, [])
            self.assertEqual(manager.executions, [])
            self.assertIsNone(manager.selected_index)
            self.assertEqual(manager.advanced_editor.value, '')
            self.assertEqual(manager.count_badge.text, '0 commands')
            self.assertEqual(len(changes), 2)

    def test_text_apply_is_atomic_and_reports_errors_without_losing_edits(self):
        original = MoveFieldCommand('A', 'Beginning')
        text_mode_changes = []
        with Client(ui.page('/')), ui.column():
            command_list = CommandListManager(
                on_text_mode_change=text_mode_changes.append,
            )
        command_list.add_command(original)
        command_list._view_changed(SimpleNamespace(value='text'))

        invalid = '# keep this edit\nDeleteField(fieldCode="B"'
        command_list.advanced_editor.set_value(invalid)
        command_list._advanced_changed(SimpleNamespace(value=invalid))
        command_list._apply_text()

        self.assertEqual(command_list.commands, [original])
        self.assertEqual(command_list.advanced_draft, invalid)
        self.assertEqual(command_list.advanced_editor.value, invalid)
        self.assertEqual(command_list.current_view, 'text')
        self.assertTrue(command_list.parse_error_dialog.value)
        self.assertEqual(command_list.parse_error_location.text, 'Line 2, column 12')
        self.assertIn('never closed', command_list.parse_error_message.text)
        self.assertEqual(
            command_list.parse_error_source.text,
            'DeleteField(fieldCode="B"',
        )

        command_list.parse_error_dialog.close()  # Fix keeps the edited text.
        self.assertEqual(command_list.advanced_draft, invalid)
        valid = '# replacement\n\nDeleteField(\n    fieldCode="B",\n)'
        command_list.advanced_editor.set_value(valid)
        command_list._advanced_changed(SimpleNamespace(value=valid))
        command_list._apply_text()

        self.assertEqual(command_list.commands, [DeleteFieldCommand('B')])
        self.assertEqual(command_list.current_view, 'list')
        self.assertEqual(command_list.count_badge.text, '1 command')
        self.assertEqual(
            command_list.advanced_draft,
            'DeleteField(fieldCode="B")',
        )
        self.assertEqual(text_mode_changes, [True, False])

    def test_parse_error_discard_restores_original_list(self):
        original = DeleteFieldCommand('A')
        with Client(ui.page('/')), ui.column():
            command_list = CommandListManager()
        command_list.add_command(original)
        command_list._view_changed(SimpleNamespace(value='text'))
        command_list._advanced_changed(SimpleNamespace(value='Unknown()'))
        command_list._apply_text()

        command_list._discard_parse_error()

        self.assertFalse(command_list.parse_error_dialog.value)
        self.assertEqual(command_list.current_view, 'list')
        self.assertEqual(command_list.commands, [original])
        self.assertEqual(command_list.advanced_draft, original.to_syntax())

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
        service.get_form_tree.return_value = {}
        service.get_database_names.return_value = {}
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
        managers = []

        def make_manager(**kwargs):
            manager = CommandListManager(**kwargs)
            managers.append(manager)
            return manager

        service = AsyncMock(spec=ActivityInfoService)
        service.get_form_tree.return_value = {}
        service.get_database_names.return_value = {}
        service.get_databases.return_value = {'db': 'Database (db)'}
        service.get_forms.return_value = {}
        payload = {'id': 'form-1', 'elements': [{'id': 'field-1'}]}
        service.get_form_tree.return_value = {'form-1': payload}
        with (patch('activity_manager.page.ActivityInfoService', return_value=service),
              patch('activity_manager.page.CommandListManager', side_effect=make_manager)):
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
            service.get_form_tree.assert_awaited_once_with('form-1')
            service.get_form_schema.assert_not_awaited()
            self.assertTrue(schema.visible)
            self.assertEqual(schema.content, '{\n  "id": "form-1",\n  "elements": [\n    {\n      "id": "field-1"\n    }\n  ]\n}')
            payload['elements'][0].update(code='NAME', relevanceCondition='field_1')
            payload['elements'][0]['id'] = 'field_1'
            await form_change(SimpleNamespace(args='form-1'))
            toggle = next(e for e in page.descendants()
                          if isinstance(e, ui.toggle) and 'names' in e.options)
            toggle.set_value('names')
            await asyncio.sleep(0.01)
            self.assertEqual(json.loads(schema.content)['elements'][0]['relevanceCondition'], 'NAME')
            toggle.set_value('ids')
            await asyncio.sleep(0.01)
            self.assertEqual(json.loads(schema.content)['elements'][0]['relevanceCondition'], 'field_1')
            self.assertEqual(service.get_form_tree.await_count, 2)
            service.get_form_schema.assert_not_awaited()
            payload['elements'][0].update(
                type='reference', defaultValue='people:record1',
                typeParameters={'range': [{'formId': 'people'}]},
            )
            service.get_form_tree.return_value['people'] = {
                'id': 'people', 'label': 'People', 'recordLabelFieldId': 'name', 'elements': [],
            }
            service.query_rows.return_value = [{'name': 'Alice'}]
            await form_change(SimpleNamespace(args='form-1'))
            await asyncio.sleep(0.01)
            toggle.set_value('names')
            await asyncio.sleep(0.01)
            self.assertEqual(json.loads(schema.content)['elements'][0]['defaultValue'], 'People:Alice')
            self.assertTrue(schema.visible)
            self.assertIn('<u>', schema.markdown._props['innerHTML'])
            self.assertNotIn('title=', schema.markdown._props['innerHTML'])
            self.assertIn('class="codehilite"', schema.markdown._props['innerHTML'])
            toggle.set_value('ids')
            await asyncio.sleep(0.01)
            self.assertEqual(json.loads(schema.content)['elements'][0]['defaultValue'], 'people:record1')
            self.assertNotIn('<u>', schema.markdown._props['innerHTML'])
            service.query_rows.assert_awaited_once_with({
                'rowSources': [{'rootFormId': 'people'}],
                'columns': [{'id': 'name', 'formula': 'name'}],
                'filter': '_id == \"record1\"',
            })
            self.assertEqual(payload['elements'][0]['defaultValue'], 'people:record1')
            # Loading alone must annotate a dotted reference to a deleted field.
            payload['elements'][0]['validationCondition'] = 'field_1.deleted_field > 0'
            await form_change(SimpleNamespace(args='form-1'))
            self.assertIn('<mark>', schema.markdown._props['innerHTML'])
            toggle.set_value('names')
            self.assertIn('NAME.deleted_field', schema.content)
            self.assertIn('<mark>', schema.markdown._props['innerHTML'])
            unresolved = next(e for e in page.descendants()
                              if isinstance(e, ui.badge) and e.props.get('aria-label') == 'Unresolved names')
            self.assertEqual(unresolved.text, 'Unresolved: 1')
            self.assertTrue(unresolved.visible)
            payload['elements'][0].pop('validationCondition')
            await form_change(SimpleNamespace(args='form-1'))
            self.assertNotIn('<mark>', schema.markdown._props['innerHTML'])
            self.assertFalse(unresolved.visible)
            manager = managers[0]
            manager.add_command(DeleteFieldCommand('missing'))
            await manager._evaluation_task
            manager.select_command(0)
            self.assertEqual(manager.selected_index, 0)
            service.get_form_tree.return_value = {'form-2': {'id': 'form-2', 'elements': []}}
            await form_change(SimpleNamespace(args='form-2'))
            self.assertIsNone(manager.selected_index)
            self.assertIsNone(manager.selected_iteration)
            self.assertEqual(json.loads(schema.content)['id'], 'form-2')
            await manager._evaluation_task
            self.assertIsNone(manager.selected_index)
            await database_change(SimpleNamespace(args='db-2'))
            self.assertFalse(schema.visible)
            self.assertEqual(schema.content, '')


if __name__ == '__main__':
    unittest.main()
