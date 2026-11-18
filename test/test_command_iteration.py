from copy import deepcopy
import asyncio
import unittest
from unittest.mock import AsyncMock

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_execution import Diagnostic, ExecutionResult, apply_command_locally
from activity_manager.command_iteration import IterativeCommand
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.command_templates import substitute_text
from activity_manager.commands import COMMAND_CREATORS, MODIFY_FIELD, ModifyFieldCommand


def source():
    return {'id': 'form', 'elements': [
        {'id': 'x', 'code': 'anchor', 'label': 'Anchor', 'type': 'quantity'},
        {'id': 'a', 'code': 'cost_2024', 'label': 'Cost 2024', 'type': 'quantity'},
        {'id': 'y', 'code': 'other', 'label': 'Other', 'type': 'quantity'},
        {'id': 'b', 'code': 'cost_2025', 'label': 'Cost 2025', 'type': 'quantity'},
        {'id': 'z', 'code': 'last', 'label': 'Last', 'type': 'quantity'},
    ]}


def manager(text, *, schema=None, **kwargs):
    instance = CommandListManager(on_execute=AsyncMock(), **kwargs)
    instance.set_schema(source() if schema is None else schema)
    for command in parse_command_text(text):
        instance.add_command(command)
    return instance


class TemplateTests(unittest.TestCase):
    def test_old_namespaces_are_rejected_without_aliases(self):
        for reference in ('match.code', 'match.year', 'match.1', 'iteration.index'):
            for variables in (None, {reference: 'old value'}):
                with self.subTest(reference=reference, variables=variables), self.assertRaises(ValueError):
                    substitute_text('${' + reference + '}', variables)
            with self.subTest(reference=reference), self.assertRaises(CommandTextError):
                parse_command_text('ModifyField(fieldCode="anchor", fieldType="quantity", label="${' + reference + '}")')

    def test_future_namespaces_validate_but_have_no_execution_context(self):
        for namespace in ('database',):
            for member in ('code', 'index', 'region', '1'):
                text = '${' + namespace + '.' + member + '}'
                with self.subTest(text=text):
                    self.assertEqual(substitute_text(text), text)
                    # Reserved namespaces cannot be supplied accidentally by a caller.
                    with self.assertRaisesRegex(ValueError, 'iteration context is not available'):
                        substitute_text(text, {namespace + '.' + member: 'unexpected'})
                    command = parse_command_text('ModifyField(fieldCode="anchor", fieldType="quantity", label="' + text + '")')[0]
                    result = apply_command_locally(command, source())
                    self.assertEqual(result.status, 'error')
                    self.assertIn('iteration context is not available', result.message)
                    self.assertEqual(result.schema, source())

    def test_namespace_member_rules(self):
        for text in ('${unknown.code}', '${field.0}', '${form.-1}', '${database.name.extra}',
                     '${vars.1}', '${field.}', '${.index}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                substitute_text(text)

    def test_single_pass_and_escape(self):
        self.assertEqual(substitute_text('[$${field.x}] ${field.x} $$ $ {x}',
                                        {'field.x': '${vars.other}'}),
                         '[${field.x}] ${vars.other} $$ $ {x}')
        self.assertEqual(substitute_text('x ${field.x} $${vars.y}'), 'x ${field.x} $${vars.y}')

    def test_unknown_optional_and_malformed(self):
        for text, values in [('${vars.no}', {}), ('${field.x}', {'field.x': None}),
                             ('${field.x', {}), ('${field.x + 1}', {}), ('${}', {})]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                substitute_text(text, values)

    def test_field_index_and_explicit_variable_lookup(self):
        self.assertEqual(substitute_text('${vars.prefix}_${field.index}',
                                        {'vars.prefix': 'budget', 'field.index': 2}), 'budget_2')

    def test_single_command_escapes_and_missing_iteration_context(self):
        result = apply_command_locally(ModifyFieldCommand('anchor', 'quantity', changes={
            'label': '$${field.year}'}), source())
        self.assertEqual(result.schema['elements'][0]['label'], '${field.year}')
        result = apply_command_locally(ModifyFieldCommand('anchor', 'quantity', changes={
            'label': '${field.year}'}), source())
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.schema, source())


