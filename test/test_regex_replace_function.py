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
from activity_manager.functions.regex_replace import REGEX_REPLACE
from activity_manager.functions.replace import REPLACE
from activity_manager.schema_resolution import resolve_command_schema
from test.test_replace_function import command, source_schema


class RegexReplaceTests(unittest.TestCase):
    def test_python_patterns_global_replacement_and_replacement_groups(self):
        for text, pattern, replacement, expected in (
            ('a1 b22 c333', r'\d+', '#', 'a# b# c#'),
            ('old older OLD', r'\bold\b', 'new', 'new older OLD'),
            ('old older OLD', r'(?i)\bold\b', 'new', 'new older new'),
            ('code12 code34', r'code(\d+)', r'field\g<1>', 'field12 field34'),
            ('a1 b22', r'(?P<number>\d+)', r'[\g<number>]', 'a[1] b[22]'),
            ('abc123 def456', r'\d+', '', 'abc def'),
            ('no match', r'\d+', 'new', 'no match'),
            ('/old/g old', '/old/g', 'new', 'new old'),
        ):
            with self.subTest(pattern=pattern):
                result = evaluate_function(FunctionCall('RegexReplace', [pattern, replacement]),
                    FunctionContext('description', text, command='ModifyField'))
                self.assertEqual(result, expected)

    def test_same_permissions_as_replace(self):
        self.assertEqual(REGEX_REPLACE.commands, REPLACE.commands)
        self.assertEqual(REGEX_REPLACE.parameters, REPLACE.parameters)
        for name in ('AddField', 'DeleteField', 'MoveField'):
            with self.subTest(command=name), self.assertRaises(ValueError):
                validate_function(FunctionCall('RegexReplace', ['x', 'y']),
                                  'description', command_name=name)
        for parameter in ('fieldCode', 'fieldType', 'fieldVariant', 'required', 'lookupFields'):
            with self.subTest(parameter=parameter), self.assertRaises(ValueError):
                validate_function(FunctionCall('RegexReplace', ['x', 'y']),
                                  parameter, command_name='ModifyField')

    def test_safe_signature_and_roundtrip(self):
        parsed = command(r'regexreplace(r"\bpopulation\b", "households")')
        self.assertIn('RegexReplace(', parsed.to_syntax())
        self.assertEqual(parsed.changes['validation_condition'].arguments,
                         (r'\bpopulation\b', 'households'))
        self.assertEqual(parse_command_text(parsed.to_syntax()), [parsed])
        for value in ('RegexReplace("x")', 'RegexReplace("x", 1)',
                      'RegexReplace(FindPattern="x", ReplaceText="y")',
                      'RegexReplace(Replace("x", "y"), "z")'):
            with self.subTest(value=value), self.assertRaises(CommandTextError):
                command(value)

    def test_invalid_patterns_and_replacements_are_atomic(self):
        for expression in ('RegexReplace("[", "x")',
                           r'RegexReplace("population", r"\g<missing>")',
                           'RegexReplace("", "x")'):
            with self.subTest(expression=expression):
                parsed = parse_command_text('ModifyField(fieldCode="target", fieldType="quantity", '
                    f'description="changed", validationCondition={expression})')[0]
                source = source_schema()
                original = deepcopy(source)
                result = parsed.execute(source)
                self.assertTrue(result.has_errors)
                self.assertIn('RegexReplace', result.message)
                self.assertIn('validationCondition', result.message)
                self.assertEqual(result.schema, original)
                self.assertEqual(source, original)

    def test_missing_value_errors_and_empty_output_removes_parameter(self):
        source = source_schema()
        source['elements'][0].pop('description')
        result = command('RegexReplace("old", "new")', 'description').execute(source)
        self.assertTrue(result.has_errors)
        self.assertEqual(result.schema, source)
        result = command('RegexReplace(".*", "")', 'description').execute(source_schema())
        self.assertFalse(result.has_errors, result.message)
        self.assertNotIn('description', result.schema['elements'][0])

    def test_creator_recognizes_regex_calls(self):
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((MODIFY_FIELD,))
            creator.controls['field_code'].set_value('target')
            creator.controls['field_type'].set_value('quantity')
            creator._parameter_changed(None)
            creator.activations['new_code'].set_value(True)
            creator.controls['new_code'].set_value(r'RegexReplace(r"_\d+$", "")')
            creator._parameter_changed(None)
            self.assertFalse(creator.add_button.props.get('disable', False))
            parsed = MODIFY_FIELD.create_command(creator.values())
        self.assertEqual(parsed.changes['new_code'], FunctionCall('RegexReplace', [r'_\d+$', '']))
        self.assertEqual(parse_command_text(parsed.to_syntax()), [parsed])


class RegexReplacePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_names_conversion_and_existing_resolution_warnings(self):
        async def resolver(candidate):
            return await resolve_command_schema(SimpleNamespace(), candidate, {}, {}, {}, {})

        for replacement, expected, status in (
            ('households', 'newid > 0 AND newid < 100', 'applied'),
            ('missing', 'missing > 0 AND missing < 100', 'warning'),
        ):
            with self.subTest(replacement=replacement):
                source = source_schema()
                source['elements'][0]['validationCondition'] = 'oldid > 0 AND oldid < 100'
                original = deepcopy(source)
                parsed = command(f'RegexReplace(r"\\bpopulation\\b", "{replacement}")')
                result = await apply_command(parsed, source, resolver)
                self.assertEqual(result.status, status, result.message)
                self.assertEqual(result.schema['elements'][0]['validationCondition'], expected)
                self.assertEqual(source, original)


if __name__ == '__main__':
    unittest.main()
