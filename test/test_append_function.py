from copy import deepcopy
from types import SimpleNamespace
import unittest

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_execution import apply_command
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands import MODIFY_FIELD
from activity_manager.functions import FunctionCall, FunctionContext, evaluate_function, validate_function
from activity_manager.functions.append import APPEND
from activity_manager.functions.append_formula import APPEND_FORMULA
from activity_manager.functions.replace import REPLACE
from activity_manager.schema_resolution import resolve_command_schema
from test.test_replace_function import command, source_schema


class AppendTests(unittest.TestCase):
    def evaluate(self, current, position='End', separator=' - ', add_text='Added'):
        return evaluate_function(FunctionCall('Append', [position, separator, add_text]),
            FunctionContext('description', current, command='ModifyField'))

    def test_missing_and_empty_current_ignore_separator(self):
        for current in (None, ''):
            for position in ('Start', 'End'):
                with self.subTest(current=current, position=position):
                    self.assertEqual(self.evaluate(current, position), 'Added')

    def test_boundary_checks_are_case_insensitive_and_separator_sensitive(self):
        for current, position, separator, added, expected in (
            ('Value', 'sTaRt', ' - ', 'Added', 'Added - Value'),
            ('Value', 'eND', ' - ', 'Added', 'Value - Added'),
            ('aDDeD', 'Start', ' - ', 'Added', 'aDDeD'),
            ('aDDeD', 'End', ' - ', 'Added', 'aDDeD'),
            ('aDDeD - Value', 'Start', ' - ', 'Added', 'aDDeD - Value'),
            ('Value - aDDeD', 'End', ' - ', 'Added', 'Value - aDDeD'),
            ('Added Value', 'Start', ' - ', 'Added', 'Added - Added Value'),
            ('Value Added', 'End', ' - ', 'Added', 'Value Added - Added'),
            ('Value - Added - extra', 'End', ' - ', 'Added', 'Value - Added - extra - Added'),
            ('extra Added - Value', 'Start', ' - ', 'Added', 'Added - extra Added - Value'),
            (' ADDED - Value', 'Start', ' - ', 'Added', 'Added -  ADDED - Value'),
            ('Value - Added ', 'End', ' - ', 'Added', 'Value - Added  - Added'),
            ('value', 'Start', '', 'PRE', 'PREvalue'),
            ('valuePOST', 'End', '', 'post', 'valuePOST'),
            (' ', 'End', '/', 'Added', ' /Added'),
            ('value', 'End', '', '', 'value'),
            ('value', 'End', '/', '', 'value/'),
        ):
            with self.subTest(current=current, position=position):
                self.assertEqual(self.evaluate(current, position, separator, added), expected)

    def test_permissions_and_signature(self):
        self.assertEqual(APPEND.commands, frozenset({'ModifyField', 'AddModifyField'}))
        self.assertEqual(APPEND.parameters, REPLACE.parameters - APPEND_FORMULA.parameters)
        call = FunctionCall('Append', ['End', ' ', 'text'])
        for parameter in (*APPEND_FORMULA.parameters, 'fieldCode', 'fieldType', 'fieldVariant', 'lookupFields'):
            with self.subTest(parameter=parameter), self.assertRaises(ValueError):
                validate_function(call, parameter, command_name='ModifyField')
        with self.assertRaises(ValueError):
            validate_function(call, 'description', command_name='AddField')
        for expression in ('Append("End", " ")', 'Append("End", " ", 1)',
                           'Append("End", 1, "text")', 'Append(1, " ", "text")',
                           'Append(Position="End", Separator=" ", AddText="text")'):
            with self.subTest(expression=expression), self.assertRaises(CommandTextError):
                command(expression, 'description')
        with self.assertRaises(ValueError):
            self.evaluate('', 'Middle')

    def test_creator_and_roundtrip(self):
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((MODIFY_FIELD,))
            creator.controls['field_code'].set_value('target')
            creator.controls['field_type'].set_value('quantity')
            creator._parameter_changed(None)
            creator.activations['description'].set_value(True)
            creator.controls['description'].set_value('aPpEnD("End", " - ", "Added")')
            creator._parameter_changed(None)
            self.assertFalse(creator.add_button.props.get('disable', False))
            parsed = MODIFY_FIELD.create_command(creator.values())
        self.assertEqual(parsed.changes['description'], FunctionCall('Append', ['End', ' - ', 'Added']))
        self.assertEqual(parse_command_text(parsed.to_syntax()), [parsed])


class AppendPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, parsed, source):
        async def resolver(candidate):
            return await resolve_command_schema(SimpleNamespace(), candidate, {}, {}, {}, {})
        return await apply_command(parsed, source, resolver)

    async def test_append_repeated_application_and_missing_description(self):
        source = source_schema()
        original = deepcopy(source)
        parsed = command('Append("End", " - ", "Added")', 'description')
        result = await self.apply(parsed, source)
        self.assertEqual(result.status, 'applied', result.message)
        self.assertEqual(result.schema['elements'][0]['description'], 'old old OLD - Added')
        repeated = await self.apply(parsed, result.schema)
        self.assertEqual(repeated.schema, result.schema)
        self.assertEqual(source, original)
        source['elements'][0].pop('description')
        result = await self.apply(parsed, source)
        self.assertEqual(result.schema['elements'][0]['description'], 'Added')

    async def test_invalid_position_is_atomic(self):
        source = source_schema()
        parsed = parse_command_text('ModifyField(fieldCode="target", fieldType="quantity", '
            'label="Changed", description=Append("Middle", " ", "Added"))')[0]
        result = await self.apply(parsed, source)
        self.assertTrue(result.has_errors)
        self.assertIn('Append', result.message)
        self.assertIn('Position', result.message)
        self.assertEqual(result.schema, source)


if __name__ == '__main__':
    unittest.main()
