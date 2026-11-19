from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client
from activity_info_api_client.models.add_update_form_response import AddUpdateFormResponse

from activity_manager.api import ActivityInfoService, DEFAULT_BASE_URL
from activity_manager.command_creator import (
    CommandCreatorDefinition, CommandCreatorRenderer, ParameterDefinition as P,
)
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands import ADD_FIELD, AddFieldCommand
from test.execution_fixtures import execute_schema_fixture
from activity_manager.schema_identity import prepare_schema_for_put


def schema():
    return {
        'id': 'form1', 'label': 'Survey', 'schemaVersion': 1, 'databaseId': 'db1',
        'elements': [{'id': 'existingid', 'code': 'old', 'label': 'Old',
                      'type': 'FREE_TEXT', 'required': False, 'key': False}],
    }


class AddFieldTests(unittest.TestCase):
    def test_text_roundtrip_validation_and_code_first_chain(self):
        command = AddFieldCommand('name', 'Name "é"\nFull name')
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        self.assertEqual(
            parse_command_text('aDdFiElD(LABEL="Name", fieldCODE="name")'),
            [AddFieldCommand('name', 'Name')],
        )
        for arguments in (
            'fieldCode="bad code", label="Name"', 'fieldCode="name", label=" "',
            'fieldCode="name"', 'fieldCode="name", label=False',
            'fieldCode="name", label="Name", id="c123"',
            'fieldCode="name", FIELDCODE="other", label="Name"',
        ):
            with self.subTest(arguments=arguments), self.assertRaises(CommandTextError) as error:
                parse_command_text(f'AddField({arguments})')
            self.assertEqual(error.exception.line, 1)
        source = schema()
        before = deepcopy(source)
        with patch('activity_manager.schema_identity.secrets.token_hex') as generate:
            result = command.execute(source).schema
            self.assertEqual(command.execute(source).schema, result)
            execution = command.execute(result)
            self.assertRegex(execution.message, 'already exists')
            self.assertTrue(execution.diagnostics)
            with Client(ui.page('/')), ui.column():
                commands = CommandListManager(on_execute=lambda: None)
            commands.set_schema(source)
            for step in parse_command_text(
                command.to_syntax() + '\nMoveField(fieldCode="name", position="Beginning")'
            ):
                commands.add_command(step)
            self.assertEqual(commands.target_schema()['elements'][0]['code'], 'name')
            self.assertFalse(commands.execute_button.props.get('disable', False))
            generate.assert_not_called()
        self.assertEqual(result['elements'][-1]['id'], 'pending-field:name')
        self.assertEqual(result['elements'][-1]['type'], 'FREE_TEXT')
        self.assertEqual(source, before)

    def test_creator_parameter_order_defaults_activation_and_validation(self):
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((ADD_FIELD,), on_add=added.append)
        self.assertEqual(list(creator.controls), [
            'position', 'before_after_field', 'field_type', 'field_variant', 'field_code', 'label', 'description', 'key', 'unique', 'required',
            'relevance_condition', 'required_condition', 'validation_condition', 'validation_message',
            'hide_from_entry', 'hide_in_table', 'reviewer_only', 'read_only', 'translation',
            'translation_field', 'translation_language', 'indentation_level', 'prefix_formula',
            'input_mask', 'barcode', 'calculation_formula', 'metadata', 'units', 'upload_type',
            'attachment_cardinality',
            'allowed_file_types', 'geo_accuracy', 'option_list', 'referenced_form',
            'reverse_reference_field', 'lookup_fields', 'default_value', 'default_formula',
        ])
        self.assertEqual(creator.controls['field_type'].value, 'FREE_TEXT')
        for name in ('field_type', 'field_variant', 'field_code', 'label', 'required'):
            activation = creator.activations[name]
            self.assertTrue(activation.value)
            self.assertTrue(activation.props['disable'])
            self.assertFalse(activation.visible)
        creator.controls['field_code'].set_value('bad code')
        creator.controls['label'].set_value('Name')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        creator.controls['field_code'].set_value('name')
        creator._parameter_changed(None)
        creator._add_command()
        self.assertEqual(added, [AddFieldCommand('name', 'Name', required=False, field_variant='text', position='End')])
        for name in ('key', 'unique'):
            self.assertTrue(creator.activations[name].visible)
            self.assertFalse(creator.activations[name].value)
            creator.activations[name].set_value(True)
        creator.controls['field_type'].set_value('narrative')
        creator.controls['required'].set_value(True)
        creator._parameter_changed(None)
        creator._add_command()
        self.assertEqual(added[-1], AddFieldCommand(
            'name', 'Name', field_type='NARRATIVE', required=True, position='End',
        ))
        creator.controls['field_type'].set_value('unknown')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])

    def test_type_and_boolean_text_roundtrip_and_schema_values(self):
        text = ('addfield(REQUIRED=True, UNIQUE=False, KEY=True, LABEL="Name", '
                'fieldcode="name", FIELDTYPE="free_text")')
        command = parse_command_text(text)[0]
        self.assertEqual(command.to_syntax(),
                         'AddField(fieldType="FREE_TEXT", fieldCode="name", label="Name", '
                         'key=True, unique=False, required=True)')
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        field = command.execute(schema()).schema['elements'][-1]
        self.assertEqual((field['type'], field['key'], field['unique'], field['required']),
                         ('FREE_TEXT', True, False, True))
        narrative = AddFieldCommand('notes', 'Notes', field_type='NARRATIVE')
        self.assertEqual(narrative.execute(schema()).schema['elements'][-1]['type'], 'NARRATIVE')
        for argument in ('key="True"', 'unique=0', 'required=None', 'required=bool(1)',
                         'key=true', 'key=True, KEY=False', 'fieldType="unknown"'):
            with self.subTest(argument=argument), self.assertRaises(CommandTextError) as error:
                parse_command_text(f'AddField(fieldCode="name", label="Name", {argument})')
            self.assertEqual(error.exception.line, 1)
        for arguments in ({'field_type': 'unknown'}, {'required': 1}, {'key': 'false'}):
            with self.subTest(arguments=arguments):
                execution = AddFieldCommand('name', 'Name', **arguments).execute(schema())
                self.assertTrue(execution.has_errors, execution.message)

    def test_materialization_preserves_existing_ids_and_preview_and_is_idempotent(self):
        preview = AddFieldCommand('name', 'Name').execute(schema()).schema
        preview = AddFieldCommand('surname', 'Surname').execute(preview).schema
        with patch('activity_manager.schema_identity.secrets.token_hex',
                   side_effect=['a' * 24, 'a' * 24, 'b' * 24]) as generate:
            target = prepare_schema_for_put(preview)
            self.assertEqual(generate.call_count, 3)
            self.assertEqual(prepare_schema_for_put(target), target)
            self.assertEqual(generate.call_count, 3)
        self.assertEqual(target['elements'][0]['id'], 'existingid')
        self.assertEqual(target['elements'][1]['id'], 'c' + 'a' * 24)
        self.assertEqual(target['elements'][2]['id'], 'c' + 'b' * 24)
        self.assertEqual(preview['elements'][1]['id'], 'pending-field:name')


