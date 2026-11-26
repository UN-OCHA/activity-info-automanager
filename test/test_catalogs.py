import ast
from dataclasses import FrozenInstanceError, replace
import unittest

from activity_manager.catalogs import index_definitions
from activity_manager.commands import COMMANDS, COMMANDS_BY_NAME, COMMAND_CREATORS
from activity_manager.command_parser import parse_command_text
from activity_manager.functions import FUNCTIONS, FunctionArgument, FunctionDefinition
from activity_manager.functions.catalog import FUNCTIONS_BY_NAME


class CatalogTests(unittest.TestCase):
    def test_catalogs_and_indexes_are_immutable(self):
        self.assertIsInstance(COMMANDS, tuple)
        self.assertIsInstance(FUNCTIONS, tuple)
        for index in (COMMANDS_BY_NAME, FUNCTIONS_BY_NAME):
            with self.assertRaises(TypeError):
                index['extra'] = COMMANDS[0]
        with self.assertRaises(FrozenInstanceError):
            COMMANDS[0].name = 'Other'

    def test_ui_and_parser_derive_from_the_same_catalog(self):
        self.assertEqual(COMMAND_CREATORS, tuple(definition.creator for definition in COMMANDS))
        self.assertEqual(tuple(COMMANDS_BY_NAME.values()), COMMANDS)
        self.assertEqual([creator.key for creator in COMMAND_CREATORS],
                         ['move_field', 'delete_field', 'add_field', 'modify_field', 'add_modify_field',
                          'set_variable', 'with_database', 'with_form', 'modify_form', 'move_form', 'add_form', 'add_modify_form',
                          'delete_form'])
        for definition in COMMANDS:
            self.assertIs(COMMANDS_BY_NAME[definition.name.casefold()], definition)
        # Exercise dispatch for every catalog entry, including mixed-case names.
        examples = (
            'WithForm(form="People")',
            'SetVariable(name="x", method="literal", value="X")',
            'MoveField(fieldCode="x", position="Before field", beforeAfterField="y")',
            'DeleteField(fieldCode="x")',
            'AddField(fieldCode="x", fieldType="quantity", label="X")',
            'ModifyField(fieldCode="x", fieldType="quantity", label="X")',
        )
        for text in examples:
            name, rest = text.split('(', 1)
            with self.subTest(name=name):
                self.assertEqual(parse_command_text(name.swapcase() + '(' + rest),
                                 parse_command_text(text))

    def test_duplicate_names_and_invalid_names_are_rejected(self):
        original = COMMANDS[0]
        for duplicate in (original, replace(original, name=original.name.swapcase())):
            with self.assertRaises(ValueError):
                index_definitions((original, duplicate))
        for name in ('not.a.name', 'class', ''):
            with self.assertRaises(ValueError):
                index_definitions((replace(original, name=name),))

    def test_named_parameters_are_order_independent_across_the_catalog(self):
        examples = {
            'MoveField': 'fieldCode="x", position="Before field", beforeAfterField="y"',
            'DeleteField': 'fieldPattern="x.*"',
            'AddField': 'fieldCode="x", label="true", fieldType="quantity", required=True, readOnly=False',
            'ModifyField': 'fieldPattern="x.*", fieldType="quantity", required=False, readOnly=True',
            'AddModifyField': 'fieldCode="x", label="X", fieldType="quantity", required=True, readOnly=False',
            'SetVariable': 'name="x", method="data lookup", multiple=True, referencedForm="X", '
                           'filter="true", returnFormula="CODE", separator=""',
            'WithDatabase': 'databasePattern="X.*"',
            'WithForm': 'form="Database/X"',
            'ModifyForm': 'currentForm=True, name="New"',
            'MoveForm': 'currentForm=False, form="X", folder="/Archive"',
            'AddForm': 'name="X", folder="/Archive"',
            'AddModifyForm': 'name="X", folder="/Archive"',
            'DeleteForm': 'currentForm=False, form="X"',
        }
        self.assertEqual(set(examples), {definition.name for definition in COMMANDS})
        for name, arguments in examples.items():
            with self.subTest(command=name):
                syntax = f'{name}({arguments})'
                expected, = parse_command_text(syntax)
                serialized = expected.to_syntax()
                self.assertEqual(parse_command_text(serialized), [expected])
                call = ast.parse(syntax, mode='eval').body
                reordered = []
                for keyword in reversed(call.keywords):
                    value = ast.get_source_segment(syntax, keyword.value)
                    if isinstance(keyword.value, ast.Constant) and type(keyword.value.value) is bool:
                        value = value.lower()
                    reordered.append(f'{keyword.arg}={value}')
                self.assertEqual(parse_command_text(f'{name}({", ".join(reordered)})'), [expected])

    def test_function_definition_copies_mutable_metadata(self):
        types = [str]
        arguments = [FunctionArgument('text', types)]
        parameters = {'description'}
        commands = {'ModifyField'}
        definition = FunctionDefinition('Example', arguments, parameters, lambda context, text: text, commands=commands)
        types.append(int)
        arguments.clear()
        parameters.clear()
        commands.clear()
        self.assertEqual(definition.arguments, (FunctionArgument('text', (str,)),))
        self.assertEqual(definition.parameters, frozenset({'description'}))
        self.assertEqual(definition.commands, frozenset({'ModifyField'}))
        with self.assertRaises(FrozenInstanceError):
            definition.name = 'Other'


if __name__ == '__main__':
    unittest.main()
