import unittest
from unittest.mock import AsyncMock

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_execution import ExecutionResult, apply_command, apply_command_locally
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands.set_variable import SET_VARIABLE, SetVariableCommand, query_variable
from test.test_command_iteration import manager, source


class VariableTests(unittest.TestCase):
    def test_variable_selectors_for_modify_move_and_delete(self):
        with Client(ui.page('/')), ui.column():
            instance = manager('''SetVariable(name="target", method="literal", value="anchor")
ModifyField(fieldCode="${vars.target}", fieldType="quantity", label="Changed")
MoveField(fieldCode="${vars.target}", position="End")
DeleteField(fieldCode="${vars.target}")''')
            self.assertEqual([e.status for e in instance.executions], ['applied'] * 4)
            self.assertEqual(instance.executions[1].schema['elements'][0]['label'], 'Changed')
            self.assertEqual(instance.executions[2].schema['elements'][-1]['code'], 'anchor')
            self.assertNotIn('anchor', [f['code'] for f in instance.target_schema()['elements']])

    def test_pattern_variables_resolve_before_captures_and_reassignment_reselects(self):
        with Client(ui.page('/')), ui.column():
            instance = manager(r'''SetVariable(name="years", method="literal", value="202[45]")
ModifyField(fieldPattern="cost_(?P<year>${vars.years})", fieldType="quantity", label="Year ${field.year}")
SetVariable(name="years", method="literal", value="2025")
DeleteField(fieldPattern="cost_${vars.years}")''')
            self.assertEqual([e.status for e in instance.executions], ['applied'] * 4)
            self.assertEqual(len(instance.executions[1].iterations), 2)
            self.assertEqual(instance.target_schema()['elements'][1]['label'], 'Year 2024')
            self.assertEqual(instance.executions[3].iterations[0].field_code, 'cost_2025')
            self.assertIn('${vars.years}', instance.commands[1].to_syntax())

    def test_missing_or_invalid_selector_variables_stop_chain(self):
        for text in [
            'DeleteField(fieldCode="${vars.missing}")',
            'DeleteField(fieldPattern="${vars.missing}")',
            'SetVariable(name="p", method="literal", value="[")\nDeleteField(fieldPattern="${vars.p}")',
            'SetVariable(name="p", method="literal", value="(?P<code>.*)")\nDeleteField(fieldPattern="${vars.p}")',
        ]:
            with self.subTest(text=text), Client(ui.page('/')), ui.column():
                instance = manager(text + '\nDeleteField(fieldCode="last")')
                self.assertEqual(instance.executions[-2].status, 'error')
                self.assertEqual(instance.executions[-1].status, 'not_executed')
                self.assertEqual(instance.executions[-2].schema, source())

    def test_selector_escapes_and_inserted_values_are_not_expanded_again(self):
        data = source()
        data['elements'][0]['code'] = '${field.code}'
        for selector in ('${vars.target}', '$${field.code}'):
            with self.subTest(selector=selector), Client(ui.page('/')), ui.column():
                instance = manager('SetVariable(name="target", method="literal", value="$${field.code}")\n'
                    f'DeleteField(fieldCode="{selector}")', schema=data)
                self.assertEqual(instance.executions[-1].status, 'applied')
                self.assertEqual(len(instance.target_schema()['elements']), 4)
        data['elements'][0]['code'] = '${vars.undefined}'
        with Client(ui.page('/')), ui.column():
            instance = manager('''SetVariable(name="pattern", method="literal", value=".*")
ModifyField(fieldPattern="${vars.pattern}", fieldType="quantity", label="Changed")''', schema=data)
            self.assertEqual(instance.executions[-1].status, 'applied')
            self.assertEqual(instance.target_schema()['elements'][0]['label'], 'Changed')

    def test_creator_accepts_variable_selectors_and_rejects_iteration_selectors(self):
        from activity_manager.commands import MODIFY_FIELD
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((MODIFY_FIELD,), on_add=added.append)
            creator.controls['field_code'].set_value('${vars.target}')
            creator.controls['field_type'].set_value('quantity')
            creator._parameter_changed(None)
            creator._add_command()
            self.assertEqual(added[-1].field_code, '${vars.target}')
            creator.target_mode.set_value('Multiple')
            creator._target_mode_changed(None)
            creator.controls['field_code'].set_value('(?${vars.group})')
            creator._parameter_changed(None)
            creator._add_command()
            self.assertEqual(added[-1].field_pattern, '(?${vars.group})')
            creator.controls['field_code'].set_value('${field.code}')
            creator._parameter_changed(None)
            self.assertFalse(creator.add_button.enabled)
            self.assertIn('references are allowed', creator.error.text)

    def test_literal_validation_and_roundtrip(self):
        for value in (' text ', 0, False, 2.5):
            command = SetVariableCommand('_value2', 'literal', value)
            variables = {}
            result = apply_command_locally(command, source(), variables=variables)
            self.assertEqual(result.status, 'applied')
            self.assertEqual(result.schema, source())
            self.assertEqual(variables, {'vars._value2': str(value)})
            self.assertIn('Variable vars._value2 set to', result.message)
            self.assertIn(str(value), result.message)
            self.assertEqual(parse_command_text(command.to_syntax()), [command])
        for value in (None, '', ' \t\n', [], float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SetVariableCommand('x', 'literal', value)

    def test_parser_rejects_invalid_names_methods_and_parameters(self):
        for text in [
            'SetVariable(name="2x", method="literal", value="a")',
            'SetVariable(name="é", method="literal", value="a")',
            'SetVariable(name="x.y", method="literal", value="a")',
            'SetVariable(name="x", method="unknown", value="a")',
            'SetVariable(name="x", method="literal", value=" ")',
            'SetVariable(name="x", method="lookup", referencedForm="F", filter="TRUE")',
            'SetVariable(name="x", method="literal", value="a", filter="TRUE")',
            'SetVariable(name="x", method="lookup", value="a", referencedForm="F", filter="TRUE", returnFormula="X")',
        ]:
            with self.subTest(text=text), self.assertRaises(CommandTextError):
                parse_command_text(text)

    def test_reassignment_iteration_and_re_evaluation(self):
        with Client(ui.page('/')), ui.column():
            instance = manager('''SetVariable(name="prefix", method="literal", value="Old")
SetVariable(name="prefix", method="literal", value="New")
ModifyField(fieldPattern="cost_.*", fieldType="quantity", label="${vars.prefix} ${field.index}")''')
            self.assertEqual([e.status for e in instance.executions], ['applied'] * 3)
            self.assertEqual(instance.target_schema()['elements'][1]['label'], 'New 1')
            self.assertEqual(instance.target_schema()['elements'][3]['label'], 'New 2')
            instance.delete_command(1)
            self.assertEqual(instance.target_schema()['elements'][1]['label'], 'Old 1')
            instance.delete_command(0)
            self.assertEqual(instance.executions[0].status, 'error')

    def test_single_pass_and_failed_reassignment(self):
        variables = {'vars.x': 'previous', 'vars.blank': ' '}
        command = SetVariableCommand('x', 'literal', '${vars.blank}')
        self.assertEqual(apply_command_locally(command, source(), variables=variables).status, 'error')
        self.assertEqual(variables['vars.x'], 'previous')
        command = SetVariableCommand('x', 'literal', '$${vars.unknown}')
        self.assertEqual(apply_command_locally(command, source(), variables=variables).status, 'applied')
        self.assertEqual(variables['vars.x'], '${vars.unknown}')

    def test_creator_switches_required_parameters_and_preserves_form_suggestions(self):
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((SET_VARIABLE,), on_add=added.append)
            creator.controls['name'].set_value('target')
            creator.controls['value'].set_value('example')
            creator._parameter_changed(None)
            creator._add_command()
            self.assertEqual(added[-1], SetVariableCommand('target', 'literal', 'example'))
            creator.controls['method'].set_value('data lookup')
            creator._parameter_changed(None)
            creator.set_suggestions('form_names', ['Database/Settings'])
            self.assertIn('Database/Settings', creator.controls['referenced_form'].options)
            creator.controls['referenced_form'].set_value('Database/Settings')
            creator.controls['filter'].set_value('ACTIVE')
            creator.controls['return_formula'].set_value('TARGET')
            creator._parameter_changed(None)
            creator._add_command()
            self.assertEqual(added[-1].method, 'data lookup')
            self.assertIsNone(added[-1].value)
            self.assertEqual(parse_command_text(added[-1].to_syntax()), [added[-1]])
            separator = next(editor for editor in creator.editors if editor.definition.key == 'separator')
            self.assertFalse(separator.container.visible)
            mode = creator.controls['mode']
            self.assertEqual(mode.value, 'first')
            self.assertIn('mode="first"', added[-1].to_syntax())
            mode.set_value('all')
            self.assertTrue(separator.container.visible)
            self.assertEqual(separator.control.value, '|')
            self.assertEqual(separator.control.props['maxlength'], 3)
            creator._add_command()
            self.assertIn('mode="all"', added[-1].to_syntax())
            self.assertIsNone(added[-1].separator)
            separator.control.set_value('')
            mode.set_value('unique')
            self.assertTrue(separator.container.visible)
            creator._add_command()
            self.assertIn('mode="unique"', added[-1].to_syntax())
            self.assertEqual(added[-1].separator, '')
            self.assertEqual(parse_command_text(added[-1].to_syntax()), [added[-1]])
            mode.set_value('first')
            self.assertFalse(separator.container.visible)


class LookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_matches_use_optional_separator_and_validate_every_value(self):
        prefix = ('SetVariable(name="x", method="data lookup", '
                  'referencedForm="Settings", filter="ACTIVE", returnFormula="TARGET"')
        rows = [{'value': value} for value in ['a', 0, 'a', False, '0', 'A']]
        for option, expected in [
            (', mode="All"', 'a|0|a|False|0|A'),
            (', mode="all", separator=" "', 'a 0 a False 0 A'),
            (', mode="all", separator=" / "', 'a / 0 / a / False / 0 / A'),
            (', mode="unique"', 'a|0|False|A'),
            (', mode="Unique", separator=""', 'a0FalseA'),
            (', multiple=true', 'a|0|a|False|0|A'),
        ]:
            with self.subTest(option=option):
                command, = parse_command_text(prefix + option + ')')
                self.assertEqual(parse_command_text(command.to_syntax()), [command])
                variables = {}
                result = await apply_command(command, source(), AsyncMock(), variables=variables,
                                             lookup_variable=AsyncMock(return_value=rows))
                self.assertEqual(result.status, 'applied')
                self.assertEqual(variables['vars.x'], expected)
        variables = {'vars.x': 'previous'}
        result = await apply_command(command, source(), AsyncMock(), variables=variables,
                                     lookup_variable=AsyncMock(return_value=[*rows, {'value': None}]))
        self.assertEqual(result.status, 'error')
        self.assertEqual(variables['vars.x'], 'previous')
        for syntax in (prefix + ', mode="all", separator="long")',
                       prefix + ', mode="unique", multiple=True)',
                       prefix + ', separator="")', prefix + ', mode="other")'):
            with self.subTest(syntax=syntax), self.assertRaises(ValueError):
                parse_command_text(syntax)

    async def test_value_validation_warning_and_atomic_reassignment(self):
        command = SetVariableCommand('x', 'lookup', referenced_form='Settings', filter='ACTIVE', return_formula='TARGET')
        self.assertEqual(command.mode, 'first')
        resolver = AsyncMock()
        for rows, status, expected in [
            ([{'value': 0}], 'applied', '0'),
            ([{'value': False}], 'applied', 'False'),
            ([{'value': ' first '}, {'value': 'second'}], 'warning', ' first '),
            ([], 'error', 'previous'),
            ([{'value': None}], 'error', 'previous'),
            ([{'value': ''}], 'error', 'previous'),
            ([{'value': ' \n'}], 'error', 'previous'),
            ([{'value': None}, {'value': 'valid'}], 'error', 'previous'),
        ]:
            with self.subTest(rows=rows):
                variables = {'vars.x': 'previous'}
                result = await apply_command(command, source(), resolver, variables=variables,
                                             lookup_variable=AsyncMock(return_value=rows))
                self.assertEqual(result.status, status)
                self.assertEqual(variables['vars.x'], expected)
                self.assertEqual(result.schema, source())
                if status != 'error':
                    self.assertIn(f'Variable vars.x set to "{expected}".', result.message)
                if status == 'warning':
                    self.assertIn('Warning: Lookup', result.message)
        resolver.assert_not_awaited()
        variables = {'vars.x': 'previous'}
        result = await apply_command(command, source(), resolver, variables=variables,
                                    lookup_variable=AsyncMock(side_effect=ValueError('API failure')))
        self.assertEqual(result.status, 'error')
        self.assertEqual(variables['vars.x'], 'previous')

    async def test_remote_form_and_formulas_are_sent_to_query_endpoint(self):
        service = AsyncMock()
        service.get_database_names.return_value = {'db': 'Other'}
        service.get_form_names.return_value = {'f': 'Settings'}
        service.query_rows.return_value = [{'value': 42}]
        command = SetVariableCommand('x', 'lookup', referenced_form='Other/Settings',
                                     filter='YEAR == 2027', return_formula='TARGET * 2')
        self.assertEqual(await query_variable(service, command, source()), [{'value': 42}])
        service.query_rows.assert_awaited_once_with({
            'rowSources': [{'rootFormId': 'f'}],
            'columns': [{'id': 'value', 'formula': 'TARGET * 2'}],
            'filter': 'YEAR == 2027',
        })
        service.get_form_tree.assert_not_awaited()

    async def test_connected_chain_warning_continues_error_stops(self):
        for rows, expected in [([{'value': 'new'}, {'value': 'other'}], ['warning', 'applied']),
                               ([], ['error', 'not_executed'])]:
            with self.subTest(rows=rows), Client(ui.page('/')), ui.column():
                lookup = AsyncMock(return_value=rows)
                instance = manager('''SetVariable(name="x", method="lookup", referencedForm="Settings", filter="TRUE", returnFormula="TARGET")
ModifyField(fieldCode="anchor", fieldType="quantity", label="${vars.x}")''',
                    resolve_schema=AsyncMock(side_effect=lambda data: ExecutionResult(data)), lookup_variable=lookup)
                await instance._evaluation_task
                self.assertEqual([e.status for e in instance.executions], expected)
                if rows:
                    self.assertEqual(instance.target_schema()['elements'][0]['label'], 'new')
                    self.assertIn('Variable vars.x set to "new".', instance.executions[0].message)

    async def test_lookup_value_can_select_fields_in_connected_evaluation(self):
        for selector, value in [('fieldCode', 'cost_2024'), ('fieldPattern', 'cost_.*')]:
            with self.subTest(selector=selector), Client(ui.page('/')), ui.column():
                instance = manager('''SetVariable(name="target", method="lookup", referencedForm="Settings", filter="TRUE", returnFormula="TARGET")
''' + f'DeleteField({selector}="${{vars.target}}")',
                    resolve_schema=AsyncMock(side_effect=lambda data: ExecutionResult(data)),
                    lookup_variable=AsyncMock(return_value=[{'value': value}]))
                await instance._evaluation_task
                self.assertEqual([e.status for e in instance.executions], ['applied', 'applied'])
                self.assertEqual(len(instance.target_schema()['elements']), 4 if selector == 'fieldCode' else 3)


if __name__ == '__main__':
    unittest.main()
