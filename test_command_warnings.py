from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from nicegui import ui
from nicegui.client import Client

from activity_manager.commands import AddFieldCommand, MoveFieldCommand, DeleteFieldCommand
from activity_manager.command_list import CommandListManager
from activity_manager.schema_names import unresolved_formula_references
from activity_manager.schema_resolution import resolve_command_schema
from activity_manager.schema_change import apply_form_schema_change
from test_add_field import schema


class FormulaWarningTests(unittest.TestCase):
    def test_functions_literals_numbers_and_resolved_paths_do_not_warn(self):
        source = schema()
        source['elements'].extend([
            {'id': 'refId', 'code': 'REF', 'type': 'reference', 'typeParameters': {
                'range': [{'formId': 'remote'}], 'lookupConfigs': [{'formula': 'NAME + LOOKUP_MISSING'}]}},
            {'id': 'calc', 'typeParameters': {'formula':
                'LEFT(old, 1) == "MISSING" AND REF.NAME != \'UNKNOWN\' AND 1e3 > 2 AND _id != ""'},
             'validationCondition': 'REF.BAD + MISSING + MISSING'},
        ])
        failures = unresolved_formula_references(source, {'remote': {'id': 'remote', 'elements': [
            {'id': 'nameId', 'code': 'NAME'}]}})
        self.assertEqual([token for _, token, _ in failures], ['LOOKUP_MISSING', 'BAD', 'MISSING'])
        self.assertEqual(failures[0][2], 'remote')
        self.assertEqual(failures[1][2], 'remote')


class CommandWarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_warnings_allow_chain_execute_and_keep_missing_commands_as_noops(self):
        async def resolve(data):
            return await resolve_command_schema(SimpleNamespace(), data, {}, {}, {}, {})

        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve, on_execute=AsyncMock())
            source = schema()
            manager.set_schema(source)
            for command in [DeleteFieldCommand('absent'), MoveFieldCommand('absent', 'End'),
                            MoveFieldCommand('old', 'Before field', 'absent'),
                            AddFieldCommand('check', 'Check', validation_condition='UNKNOWN == "1"'),
                            AddFieldCommand('last', 'Last')]:
                manager.add_command(command)
            await manager._evaluation_task
            self.assertEqual([item.status for item in manager.executions], ['warning'] * 5)
            for item in manager.executions[:3]:
                self.assertEqual(item.schema, source)
                self.assertIn('absent', item.message)
                self.assertIn('Warning:', item.message)
                self.assertIn('Command skipped; schema unchanged.', item.message)
            self.assertIn('UNKNOWN', manager.executions[3].message)
            self.assertTrue(manager.execute_button.enabled)
            self.assertEqual(manager.target_schema()['elements'][-1]['code'], 'last')
            icons = [element.props.get('name') for element in manager.simple_content.descendants()
                     if isinstance(element, ui.icon)]
            self.assertIn('warning', icons)
            service = SimpleNamespace(get_form_schema=AsyncMock(side_effect=[source, manager.target_schema()]),
                                      put_resolved_form_schema=AsyncMock())
            # Use a write echo so verification sees the materialized IDs.
            async def write(form_id, target):
                service.get_form_schema.side_effect = None
                service.get_form_schema.return_value = target
            service.put_resolved_form_schema.side_effect = write
            result = await apply_form_schema_change(service, 'form1', source, manager.target_schema(),
                                                    AsyncMock(), resolved=True)
            self.assertEqual(result.status, 'success', result.messages)
            self.assertEqual(service.put_resolved_form_schema.await_args.args[1]['elements'][-2]['validationCondition'],
                             'UNKNOWN == "1"')

    async def test_true_errors_still_stop_following_commands(self):
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(on_execute=AsyncMock())
            manager.set_schema(schema())
            manager.add_command(DeleteFieldCommand('absent'))
            manager.add_command(MoveFieldCommand('old', 'Before field', 'old'))
            manager.add_command(AddFieldCommand('last', 'Last'))
            self.assertEqual([item.status for item in manager.executions], ['warning', 'error', 'not_executed'])
            self.assertFalse(manager.execute_button.enabled)
            self.assertIsNone(manager.target_schema())

    async def test_formula_warning_does_not_hide_default_resolution_error(self):
        command = AddFieldCommand('choice', 'Choice', field_type='enumerated', default_value='Missing option',
            option_list={'cardinality': 'single', 'values': [{'code': 'yes', 'label': 'Yes'}]},
            validation_condition='UNKNOWN == "1"')

        async def resolve(data):
            return await resolve_command_schema(SimpleNamespace(), data, {}, {}, {}, {})

        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve, on_execute=AsyncMock())
            manager.set_schema(schema())
            manager.add_command(command)
            await manager._evaluation_task
            execution = manager.executions[0]
            self.assertEqual(execution.status, 'error')
            self.assertEqual({issue.severity for issue in execution.issues}, {'error', 'warning'})
            self.assertIn('Missing option', execution.message)
            self.assertIn('UNKNOWN', execution.message)
            self.assertFalse(manager.execute_button.enabled)
