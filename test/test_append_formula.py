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
from activity_manager.functions.append_formula import APPEND_FORMULA
from activity_manager.schema_resolution import resolve_command_schema
from test.test_replace_function import command, source_schema


class AppendFormulaTests(unittest.TestCase):
    def evaluate(self, current, operator='AND', formula='B'):
        return evaluate_function(FunctionCall('AppendFormula', [operator, formula]),
            FunctionContext('validationCondition', current, command='ModifyField'))

    def test_empty_current_gets_formula_directly(self):
        for current in (None, '', '  \n'):
            with self.subTest(current=current):
                self.assertEqual(self.evaluate(current, formula='  A > 0 '), '  A > 0 ')

    def test_append_and_presence_preserve_original_text(self):
        for current, operator, formula, expected in (
            (' A ', 'and', ' B ', '( A ) && ( B )'),
            ('A', 'oR', 'B', '(A) || (B)'),
            (' B && A ', 'AND', '(A)', ' B && A '),
            ('A && B && C', 'AND', 'B && A', 'A && B && C'),
            ('A || B', 'AND', 'A', '(A || B) && (A)'),
            ('A && B', 'OR', 'A', '(A && B) || (A)'),
            ('A && B', 'OR', 'B && A', 'A && B'),
            (' ((A) > (0)) ', 'AND', 'A>0', ' ((A) > (0)) '),
            (' ((A) > (0)) ', 'OR', 'A>0', ' ((A) > (0)) '),
        ):
            with self.subTest(current=current, formula=formula):
                self.assertEqual(self.evaluate(current, operator, formula), expected)

    def test_invalid_arguments_or_formulas_error(self):
        for current, operator, formula in (
            ('A', 'XOR', 'B'), ('A', '&&', 'B'), ('A', 'AND', ' '),
            ('A &&', 'AND', 'B'), ('A', 'AND', 'B ||'), (None, 'OR', 'B ||'),
        ):
            with self.subTest(current=current, operator=operator, formula=formula), self.assertRaises(ValueError):
                self.evaluate(current, operator, formula)

    def test_permissions_and_safe_signature(self):
        self.assertEqual(APPEND_FORMULA.commands, frozenset({'ModifyField', 'AddModifyField'}))
        self.assertEqual(APPEND_FORMULA.parameters, frozenset({
            'validationCondition', 'relevanceCondition', 'requiredCondition',
            'calculationFormula', 'defaultFormula', 'prefixFormula'}))
        call = FunctionCall('AppendFormula', ['AND', 'A'])
        for parameter in ('newCode', 'description', 'label', 'defaultValue', 'lookupFields'):
            with self.subTest(parameter=parameter), self.assertRaises(ValueError):
                validate_function(call, parameter, command_name='ModifyField')
        with self.assertRaises(ValueError):
            validate_function(call, 'validationCondition', command_name='AddField')
        for expression in ('AppendFormula("AND")', 'AppendFormula(1, "A")',
                           'AppendFormula(Operator="AND", Formula="A")'):
            with self.subTest(expression=expression), self.assertRaises(CommandTextError):
                command(expression)

    def test_creator_and_serialization(self):
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((MODIFY_FIELD,))
            creator.controls['field_code'].set_value('target')
            creator.controls['field_type'].set_value('quantity')
            creator._parameter_changed(None)
            creator.activations['validation_condition'].set_value(True)
            creator.controls['validation_condition'].set_value('appendformula("AND", "households > 0")')
            creator._parameter_changed(None)
            self.assertFalse(creator.add_button.props.get('disable', False))
            parsed = MODIFY_FIELD.create_command(creator.values())
            self.assertEqual(parse_command_text(parsed.to_syntax()), [parsed])
            creator.activations['description'].set_value(True)
            creator.controls['description'].set_value('AppendFormula("AND", "A")')
            creator._parameter_changed(None)
            self.assertTrue(creator.add_button.props['disable'])


class AppendFormulaPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, parsed, source, related=None):
        related = related or {}

        async def resolver(candidate):
            return await resolve_command_schema(SimpleNamespace(), candidate, related, {}, {}, {})

        return await apply_command(parsed, source, resolver, related_schemas=related)

    async def test_names_based_presence_append_and_repeat(self):
        source = source_schema()
        original = deepcopy(source)
        existing = await self.apply(command('AppendFormula("AND", "((population) > (0))")'), source)
        self.assertEqual(existing.status, 'applied', existing.message)
        self.assertEqual(existing.schema, source)
        parsed = command('AppendFormula("AND", "households > 0")')
        result = await self.apply(parsed, source)
        self.assertEqual(result.status, 'applied', result.message)
        self.assertEqual(result.schema['elements'][0]['validationCondition'], '(oldid > 0) && (newid > 0)')
        repeated = await self.apply(parsed, result.schema)
        self.assertEqual(repeated.schema, result.schema)
        self.assertEqual(source, original)

    async def test_absent_calculated_formula_and_default_formula(self):
        for parameter, kind, location in (
            ('calculationFormula', 'calculated', 'formula'),
            ('defaultFormula', 'quantity', 'defaultValueFormula'),
        ):
            with self.subTest(parameter=parameter):
                source = source_schema()
                source['elements'][0]['type'] = kind
                result = await self.apply(command('AppendFormula("OR", "households > 0")',
                    parameter=parameter, field_type=kind), source)
                self.assertFalse(result.has_errors, result.message)
                target = result.schema['elements'][0]
                value = target['typeParameters'][location] if kind == 'calculated' else target[location]
                self.assertEqual(value, 'newid > 0')

    async def test_failures_are_atomic_and_unresolved_references_warn(self):
        for current, formula in (('oldid &&', 'households > 0'), ('oldid > 0', 'households &&')):
            with self.subTest(current=current):
                source = source_schema()
                source['elements'][0]['validationCondition'] = current
                parsed = parse_command_text('ModifyField(fieldCode="target", fieldType="quantity", '
                    f'description="changed", validationCondition=AppendFormula("AND", "{formula}"))')[0]
                result = await self.apply(parsed, source)
                self.assertTrue(result.has_errors)
                self.assertIn('AppendFormula', result.message)
                self.assertEqual(result.schema, source)
        result = await self.apply(command('AppendFormula("AND", "missing > 0")'), source_schema())
        self.assertEqual(result.status, 'warning')
        self.assertIn('missing', result.message)

    async def test_related_form_names_are_used_for_presence(self):
        source = source_schema()
        source['elements'].append({'id': 'refid', 'code': 'people', 'label': 'People',
            'type': 'reference', 'typeParameters': {'range': [{'formId': 'remote'}]}})
        source['elements'][0]['validationCondition'] = 'refid.remoteid > 0'
        related = {'remote': {'id': 'remote', 'elements': [
            {'id': 'remoteid', 'code': 'count', 'type': 'quantity'}]}}
        result = await self.apply(command('AppendFormula("AND", "people.count > 0")'), source, related)
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(result.schema, source)


if __name__ == '__main__':
    unittest.main()