class ParsingTests(unittest.TestCase):
    def test_built_in_capture_names_are_reserved(self):
        for name in ('code', 'index'):
            with self.subTest(name=name), self.assertRaisesRegex(CommandTextError, 'reserved'):
                parse_command_text(f'DeleteField(fieldPattern="(?P<{name}>.*)")')
            with self.subTest(name=name), Client(ui.page('/')), ui.column():
                instance = manager(f'''SetVariable(name="pattern", method="literal", value="(?P<{name}>.*)")
DeleteField(fieldPattern="${{vars.pattern}}")''')
                self.assertEqual(instance.executions[-1].status, 'error')
                self.assertIn('reserved', instance.executions[-1].message)

    def test_roundtrip_preserves_pattern_functions_and_placeholders(self):
        text = r'''ModifyField(fieldPattern=r"cost_(?P<year>\d{4})", fieldType="quantity",
            newCode="budget_${field.year}", label=Append("End", " ", "${field.1}"))'''
        command = parse_command_text(text)[0]
        self.assertIsInstance(command, IterativeCommand)
        self.assertEqual(parse_command_text(command.to_syntax())[0], command)

    def test_invalid_authored_commands_are_source_located(self):
        for text in ['DeleteField(fieldCode="a", fieldPattern=".*")',
                     'DeleteField(fieldPattern="a", fieldPattern="b")',
                     'DeleteField(fieldPattern="[")', 'DeleteField(fieldPattern="")',
                     'DeleteField(fieldPattern=1)', 'DeleteField(fieldPattern="(?P<code>.*)")',
                     'AddField(fieldPattern=".*", label="X")',
                     'ModifyField(fieldCode="a", fieldType="quantity", label="${broken}")']:
            with self.subTest(text=text), self.assertRaises(CommandTextError) as error:
                parse_command_text(text)
            self.assertEqual(error.exception.line, 1)

    def test_authored_selectors_preserve_templates_until_execution(self):
        command = parse_command_text('DeleteField(fieldPattern=r"cost_\\d{4}$")')[0]
        self.assertEqual(len(command.targets(source())), 2)
        literal = parse_command_text('DeleteField(fieldCode="${vars.literal}")')[0]
        self.assertEqual(literal.field_code, '${vars.literal}')

    def test_selectors_reject_iteration_context(self):
        for selector in ('fieldCode', 'fieldPattern'):
            for reference in ('field.code', 'field.year', 'field.1', 'field.index', 'database.index'):
                text = f'DeleteField({selector}="${{{reference}}}")'
                with self.subTest(text=text), self.assertRaises(CommandTextError) as error:
                    parse_command_text(text)
                self.assertIn('references are allowed', str(error.exception))