class GenericCreatorTests(unittest.TestCase):
    def make_creator(self, parameters):
        with Client(ui.page('/')), ui.column():
            return CommandCreatorRenderer((CommandCreatorDefinition(
                'example', 'Example', parameters, dict,
            ),))

    def test_activation_distinguishes_omission_blank_unset_and_false(self):
        creator = self.make_creator((
            P('label', 'Label', 'text', multiline=True),
            P('description', 'Description', 'text', blank_action='unset'),
            P('required', 'Required', 'boolean'),
        ))
        self.assertEqual(creator.values(), {})
        for activation in creator.activations.values():
            self.assertTrue(activation.visible)
            self.assertFalse(activation.props.get('label'))
            self.assertTrue(activation.props['aria-label'].startswith('Include '))
        for activation in creator.activations.values():
            activation.set_value(True)
        creator._parameter_changed(None)
        self.assertEqual(creator.values(), {'label': '', 'description': None, 'required': False})
        creator.controls['required'].set_value(True)
        creator.activations['label'].set_value(False)
        creator._parameter_changed(None)
        self.assertEqual(creator.values(), {'description': None, 'required': True})
        creator.set_enabled(False)
        self.assertTrue(creator.controls['required'].props['disable'])
        self.assertTrue(creator.activations['required'].props['disable'])
        creator.set_enabled(True)
        self.assertTrue(creator.controls['label'].props['disable'])
        self.assertFalse(creator.controls['required'].props['disable'])

    def test_typed_values_conditions_and_nested_output_paths(self):
        creator = self.make_creator((
            P('type', 'Type', 'single_select', required=True, options=('quantity', 'text')),
            P('units', 'Units', 'combobox', path='typeParameters.units',
              required_when=('type', ('quantity',)), visible_when=('type', ('quantity',))),
            P('digits', 'Digits', 'text', value_type='integer', initially_active=True),
            P('accuracy', 'Accuracy', 'text', value_type='number', initially_active=True),
        ))
        creator.controls['type'].set_value('quantity')
        creator.controls['digits'].set_value('0')
        creator.controls['accuracy'].set_value('2.5')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        self.assertFalse(creator.activations['units'].visible)
        creator.controls['units'].set_value('kg')
        creator._parameter_changed(None)
        self.assertEqual(creator.values(), {
            'type': 'quantity', 'typeParameters': {'units': 'kg'}, 'digits': 0, 'accuracy': 2.5,
        })
        creator.controls['digits'].set_value('2.3')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        creator.controls['digits'].set_value('2')
        creator.controls['accuracy'].set_value('nan')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        creator.controls['accuracy'].set_value('1')
        creator.controls['type'].set_value('text')
        creator._parameter_changed(None)
        self.assertNotIn('typeParameters', creator.values())
        self.assertTrue(creator.activations['units'].visible)

    def test_repeat_rows_nested_validation_reordering_and_activation_survive_rebuild(self):
        creator = self.make_creator((P('settings', 'Settings', 'group', children=(
            P('lookups', 'Lookups', 'repeat', children=(
                P('label', 'Label', 'text', required=True),
                P('formula', 'Formula', 'combobox', required=True, option_source='field_codes'),
                P('extra', 'Extra', 'text'),
            )),
        )),))
        group = creator.editors[0]
        lookups = group.children[0]
        group.activation.set_value(True)
        lookups.activation.set_value(True)
        lookups._change_rows(None, 0)
        self.assertTrue(creator.add_button.props['disable'])
        for child, value in zip(lookups.rows[0], ['Name', 'name', 'kept draft']):
            child.control.set_value(value)
        lookups._change_rows(None, 0)
        for child, value in zip(lookups.rows[1], ['Code', 'code']):
            child.control.set_value(value)
        creator._parameter_changed(None)
        self.assertFalse(creator.add_button.props['disable'])
        lookups._change_rows(1, -1)
        self.assertEqual(creator.values(), {'settings': {'lookups': [
            {'label': 'Code', 'formula': 'code'}, {'label': 'Name', 'formula': 'name'},
        ]}})
        self.assertEqual(lookups.rows[1][2].control.value, 'kept draft')
        creator.set_schema({'elements': [{'code': 'suggested'}]})
        self.assertEqual(lookups.rows[0][1].control.options, ['suggested'])
        self.assertEqual(lookups.rows[0][1].control.value, 'code')
        creator.set_enabled(False)
        self.assertTrue(lookups.rows[0][1].control.dropdown_button.props['disable'])
        self.assertTrue(all(button.props['disable'] for button in lookups.buttons))
        creator.set_enabled(True)
        lookups._change_rows(0, 0)
        lookups._change_rows(0, 0)
        self.assertEqual(creator.values(), {'settings': {'lookups': []}})
        group.activation.set_value(False)
        creator._parameter_changed(None)
        self.assertEqual(creator.values(), {})

    def test_hidden_dependencies_hide_their_dependents_in_the_same_event(self):
        creator = self.make_creator((
            P('type', 'Type', 'single_select', required=True, options=('a', 'b')),
            P('mode', 'Mode', 'text', required=True, visible_when=('type', ('a',))),
            P('detail', 'Detail', 'text', required=True, visible_when=('mode', ('custom',))),
        ))
        creator.controls['type'].set_value('a')
        creator._parameter_changed(None)
        creator.controls['mode'].set_value('custom')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        creator.controls['type'].set_value('b')
        creator._parameter_changed(None)
        self.assertFalse(creator.parameter_containers['detail'].visible)
        self.assertEqual(creator.values(), {'type': 'b'})
        self.assertFalse(creator.add_button.props['disable'])


class DeferredWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_and_verification_share_one_materialized_target(self):
        source = schema()
        preview = AddFieldCommand('name', 'Name').execute(source).schema
        reads = 0
        written = None

        async def read(form_id):
            nonlocal reads
            reads += 1
            return source if written is None else written

        async def write(form_id, target):
            nonlocal written
            written = deepcopy(target)
            self.assertRegex(written['elements'][-1]['id'], r'^c[a-f0-9]{24}$')

        service = SimpleNamespace(get_form_schema=AsyncMock(side_effect=read),
                                  put_form_schema=AsyncMock(side_effect=write))
        choose = AsyncMock()
        with patch('activity_manager.schema_identity.secrets.token_hex', return_value='a' * 24) as generate:
            outcome = await execute_schema_fixture(service, 'form1', source, preview, choose)
        self.assertEqual(outcome.status, 'success')
        generate.assert_called_once()
        choose.assert_not_awaited()
        self.assertEqual(preview['elements'][-1]['id'], 'pending-field:name')

    async def test_stale_materializes_before_preflight_and_eliminated_fields_allocate_nothing(self):
        source = schema()
        preview = AddFieldCommand('name', 'Name').execute(source).schema
        changed = deepcopy(source)
        changed['label'] = 'Changed elsewhere'
        service = SimpleNamespace(get_form_schema=AsyncMock(return_value=changed),
                                  put_form_schema=AsyncMock())
        with patch('activity_manager.schema_identity.secrets.token_hex', return_value='a' * 24) as generate:
            stale = await execute_schema_fixture(service, 'form1', source, preview, AsyncMock())
            self.assertEqual(stale.status, 'preflight_failed')
            service.get_form_schema.return_value = source
            target = parse_command_text('DeleteField(fieldCode="name")')[0].execute(preview).schema
            noop = await execute_schema_fixture(service, 'form1', source, target, AsyncMock())
            self.assertEqual(noop.status, 'success')
            generate.assert_called_once()
        service.put_form_schema.assert_not_awaited()

    async def test_api_materializes_direct_put_calls_without_mutating_input(self):
        preview = AddFieldCommand('name', 'Name').execute(schema()).schema
        service = ActivityInfoService(DEFAULT_BASE_URL, 'test-token')
        try:
            response = AddUpdateFormResponse(database=object(), forms=[])
            with patch.object(service, '_request', AsyncMock(return_value=response)) as request:
                await service.put_form_schema('form1', preview)
            payload = request.await_args.kwargs['body'].to_dict()
            self.assertRegex(payload['elements'][-1]['id'], r'^c[a-f0-9]{24}$')
            self.assertEqual(preview['elements'][-1]['id'], 'pending-field:name')
        finally:
            await service.aclose()
