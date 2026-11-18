from copy import deepcopy
from types import SimpleNamespace
import unittest

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_execution import apply_command
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands.modify_field import FUNCTION_PARAMETERS
from activity_manager.functions import FunctionCall, FunctionContext, evaluate_function
from activity_manager.functions import validate_function
from activity_manager.functions.replace import REPLACE
from activity_manager.schema_resolution import resolve_command_schema


def source_schema():
    return {'id': 'form', 'elements': [
        {'id': 'targetid', 'code': 'target', 'label': 'Target', 'type': 'quantity',
         'validationCondition': 'oldid > 0', 'description': 'old old OLD',
         'typeParameters': {'units': 'kg'}},
        {'id': 'oldid', 'code': 'population', 'label': 'Population', 'type': 'quantity'},
        {'id': 'newid', 'code': 'households', 'label': 'Households', 'type': 'quantity'},
    ]}


def command(expression='Replace("population", "households")', parameter='validationCondition',
            field_type='quantity'):
    return parse_command_text(f'ModifyField(fieldCode="target", fieldType="{field_type}", '
                              f'{parameter}={expression})')[0]


class ReplaceTests(unittest.TestCase):
    def test_command_and_parameter_permissions(self):
        call = FunctionCall('Replace', ['old', 'new'])
        self.assertEqual(REPLACE.commands, frozenset({'ModifyField'}))
        validate_function(call, 'description', command_name='mOdIfYfIeLd')
        for name in ('AddField', 'MoveField', 'DeleteField', 'OtherCommand'):
            with self.subTest(command=name), self.assertRaises(ValueError):
                validate_function(call, 'description', command_name=name)
        for name in ('fieldCode', 'fieldType', 'fieldVariant', 'required', 'geoAccuracy',
                     'optionList', 'lookupFields', 'lookupFields.formula'):
            with self.subTest(parameter=name), self.assertRaises(ValueError):
                validate_function(call, name, command_name='ModifyField')
        for arguments in (
            'fieldCode=Replace("x", "y"), fieldType="quantity"',
            'fieldCode="x", fieldType=Replace("x", "y")',
            'fieldCode="x", fieldType="FREE_TEXT", fieldVariant=Replace("x", "y")',
            'fieldCode="x", fieldType="quantity", required=Replace("x", "y")',
            'fieldCode="x", fieldType="section", indentationLevel=Replace("x", "y")',
            'fieldCode="x", fieldType="reference", fieldVariant="reference", '
                'lookupFields=[{"label": "Name", "formula": Replace("x", "y")}]',
            'fieldCode="x", fieldType="enumerated", fieldVariant="single", '
                'optionList={"values": [{"label": Replace("x", "y")}]}',
        ):
            with self.subTest(arguments=arguments), self.assertRaises(CommandTextError):
                parse_command_text('ModifyField(' + arguments + ')')
        with self.assertRaises(CommandTextError):
            parse_command_text('AddField(fieldCode="x", fieldType="quantity", label=Replace("x", "y"))')

    def test_literal_matching_contract(self):
        for text, find, replacement, expected in (
            ('old old OLD', 'old', 'new', 'new new OLD'),
            ('aaaa', 'aa', 'b', 'bb'),
            ('a.b aXb', '.', '*', 'a*b aXb'),
            ('a\\b', '\\', '/', 'a/b'),
            ('nothing', 'missing', 'new', 'nothing'),
            ('remove this', 'remove ', '', 'this'),
            ('', 'x', 'y', ''),
        ):
            with self.subTest(text=text):
                result = evaluate_function(FunctionCall('Replace', [find, replacement]),
                    FunctionContext('description', text, command='ModifyField'))
                self.assertEqual(result, expected)

    def test_signature_and_case_insensitive_roundtrip(self):
        parsed = command('rEpLaCe("population", "households")')
        self.assertIn('Replace("population", "households")', parsed.to_syntax())
        self.assertEqual(parse_command_text(parsed.to_syntax()), [parsed])
        for expression in ('Replace()', 'Replace("x")', 'Replace("x", "y", "z")',
                           'Replace(1, "x")', 'Replace("x", None)',
                           'Replace(FindText="x", ReplaceText="y")'):
            with self.subTest(expression=expression), self.assertRaises(CommandTextError):
                command(expression)
        self.assertEqual(REPLACE.parameters, frozenset(FUNCTION_PARAMETERS.values()))

    def test_errors_leave_all_edits_atomic(self):
        for expression, remove_current in (('Replace("", "x")', False),
                                           ('Replace("population", "x")', True)):
            with self.subTest(expression=expression):
                source = source_schema()
                if remove_current:
                    source['elements'][0].pop('validationCondition')
                parsed = parse_command_text('ModifyField(fieldCode="target", fieldType="quantity", '
                    f'description="changed", validationCondition={expression})')[0]
                original = deepcopy(source)
                result = parsed.execute(source)
                self.assertTrue(result.has_errors)
                self.assertIn('Replace', result.message)
                self.assertIn('validationCondition', result.message)
                self.assertEqual(result.schema, original)
                self.assertEqual(source, original)

    def test_empty_result_removes_optional_parameter(self):
        source = source_schema()
        result = command('Replace("old old OLD", "")', 'description').execute(source)
        self.assertFalse(result.has_errors)
        self.assertNotIn('description', result.schema['elements'][0])

    def test_all_functions_read_pre_edit_values(self):
        source = source_schema()
        parsed = parse_command_text('ModifyField(fieldCode="population", fieldType="quantity", '
            'newCode="renamed", description=Replace("x", "y"))')[0]
        # Missing description fails even alongside a valid rename.
        self.assertTrue(parsed.execute(source).has_errors)
        parsed = parse_command_text('ModifyField(fieldCode="target", fieldType="quantity", '
            'newCode="renamed", validationCondition=Replace("population", "households"), '
            'description=Replace("old", "new"))')[0]
        result = parsed.execute(source)
        self.assertEqual(result.schema['elements'][0]['validationCondition'], 'households > 0')
        self.assertEqual(result.schema['elements'][0]['description'], 'new new OLD')
        self.assertEqual(source, source_schema())


class ReplacePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, parsed, source, related=None):
        related = related or {}

        async def resolver(candidate):
            return await resolve_command_schema(SimpleNamespace(), candidate, related, {}, {}, {})

        return await apply_command(parsed, source, resolver, related_schemas=related)

    async def test_ids_to_names_replace_and_back_to_ids(self):
        source = source_schema()
        result = await self.apply(command(), source)
        self.assertEqual(result.status, 'applied', result.message)
        self.assertEqual(result.schema['elements'][0]['validationCondition'], 'newid > 0')
        self.assertEqual(result.names_schema['elements'][0]['validationCondition'], 'households > 0')
        self.assertEqual(source, source_schema())

    async def test_no_match_resolves_original_names_back_to_original_ids(self):
        result = await self.apply(command('Replace("absent", "new")'), source_schema())
        self.assertEqual(result.status, 'applied', result.message)
        self.assertEqual(result.schema['elements'][0]['validationCondition'], 'oldid > 0')

    async def test_calculated_formula_and_default_formula(self):
        for parameter, field_type, container, wire in (
            ('calculationFormula', 'calculated', 'typeParameters', 'formula'),
            ('defaultFormula', 'quantity', None, 'defaultValueFormula'),
        ):
            with self.subTest(parameter=parameter):
                source = source_schema()
                field = source['elements'][0]
                field['type'] = field_type
                (field[container] if container else field)[wire] = 'oldid + 1'
                result = await self.apply(command(parameter=parameter, field_type=field_type), source)
                self.assertFalse(result.has_errors, result.message)
                target = result.schema['elements'][0]
                self.assertEqual((target[container] if container else target)[wire], 'newid + 1')

    async def test_ordinary_text_and_quoted_formula_contents(self):
        result = await self.apply(command('Replace("old", "new")', 'description'), source_schema())
        self.assertEqual(result.schema['elements'][0]['description'], 'new new OLD')
        source = source_schema()
        source['elements'][0]['validationCondition'] = 'oldid > 0 AND "population" == "oldid"'
        result = await self.apply(command(), source)
        self.assertEqual(result.schema['elements'][0]['validationCondition'],
                         'newid > 0 AND "households" == "oldid"')

    async def test_text_default_and_nontext_default_rejection(self):
        source = source_schema()
        source['elements'][0].update(type='FREE_TEXT', defaultValue='old value')
        parsed = parse_command_text('ModifyField(fieldCode="target", fieldType="FREE_TEXT", '
            'fieldVariant="text", defaultValue=Replace("old", "new"))')[0]
        result = await self.apply(parsed, source)
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(result.schema['elements'][0]['defaultValue'], 'new value')
        source = source_schema()
        source['elements'][0]['defaultValue'] = 123
        result = await self.apply(command('Replace("1", "2")', 'defaultValue'), source)
        self.assertTrue(result.has_errors)
        self.assertEqual(result.schema, source)

    async def test_option_default_is_converted_to_label_before_replacement(self):
        source = source_schema()
        source['elements'][0].update(type='enumerated', defaultValue='optionold',
            typeParameters={'cardinality': 'single', 'values': [
                {'id': 'optionold', 'label': 'Old option'},
                {'id': 'optionnew', 'label': 'New option'}]})
        parsed = parse_command_text('ModifyField(fieldCode="target", fieldType="enumerated", '
            'fieldVariant="single", defaultValue=Replace("Old", "New"))')[0]
        result = await self.apply(parsed, source)
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(result.schema['elements'][0]['defaultValue'], 'optionnew')

    async def test_translation_target_and_languages(self):
        source = source_schema()
        for field in source['elements']:
            field['type'] = 'FREE_TEXT'
        source['elements'][0]['typeParameters']['translationConfig'] = {
            'fieldId': 'oldid', 'languages': ['en', 'fr']}
        parsed = parse_command_text('ModifyField(fieldCode="target", fieldType="FREE_TEXT", '
            'fieldVariant="text", translationField=Replace("population", "households"), '
            'translationLanguage=Replace("fr", "de"))')[0]
        self.assertEqual(parse_command_text(parsed.to_syntax()), [parsed])
        result = await self.apply(parsed, source)
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(result.schema['elements'][0]['typeParameters']['translationConfig'],
                         {'fieldId': 'newid', 'languages': ['en', 'de']})
        self.assertEqual(source['elements'][0]['typeParameters']['translationConfig'],
                         {'fieldId': 'oldid', 'languages': ['en', 'fr']})

    async def test_unresolved_replacement_keeps_existing_warning_behavior(self):
        result = await self.apply(command('Replace("population", "missing")'), source_schema())
        self.assertEqual(result.status, 'warning')
        self.assertEqual(result.schema['elements'][0]['validationCondition'], 'missing > 0')
        self.assertIn('missing', result.message)

    async def test_command_list_passes_related_context_and_chains_ids(self):
        source = source_schema()
        source['elements'].append({'id': 'refid', 'code': 'people', 'label': 'People',
            'type': 'reference', 'typeParameters': {'range': [{'formId': 'remote'}]}})
        source['elements'][0]['validationCondition'] = 'refid.remoteold > 0'
        related = {'remote': {'id': 'remote', 'elements': [
            {'id': 'remoteold', 'code': 'population', 'label': 'Population', 'type': 'quantity'},
            {'id': 'remotenew', 'code': 'households', 'label': 'Households', 'type': 'quantity'},
        ]}}

        async def resolver(candidate):
            return await resolve_command_schema(SimpleNamespace(), candidate, related, {}, {}, {})

        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolver)
            manager.related_schemas = related
            manager.set_schema(source)
            manager.add_command(command())
            await manager._evaluation_task
            self.assertEqual(manager.target_schema()['elements'][0]['validationCondition'],
                             'refid.remotenew > 0')
            manager.add_command(command('Replace("households", "population")'))
            await manager._evaluation_task
            self.assertEqual(manager.target_schema()['elements'][0]['validationCondition'],
                             'refid.remoteold > 0')


if __name__ == '__main__':
    unittest.main()