class IterationTests(unittest.TestCase):
    def test_move_order_at_all_destinations(self):
        for position, relative, expected in [
            ('Beginning', '', ['cost_2024', 'cost_2025', 'anchor', 'other', 'last']),
            ('End', '', ['anchor', 'other', 'last', 'cost_2024', 'cost_2025']),
            ('Before field', ', beforeAfterField="other"', ['anchor', 'cost_2024', 'cost_2025', 'other', 'last']),
            ('After field', ', beforeAfterField="anchor"', ['anchor', 'cost_2024', 'cost_2025', 'other', 'last']),
        ]:
            with self.subTest(position=position), Client(ui.page('/')), ui.column():
                instance = manager(f'MoveField(fieldPattern="cost_.*", position="{position}"{relative})')
                result = instance.executions[0]
                self.assertEqual([field['code'] for field in result.schema['elements']], expected)
                self.assertEqual(len(result.iterations), 2)
                self.assertEqual(result.iterations[1].concrete_command.position, 'After field')
                self.assertEqual(result.iterations[1].concrete_command.before_after_field, 'cost_2024')

    def test_rename_capture_function_and_subsequent_command(self):
        with Client(ui.page('/')), ui.column():
            instance = manager(r'''ModifyField(fieldPattern=r"cost_(?P<year>\d{4})", fieldType="quantity",
                newCode="budget_${field.year}", label=Append("End", " ", "${field.index}"))
DeleteField(fieldCode="budget_2024")''')
            parent = instance.executions[0]
            self.assertEqual([child.field_code for child in parent.iterations], ['cost_2024', 'cost_2025'])
            self.assertEqual(parent.iterations[0].schema['elements'][1]['code'], 'budget_2024')
            self.assertEqual(parent.iterations[0].schema['elements'][3]['code'], 'cost_2025')
            self.assertEqual(parent.schema['elements'][3]['label'], 'Cost 2025 2')
            self.assertEqual(parent.schema['elements'][1]['id'], 'a')
            self.assertNotIn('budget_2024', [field['code'] for field in instance.target_schema()['elements']])

    def test_delete_snapshot_and_fullmatch(self):
        with Client(ui.page('/')), ui.column():
            instance = manager('DeleteField(fieldPattern="cost_.*")')
            self.assertEqual([field['code'] for field in instance.target_schema()['elements']], ['anchor', 'other', 'last'])
            instance = manager('DeleteField(fieldPattern="cost")')
            self.assertEqual(instance.executions[0].status, 'warning')
            self.assertEqual(instance.executions[0].iterations, ())
            self.assertEqual(instance.target_schema(), source())

    def test_iteration_error_stops_children_and_later_commands(self):
        with Client(ui.page('/')), ui.column():
            instance = manager('''ModifyField(fieldPattern="cost_.*", fieldType="quantity", newCode="same")
DeleteField(fieldCode="last")''')
            parent = instance.executions[0]
            self.assertEqual(parent.status, 'error')
            self.assertEqual([child.status for child in parent.iterations], ['applied', 'error'])
            self.assertIn('Stopped at iteration 2', parent.message)
            self.assertEqual(parent.schema['elements'][1]['code'], 'same')
            self.assertEqual(parent.schema['elements'][3]['code'], 'cost_2025')
            self.assertEqual(instance.executions[1].status, 'not_executed')
            self.assertIsNone(instance.target_schema())

    def test_type_mismatch_skips_without_label_fallback(self):
        data = source()
        data['elements'][1]['type'] = 'date'
        with Client(ui.page('/')), ui.column():
            instance = manager('ModifyField(fieldPattern="cost_.*", fieldType="quantity", label="Changed")', schema=data)
            self.assertEqual([child.status for child in instance.executions[0].iterations], ['warning', 'applied'])
            self.assertEqual(instance.executions[0].iterations[0].schema, data)
            self.assertEqual(instance.executions[0].schema['elements'][1], data['elements'][1])
            self.assertEqual(instance.target_schema()['elements'][3]['label'], 'Changed')

    def test_optional_group_error_is_atomic(self):
        with Client(ui.page('/')), ui.column():
            instance = manager('''ModifyField(fieldPattern=r"cost_(?P<year>2024)?(?:2025)?", fieldType="quantity",
                label="${field.year}")''')
            self.assertEqual([child.status for child in instance.executions[0].iterations], ['applied', 'error'])
            self.assertIn('did not match', instance.executions[0].message)

    def test_stable_identity_uses_current_code(self):
        command = parse_command_text('DeleteField(fieldPattern="cost_.*")')[0]
        target = command.targets(source())[0]
        data = source()
        data['elements'][1]['code'] = 'renamed'
        prepared = target.prepare(command, data)
        self.assertEqual(prepared.command.field_code, 'renamed')
        self.assertEqual(target.variables['field.code'], 'cost_2024')

    def test_nested_values_substitute_once(self):
        data = source()
        data['elements'][1].update(type='enumerated', typeParameters={
            'cardinality': 'single', 'values': [{'id': 'yes', 'label': 'Yes'}]})
        with Client(ui.page('/')), ui.column():
            instance = manager('''ModifyField(fieldPattern="cost_2024", fieldType="enumerated", fieldVariant="single",
                optionList={"values": [{"label": "Option ${field.code}"}]})''', schema=data)
            self.assertEqual(instance.executions[0].status, 'applied')
            self.assertEqual(instance.target_schema()['elements'][1]['typeParameters']['values'][0]['label'],
                             'Option cost_2024')

    def test_empty_capture_uses_normal_removal_semantics(self):
        data = source()
        data['elements'][1]['description'] = 'Remove this'
        with Client(ui.page('/')), ui.column():
            instance = manager('''ModifyField(fieldPattern="cost_2024(?P<empty>)", fieldType="quantity",
                description="${field.empty}")''', schema=data)
            self.assertEqual(instance.executions[0].status, 'applied')
            self.assertNotIn('description', instance.target_schema()['elements'][1])

    def test_inserted_capture_and_escaped_text_are_not_expanded_twice(self):
        data = source()
        data['elements'][1]['code'] = '${vars.literal}'
        with Client(ui.page('/')), ui.column():
            instance = manager('''ModifyField(fieldPattern=r"\\$\\{vars.literal\\}", fieldType="quantity",
                label="${field.code}", description="$${field.code}")''', schema=data)
            self.assertEqual(instance.executions[0].status, 'applied')
            field = instance.target_schema()['elements'][1]
            self.assertEqual(field['label'], '${vars.literal}')
            self.assertEqual(field['description'], '${field.code}')

    def test_changed_source_reselects_parent_when_iteration_disappears(self):
        with Client(ui.page('/')), ui.column():
            instance = manager('DeleteField(fieldPattern="cost_.*")')
            instance.select_iteration(0, 1)
            data = source()
            data['elements'] = data['elements'][:1]
            instance.set_schema(data)
            self.assertEqual(instance.selected_index, 0)
            self.assertIsNone(instance.selected_iteration)
            self.assertEqual(instance.executions[0].schema, data)

    def test_selection_collapse_and_parent_operations(self):
        selected = []
        with Client(ui.page('/')), ui.column():
            instance = manager('DeleteField(fieldPattern="cost_.*")\nDeleteField(fieldCode="last")',
                               on_selection_change=lambda index, execution: selected.append((index, execution)))
            instance.toggle_iterations(0)
            instance.select_iteration(0, 0)
            self.assertEqual(len(selected[-1][1].schema['elements']), 4)
            instance.toggle_iterations(0)
            self.assertIsNone(instance.selected_iteration)
            self.assertEqual(len(selected[-1][1].schema['elements']), 3)
            instance.toggle_iterations(0)
            instance.select_iteration(0, 1)
            instance.move_command(0, 1)
            self.assertEqual(instance.selected_index, 1)
            self.assertEqual(len(instance.commands), 2)
            instance.delete_command(1)
            self.assertEqual(len(instance.commands), 1)


class AsyncIterationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolution_per_step_warnings_and_snapshot_annotations(self):
        async def resolve(data):
            warning = Diagnostic('warning', 'Check field', ('elements', 0, 'code'))
            return ExecutionResult(data, (warning,) if len(data['elements']) == 4 else (), deepcopy(data))
        resolver = AsyncMock(side_effect=resolve)
        with Client(ui.page('/')), ui.column():
            instance = manager('DeleteField(fieldPattern="cost_.*")', resolve_schema=resolver)
            await instance._evaluation_task
            parent = instance.executions[0]
            self.assertEqual(resolver.await_count, 2)
            self.assertEqual(parent.status, 'warning')
            self.assertIn('Iteration 1 (cost_2024): Warning: Check field', parent.message)
            self.assertEqual(len(parent.iterations[0].issues), 1)
            self.assertEqual(parent.issues, ())
            self.assertTrue(instance.execute_button.enabled)

    async def test_resolver_error_stops_chain(self):
        resolver = AsyncMock(side_effect=lambda data: ExecutionResult.error(data, 'Resolution failed'))
        with Client(ui.page('/')), ui.column():
            instance = manager('DeleteField(fieldPattern="cost_.*")\nDeleteField(fieldCode="last")', resolve_schema=resolver)
            await instance._evaluation_task
            self.assertEqual(resolver.await_count, 1)
            self.assertEqual([child.status for child in instance.executions[0].iterations], ['error', 'not_executed'])
            self.assertEqual(instance.executions[1].status, 'not_executed')

    async def test_source_change_cancels_old_iteration_results(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def resolve(data):
            entered.set()
            await release.wait()
            return ExecutionResult(data)
        with Client(ui.page('/')), ui.column():
            instance = manager('DeleteField(fieldPattern="cost_.*")', resolve_schema=resolve)
            old_task = instance._evaluation_task
            await entered.wait()
            data = source()
            data['elements'] = data['elements'][:1]
            instance.set_schema(data)
            release.set()
            await instance._evaluation_task
            self.assertTrue(old_task.cancelled())
            self.assertEqual(instance.target_schema(), data)
            self.assertEqual(instance.executions[0].iterations, ())


class CreatorIterationTests(unittest.TestCase):
    def test_mode_creation_and_single_target_autofill_preserved(self):
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((MODIFY_FIELD,), on_add=added.append)
            creator.set_schema(source())
            creator.set_suggestions('field_codes', ['anchor', 'cost_2024'])
            creator.controls['field_code'].set_value('anchor')
            creator._parameter_changed(None)
            self.assertEqual(creator.controls['field_type'].value, 'quantity')
            creator.controls['field_type'].set_value('date')
            creator._parameter_changed(None)
            self.assertEqual(creator.controls['field_type'].value, 'date')
            creator.target_mode.set_value('Multiple')
            creator._target_mode_changed(None)
            creator.controls['field_code'].set_value('cost_.*')
            creator._parameter_changed(None)
            self.assertEqual(creator.controls['field_type'].value, 'date')
            self.assertEqual(creator.controls['field_code'].options, [])
            creator._add_command()
            self.assertIsInstance(added[0], IterativeCommand)
            self.assertEqual(added[0].field_pattern, 'cost_.*')
            creator.controls['field_code'].set_value('[')
            creator._parameter_changed(None)
            self.assertFalse(creator.add_button.enabled)
            creator.controls['field_code'].set_value('anchor')
            creator.target_mode.set_value('Single')
            creator._target_mode_changed(None)
            self.assertEqual(creator.controls['field_type'].value, 'quantity')
            self.assertIn('anchor', creator.controls['field_code'].options)

    def test_mode_not_offered_for_add_field(self):
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer(COMMAND_CREATORS)
            creator.command_select.set_value('add_field')
            creator._command_changed(None)
            self.assertFalse(creator.target_mode.visible)
