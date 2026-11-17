from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_execution import Diagnostic, ExecutionResult, apply_command
from activity_manager.commands import ADD_FIELD, AddFieldCommand, MoveFieldCommand, DeleteFieldCommand
from activity_manager.schema_resolution import resolve_command_schema
from test_add_field import schema


class ExecutionContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_and_resolver_diagnostics_stack_in_order(self):
        source = schema()
        command = SimpleNamespace(execute=lambda data: ExecutionResult(
            deepcopy(data), (Diagnostic('warning', 'X'),)))
        resolver = AsyncMock(return_value=ExecutionResult(source, (
            Diagnostic('warning', 'Y'), Diagnostic('error', 'Z'))))
        result = await apply_command(command, source, resolver)
        self.assertEqual(result.message, 'Warning: X\nWarning: Y\nError: Z')
        self.assertEqual(result.status, 'error')
        self.assertEqual([item.severity for item in result.diagnostics], ['warning', 'warning', 'error'])

    async def test_command_error_stops_before_resolver_without_losing_earlier_warnings(self):
        source = schema()
        command = SimpleNamespace(execute=lambda data: ExecutionResult(data, (
            Diagnostic('warning', 'Earlier'), Diagnostic('error', 'Stop'))))
        resolver = AsyncMock()
        result = await apply_command(command, source, resolver)
        self.assertEqual(result.message, 'Warning: Earlier\nError: Stop')
        resolver.assert_not_awaited()

    async def test_unexpected_resolver_failure_keeps_command_warning(self):
        command = DeleteFieldCommand('absent')
        result = await apply_command(command, schema(), AsyncMock(side_effect=RuntimeError('Failure')))
        self.assertEqual([item.severity for item in result.diagnostics], ['warning', 'error'])
        self.assertIn('DeleteField could not find', result.message)
        self.assertIn('Error: RuntimeError: Failure', result.message)

    async def test_internal_resolver_failure_keeps_earlier_formula_warning(self):
        source = schema()
        source['elements'][0]['validationCondition'] = 'UNKNOWN != ""'
        source['elements'].append({'id': 'ref', 'code': 'REF', 'type': 'reference',
            'defaultValue': 'Alice', 'typeParameters': {'range': [{'formId': 'people'}]}})
        service = SimpleNamespace(query_rows=AsyncMock(side_effect=RuntimeError('Failure')))
        related = {'people': {'id': 'people', 'recordLabelFieldId': 'name', 'elements': []}}

        async def resolve(data):
            return await resolve_command_schema(service, data, related, {}, {}, {})

        result = await apply_command(DeleteFieldCommand('absent'), source, resolve)
        self.assertEqual([item.severity for item in result.diagnostics], ['warning', 'warning', 'error'])
        self.assertIn('UNKNOWN', result.diagnostics[1].message)
        self.assertIn('Failure', result.diagnostics[2].message)

    def test_every_command_returns_the_same_result_contract(self):
        source = schema()
        for command, state in [(AddFieldCommand('new', 'New'), 'applied'),
                               (AddFieldCommand('old', 'Duplicate'), 'error'),
                               (MoveFieldCommand('absent', 'End'), 'warning'),
                               (DeleteFieldCommand('absent'), 'warning')]:
            with self.subTest(command=command):
                result = command.execute(source)
                self.assertIsInstance(result, ExecutionResult)
                self.assertEqual(result.status, state)
                if result.diagnostics:
                    self.assertTrue(result.diagnostics[0].code)
        self.assertEqual(source, schema())

    def test_creator_parses_without_executing_or_schema_validation(self):
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((ADD_FIELD,), on_add=added.append)
        with patch.object(AddFieldCommand, 'execute', side_effect=AssertionError('Creation must not execute')):
            creator.set_schema(schema())
            creator.controls['field_code'].set_value('old')  # Duplicate: application error.
            creator.controls['label'].set_value('Duplicate')
            creator._parameter_changed(None)
            self.assertTrue(creator.add_button.enabled)
            creator._add_command()
            self.assertEqual(len(added), 1)
            self.assertIn('fieldCode="old"', added[0].to_syntax())
        result = added[0].execute(schema())
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.diagnostics[0].code, 'duplicate_field')

    def test_cross_parameter_validation_happens_on_application(self):
        command = AddFieldCommand('new', 'New', field_type='quantity', barcode=True)
        self.assertIn('barcode=True', command.to_syntax())
        result = command.execute(schema())
        self.assertEqual(result.status, 'error')
        self.assertIn('does not apply', result.message)
