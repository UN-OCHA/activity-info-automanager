from copy import deepcopy
import unittest
from unittest.mock import patch

from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands import ModifyFieldCommand
from activity_manager.functions import (
    FunctionArgument, FunctionCall, FunctionDefinition,
)

from activity_manager.catalogs import index_definitions
from activity_manager.functions import catalog


class ParameterFunctionTests(unittest.TestCase):
    def setUp(self):
        self.catalog_patch = patch.object(catalog, 'FUNCTIONS_BY_NAME', index_definitions(()))
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.calls = []

        def append(context, suffix):
            self.calls.append(context)
            return (context.current_value or '') + suffix

        self.add_function(FunctionDefinition('Append', (FunctionArgument('suffix'),),
            frozenset({'validationCondition', 'description', 'units'}), append, commands=frozenset({'ModifyField'})))
        self.source = {'elements': [{'id': 'field1', 'code': 'x', 'label': 'X',
            'type': 'quantity', 'validationCondition': 'old', 'typeParameters': {'units': 'kg'}}]}

    def add_function(self, definition):
        replacement = index_definitions((*catalog.FUNCTIONS_BY_NAME.values(), definition))
        catalog_patch = patch.object(catalog, 'FUNCTIONS_BY_NAME', replacement)
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)

    def parse(self, value, parameter='validationCondition'):
        return parse_command_text(
            f'ModifyField(fieldCode="x", fieldType="quantity", {parameter}={value})')[0]

    def test_parse_roundtrip_does_not_execute(self):
        command = self.parse('Append(" + 1")')
        self.assertEqual(self.calls, [])
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        result = command.execute(self.source)
        self.assertFalse(result.has_errors)
        self.assertEqual(result.schema['elements'][0]['validationCondition'], 'old + 1')
        self.assertEqual(self.calls[0].parameter, 'validationCondition')
        self.assertEqual(self.calls[0].current_value, 'old')
        self.assertEqual(self.source['elements'][0]['validationCondition'], 'old')

    def test_function_names_are_case_insensitive_and_serialize_canonically(self):
        for name in ('Append', 'append', 'APPEND', 'aPpEnD'):
            with self.subTest(name=name):
                command = self.parse(f'{name}("!")')
                self.assertEqual(command.changes['validation_condition'].name, 'Append')
                self.assertIn('Append("!")', command.to_syntax())
                self.assertEqual(command.execute(self.source).schema['elements'][0]['validationCondition'], 'old!')
        # Programmatic calls use the same case-insensitive validation/evaluation.
        command = ModifyFieldCommand('x', 'quantity', changes={
            'validation_condition': FunctionCall('aPpEnD', ['!'])})
        self.assertFalse(command.execute(self.source).has_errors)

    def test_case_only_duplicate_function_is_rejected(self):
        with self.assertRaises(ValueError):
            self.add_function(FunctionDefinition('APPEND', (),
                frozenset({'description'}), lambda context: 'x', commands=frozenset({'ModifyField'})))

    def test_existing_nested_and_missing_values(self):
        result = self.parse('Append("!")', 'units').execute(self.source)
        self.assertEqual(result.schema['elements'][0]['typeParameters']['units'], 'kg!')
        result = self.parse('Append("new")', 'description').execute(self.source)
        self.assertEqual(result.schema['elements'][0]['description'], 'new')
        self.assertIsNone(self.calls[-1].current_value)

    def test_quoted_call_is_text(self):
        command = self.parse(repr('Append("x")'))
        self.assertEqual(command.changes['validation_condition'], 'Append("x")')
        self.assertEqual(self.calls, [])

    def test_reject_unsafe_or_invalid_calls(self):
        for value in ('Unknown("x")', 'Append()', 'Append(1)', 'Append("x", "y")',
                      'Append(suffix="x")', 'module.Append("x")', 'Append(*["x"])',
                      'Append(Append("x"))', 'Append(variable)', 'Append("x" + "y")',
                      'Append(**{"suffix": "x"})'):
            with self.subTest(value=value), self.assertRaises(CommandTextError) as error:
                self.parse(value)
            self.assertEqual(error.exception.line, 1)
            self.assertGreater(error.exception.column, 1)
        for parameter in ('fieldCode', 'required', 'defaultValue', 'label'):
            with self.subTest(parameter=parameter), self.assertRaises(CommandTextError):
                self.parse('Append("x")', parameter)

    def test_literal_containers_roundtrip_and_detachment(self):
        self.add_function(FunctionDefinition('Literal',
            (FunctionArgument('value', (dict,)),), frozenset({'description'}),
            lambda context, value: str(value), commands=frozenset({'ModifyField'})))
        command = self.parse('Literal({"x": [True, None, -2, 1.5]})', 'description')
        call = command.changes['description']
        call.arguments[0]['x'].append('mutation')
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        self.assertEqual(call.arguments[0]['x'], [True, None, -2, 1.5])
        for value in ('Literal({"x": 1, "x": 2})', 'Literal({"x": 1e999})',
                      'Literal({"x": Append("x")})'):
            with self.subTest(value=value), self.assertRaises(CommandTextError):
                self.parse(value, 'description')

    def test_failure_is_atomic_and_identifies_function(self):
        for implementation in (lambda context: 123,
                               lambda context: (_ for _ in ()).throw(RuntimeError('broken'))):
            with self.subTest(implementation=implementation):
                definitions = (FunctionDefinition('Fail', (),
                    frozenset({'validationCondition'}), implementation, commands=frozenset({'ModifyField'})),)
                with patch.object(catalog, 'FUNCTIONS_BY_NAME', index_definitions(definitions)):
                    command = ModifyFieldCommand('x', 'quantity', changes={
                        'description': 'changed', 'validation_condition': FunctionCall('Fail')})
                    original = deepcopy(self.source)
                    result = command.execute(self.source)
                self.assertTrue(result.has_errors)
                self.assertEqual(result.schema, original)
                self.assertEqual(self.source, original)
                self.assertIn('validationCondition', result.message)
                self.assertIn('Fail', result.message)

    def test_missing_target_does_not_evaluate(self):
        command = self.parse('Append("x")')
        result = command.execute({'elements': []})
        self.assertEqual(result.status, 'warning')
        self.assertEqual(self.calls, [])

    def test_multiple_calls_observe_original_values_and_preserve_calls(self):
        command = ModifyFieldCommand('x', 'quantity', changes={
            'description': FunctionCall('Append', ['new']),
            'units': FunctionCall('Append', ['!']),
            'validation_condition': FunctionCall('Append', ['!'])})
        result = command.execute(self.source)
        self.assertFalse(result.has_errors)
        self.assertEqual([context.current_value for context in self.calls], [None, 'kg', 'old'])
        self.assertIsInstance(command.changes['units'], FunctionCall)

    def test_output_validation_and_blank_removal(self):
        self.add_function(FunctionDefinition('Empty', (),
            frozenset({'description', 'label'}), lambda context: '  ', commands=frozenset({'ModifyField'})))
        self.source['elements'][0]['description'] = 'remove me'
        result = self.parse('Empty()', 'description').execute(self.source)
        self.assertNotIn('description', result.schema['elements'][0])
        result = self.parse('Empty()', 'label').execute(self.source)
        self.assertTrue(result.has_errors)
        self.assertEqual(result.schema, self.source)

    def test_catalog_rejects_duplicates_and_calls_validate_programmatically(self):
        with self.assertRaises(ValueError):
            self.add_function(catalog.FUNCTIONS_BY_NAME['append'])
        with self.assertRaises(ValueError):
            ModifyFieldCommand('x', 'quantity', changes={'description': FunctionCall('Missing')})
        with self.assertRaises(ValueError):
            FunctionCall('Append', [object()])


if __name__ == '__main__':
    unittest.main()
