import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nicegui import ui
from nicegui.client import Client
from activity_info_api_client.models.form_schema import FormSchema

from activity_manager.command_creator import CommandCreatorDefinition, CommandCreatorRenderer
from activity_manager.field_parameters import FIELD_PARAMETERS, configure_field_parameters
from activity_manager.commands.add_field_parameters import ADD_FIELD_PARAMETERS
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands import ADD_FIELD, AddFieldCommand
from activity_manager.page import connection_page
from activity_manager.api import ActivityInfoService
from activity_manager.listbox import DatabaseListbox
from activity_manager.schema_change import apply_form_schema_change
from activity_manager.schema_identity import prepare_schema_for_put, prepare_schema_for_write
from test_add_field import schema


OPTIONS = {'cardinality': 'multiple', 'presentation': 'dropdown', 'values': [
    {'code': 'yes', 'label': 'Yes'}, {'code': 'no', 'label': 'No'},
]}


class StandardFieldTests(unittest.TestCase):
    def roundtrip(self, command):
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        return command.execute(schema())['elements'][-1]

    def test_common_properties_conditions_and_false_values_survive(self):
        command = AddFieldCommand(
            'new', 'New', description='', key=False, unique=False, required=True,
            hide_from_entry=True, hide_in_table=False, read_only=False,
            relevance_condition='old != ""', required_condition='old == "yes"',
            validation_condition='LEN(new) > 2', validation_message='Longer, please',
            default_value='', default_formula='old',
        )
        field = self.roundtrip(command)
        self.assertEqual(field['description'], '')
        self.assertFalse(field['dataEntryVisible'])
        self.assertTrue(field['tableVisible'])
        self.assertFalse(field['readOnly'])
        self.assertFalse(field['unique'])
        self.assertEqual(field['relevanceCondition'], 'old != ""')
        self.assertEqual(field['requiredCondition'], 'old == "yes"')
        self.assertEqual(field['validationCondition'], 'LEN(new) > 2')
        self.assertEqual(field['validationMessage'], 'Longer, please')
        self.assertEqual(field['defaultValue'], '')
        self.assertEqual(field['defaultValueFormula'], 'old')
        self.assertNotIn('typeParameters', field)

    def test_specialized_scalars_and_defaults(self):
        cases = [
            ('section', {'indentation_level': 2}, {'indentationLevel': 2}),
            ('serial', {'prefix_formula': '"ID-"'}, {'prefixFormula': '"ID-"'}),
            ('FREE_TEXT', {'input_mask': 'AAA-999'}, {'inputMask': 'AAA-999'}),
            ('calculated', {'calculation_formula': '1 + 2'}, {'formula': '1 + 2'}),
            ('quantity', {'units': 'kg', 'default_value': '-2.5'}, {'units': 'kg'}),
            ('geopoint', {'geo_accuracy': 2.5, 'default_value': '{"latitude": -2, "longitude": 3}'},
             {'requiredAccuracy': 2.5}),
            ('subform', {'referenced_form': 'child1'}, {'formId': 'child1'}),
            ('multiselectreference', {'referenced_form': 'other', 'default_value': '["record1"]'},
             {'range': [{'formId': 'other'}]}),
        ]
        for field_type, settings, expected in cases:
            with self.subTest(field_type=field_type):
                command = AddFieldCommand('new', 'New', field_type=field_type, **settings)
                field = self.roundtrip(command)
                self.assertEqual(field['typeParameters'], expected)
                target = prepare_schema_for_put(command.execute(schema()))
                self.assertEqual(FormSchema.from_dict(target).to_dict(), target)
        quantity = AddFieldCommand('new', 'New', field_type='quantity', default_value='0')
        self.assertEqual(quantity.default_value, 0.0)

    def test_attachment_multiselect_literals_and_empty_lists(self):
        command = AddFieldCommand('files', 'Files', field_type='attachment',
                                  upload_type=['FILE', 'AUDIO', 'FILE'], allowed_file_types=['PDF', 'AUDIO'])
        field = self.roundtrip(command)
        self.assertEqual(field['typeParameters'], {'captureMethods': ['FILE', 'AUDIO'], 'fileTypes': ['PDF', 'AUDIO']})
        empty = self.roundtrip(AddFieldCommand('files', 'Files', field_type='attachment',
                                               upload_type=[], allowed_file_types=[]))
        self.assertEqual(empty['typeParameters'], {'captureMethods': [], 'fileTypes': []})
        for cardinality in ('single', 'multiple'):
            with self.subTest(cardinality=cardinality):
                command = AddFieldCommand('files', 'Files', field_type='attachment',
                                          attachment_cardinality=cardinality)
                self.assertEqual(self.roundtrip(command)['typeParameters'], {'cardinality': cardinality})
                target = prepare_schema_for_put(command.execute(schema()))
                self.assertEqual(FormSchema.from_dict(target).to_dict(), target)

    def test_options_defaults_and_lookup_ids_are_deferred_and_consistent(self):
        selection = AddFieldCommand('choice', 'Choice', field_type='enumerated',
                                   option_list=OPTIONS, default_value=['yes'])
        reference = AddFieldCommand('partner', 'Partner', field_type='reference', referenced_form='partners',
                                   lookup_fields=[{'label': 'Name', 'formula': 'Name'},
                                                  {'label': 'Region', 'formula': 'Region.Name'}])
        with patch('activity_manager.schema_identity.secrets.token_hex') as generate:
            preview = reference.execute(selection.execute(schema()))
            self.assertEqual(parse_command_text(selection.to_syntax()), [selection])
            self.assertEqual(parse_command_text(reference.to_syntax()), [reference])
            generate.assert_not_called()
        before = deepcopy(preview)
        target = prepare_schema_for_put(preview)
        fields = {field['code']: field for field in target['elements']}
        choices = fields['choice']['typeParameters']['values']
        self.assertEqual(fields['choice']['defaultValue'], [choices[0]['id']])
        identities = [item['id'] for item in choices + fields['partner']['typeParameters']['lookupConfigs']]
        self.assertEqual(len(set(identities)), 4)
        for identity in identities:
            self.assertRegex(identity, r'^c[a-f0-9]{24}$')
        self.assertEqual(preview, before)
        self.assertEqual(prepare_schema_for_put(target), target)
        self.assertEqual(FormSchema.from_dict(target).to_dict(), target)

    def test_translation_resolves_later_field_codes_and_preserves_languages(self):
        original = AddFieldCommand('original', 'Original', translation=True,
                                  translation_field='translated', translation_language='fr, ar, fr')
        translated = AddFieldCommand('translated', 'Translation')
        self.assertEqual(parse_command_text(original.to_syntax()), [original])
        preview = translated.execute(original.execute(schema()))
        self.assertEqual(preview['elements'][1]['typeParameters']['translationConfig']['fieldId'],
                         'field-code:translated')
        target = prepare_schema_for_put(preview)
        self.assertEqual(target['elements'][1]['typeParameters']['translationConfig'],
                         {'fieldId': target['elements'][2]['id'], 'languages': ['fr', 'ar']})
        self.assertEqual(FormSchema.from_dict(target).to_dict(), target)
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            prepare_schema_for_put(original.execute(schema()))
        with self.assertRaisesRegex(ValueError, 'distinct'):
            prepare_schema_for_put(AddFieldCommand('old', 'New', translation=True,
                translation_field='old', translation_language='fr').execute({'id': 'form1', 'elements': []}))

    def test_metadata_uses_reserved_identity_and_user_reference(self):
        for metadata, reserved in [('ADDED_BY', '_addedBy'), ('LAST_EDITED_BY', '_lastEditedBy')]:
            with self.subTest(metadata=metadata):
                command = AddFieldCommand('author', 'Author', field_type='reference', metadata=metadata)
                field = self.roundtrip(command)
                self.assertTrue(field['readOnly'])
                self.assertEqual(field['typeParameters']['range'], [{'formId': 'db1@users'}])
                with patch('activity_manager.schema_identity.secrets.token_hex') as generate:
                    target = prepare_schema_for_put(command.execute(schema()))
                    generate.assert_not_called()
                self.assertEqual(target['elements'][-1]['id'], reserved)
                self.assertEqual(FormSchema.from_dict(target).to_dict(), target)
                with self.assertRaisesRegex(ValueError, 'already contains'):
                    AddFieldCommand('otherAuthor', 'Other', field_type='reference', metadata=metadata).execute(target)

    def test_validation_rejects_wrong_types_combinations_and_nested_shapes(self):
        cases = [
            {'reviewer_only': False}, {'units': 'kg'}, {'translation': True},
            {'translation_field': 'old'}, {'field_type': 'section', 'indentation_level': -1},
            {'field_type': 'section', 'indentation_level': True},
            {'field_type': 'geopoint', 'geo_accuracy': float('nan')},
            {'field_type': 'geopoint', 'geo_accuracy': 10 ** 500},
            {'field_type': 'calculated'}, {'field_type': 'reference'},
            {'field_type': 'reversereference', 'referenced_form': 'other'},
            {'field_type': 'reference', 'metadata': 'INVALID'},
            {'field_type': 'reference', 'metadata': 'ADDED_BY', 'read_only': False},
            {'field_type': 'reference', 'metadata': 'ADDED_BY', 'default_value': 'record'},
            {'field_type': 'attachment', 'upload_type': ['INVALID']},
            {'field_type': 'attachment', 'attachment_cardinality': 'invalid'},
            {'field_type': 'attachment', 'attachment_cardinality': True},
            {'attachment_cardinality': 'single'},
            {'field_type': 'attachment', 'upload_type': ['AUDIO'], 'allowed_file_types': ['PDF']},
            {'field_type': 'enumerated', 'option_list': {'cardinality': 'single', 'values': []}},
            {'field_type': 'enumerated', 'option_list': OPTIONS, 'default_value': ['unknown']},
            {'field_type': 'reference', 'referenced_form': 'other', 'lookup_fields': [{'formula': 'Name'}]},
            {'field_type': 'quantity', 'default_value': True},
            {'field_type': 'geopoint', 'default_value': {'latitude': 91, 'longitude': 0}},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                AddFieldCommand('new', 'New', **values)

    def test_nested_text_rejects_calls_expansion_duplicate_keys_and_invalid_literals(self):
        for extra in (
            'lookupFields=[dict(label="Name", formula="Name")]',
            'lookupFields=[{"label": "Name", "label": "Other", "formula": "Name"}]',
            'lookupFields=[{**other}]', 'lookupFields=[item for item in values]',
            'geoAccuracy=1e999', 'indentationLevel=True', 'reviewerOnly=True',
        ):
            with self.subTest(extra=extra), self.assertRaises(CommandTextError) as error:
                parse_command_text(f'AddField(fieldCode="new", label="New", {extra})')
            self.assertEqual(error.exception.line, 1)


class SharedFieldDefinitionTests(unittest.TestCase):
    def test_shared_fields_allow_partial_values_without_creation_requirements(self):
        def check_optional(parameters):
            for parameter in parameters:
                self.assertFalse(parameter.required)
                self.assertIsNone(parameter.required_when)
                self.assertIsNone(parameter.default)
                check_optional(parameter.children)

        check_optional(FIELD_PARAMETERS)
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((CommandCreatorDefinition(
                'field_settings', 'Field settings', FIELD_PARAMETERS, dict,
            ),), on_add=added.append)
        self.assertEqual(creator.values(), {})
        creator.activations['description'].set_value(True)
        creator.controls['description'].set_value('')
        creator.activations['required'].set_value(True)
        creator.controls['required'].set_value(False)
        creator._parameter_changed(None)
        creator._add_command()
        self.assertEqual(added, [{'description': '', 'required': False}])

    def test_nested_requirement_overrides_do_not_change_catalog_or_other_consumers(self):
        def find(parameters, path):
            first, *rest = path.split('.')
            parameter = next(parameter for parameter in parameters if parameter.key == first)
            return find(parameter.children, '.'.join(rest)) if rest else parameter

        path = 'option_list.values.label'
        configured = configure_field_parameters({path: {'required': True, 'default': 'Custom'}})
        self.assertTrue(find(configured, path).required)
        self.assertEqual(find(configured, path).default, 'Custom')
        for independent in (FIELD_PARAMETERS, configure_field_parameters({})):
            self.assertFalse(find(independent, path).required)
            self.assertIsNone(find(independent, path).default)
        self.assertTrue(find(ADD_FIELD_PARAMETERS, path).required)
        self.assertIsNone(find(ADD_FIELD_PARAMETERS, path).default)
        self.assertEqual(find(ADD_FIELD_PARAMETERS, 'field_type').default, 'FREE_TEXT')
        with self.assertRaisesRegex(ValueError, 'Unknown field parameter paths'):
            configure_field_parameters({'option_list.values.typo': {'required': True}})


class ExpandedEditorTests(unittest.TestCase):
    def creator(self):
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((ADD_FIELD,))
        creator.controls['field_code'].set_value('new')
        creator.controls['label'].set_value('New')
        creator._parameter_changed(None)
        return creator

    def test_required_condition_depends_on_included_true_boolean(self):
        creator = self.creator()
        self.assertFalse(creator.parameter_containers['required_condition'].visible)
        creator.activations['required'].set_value(True)
        creator._parameter_changed(None)
        self.assertFalse(creator.parameter_containers['required_condition'].visible)
        creator.controls['required'].set_value(True)
        creator._parameter_changed(None)
        self.assertTrue(creator.parameter_containers['required_condition'].visible)
        creator.activations['required_condition'].set_value(True)
        creator.controls['required_condition'].set_value('old != ""')
        creator._parameter_changed(None)
        command = ADD_FIELD.create_command(creator.values())
        self.assertEqual(command.execute(schema())['elements'][-1]['requiredCondition'], 'old != ""')
        creator.activations['required'].set_value(False)
        creator._parameter_changed(None)
        self.assertFalse(creator.parameter_containers['required_condition'].visible)
        self.assertNotIn('required_condition', creator.values())
        creator.activations['required'].set_value(True)
        creator.controls['required'].set_value(False)
        creator._parameter_changed(None)
        self.assertFalse(creator.parameter_containers['required_condition'].visible)

    def test_metadata_hides_conflicting_drafts_and_uses_current_database_users(self):
        for metadata in ('ADDED_BY', 'LAST_EDITED_BY'):
            with self.subTest(metadata=metadata):
                creator = self.creator()
                creator.controls['field_type'].set_value('reference')
                creator._parameter_changed(None)
                creator.controls['referenced_form'].set_value('other')
                for key, value in (('default_value', 'record'), ('default_formula', 'old')):
                    creator.activations[key].set_value(True)
                    creator.controls[key].set_value(value)
                lookups = next(editor for editor in creator.editors if editor.definition.key == 'lookup_fields')
                lookups.activation.set_value(True)
                lookups._change_rows(None, 0)
                lookups.rows[0][0].control.set_value('Name')
                lookups.rows[0][1].control.set_value('Name')
                creator.activations['metadata'].set_value(True)
                creator.controls['metadata'].set_value(metadata)
                creator._parameter_changed(None)
                for key in ('referenced_form', 'lookup_fields', 'default_value', 'default_formula'):
                    self.assertFalse(creator.parameter_containers[key].visible)
                    self.assertNotIn(key, creator.values())
                self.assertFalse(creator.add_button.props['disable'])
                command = ADD_FIELD.create_command(creator.values())
                source = schema()
                source['databaseId'] = 'selectedDatabase'
                field = command.execute(source)['elements'][-1]
                self.assertEqual(field['typeParameters'], {'range': [{'formId': 'selectedDatabase@users'}]})
                self.assertTrue(field['readOnly'])
                self.assertNotIn('defaultValue', field)
                self.assertNotIn('defaultValueFormula', field)
                creator.activations['metadata'].set_value(False)
                creator._parameter_changed(None)
                for key in ('referenced_form', 'lookup_fields', 'default_value', 'default_formula'):
                    self.assertTrue(creator.parameter_containers[key].visible)

    def test_type_visibility_disabled_reviewer_and_multiselect_activation(self):
        creator = self.creator()
        self.assertTrue(creator.controls['reviewer_only'].props['disable'])
        self.assertTrue(creator.activations['reviewer_only'].props['disable'])
        self.assertFalse(creator.parameter_containers['units'].visible)
        self.assertFalse(creator.parameter_containers['attachment_cardinality'].visible)
        creator.controls['field_type'].set_value('attachment')
        creator._parameter_changed(None)
        self.assertEqual(set(creator.controls['upload_type'].options), {'FILE', 'SIGNATURE', 'CAMERA', 'AUDIO'})
        self.assertEqual(len(creator.controls['allowed_file_types'].options), 5)
        self.assertTrue(creator.parameter_containers['attachment_cardinality'].visible)
        self.assertEqual(creator.controls['attachment_cardinality'].options,
                         {'single': 'Single', 'multiple': 'Multiple'})
        self.assertNotIn('attachment_cardinality', creator.values())
        creator.activations['attachment_cardinality'].set_value(True)
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        creator.controls['attachment_cardinality'].set_value('single')
        creator._parameter_changed(None)
        self.assertFalse(creator.add_button.props['disable'])
        self.assertEqual(creator.values()['attachment_cardinality'], 'single')
        self.assertNotIn('upload_type', creator.values())
        creator.activations['upload_type'].set_value(True)
        creator._parameter_changed(None)
        self.assertEqual(creator.values()['upload_type'], [])
        creator.controls['upload_type'].set_value(['FILE', 'CAMERA'])
        creator._parameter_changed(None)
        self.assertTrue(creator.parameter_containers['allowed_file_types'].visible)
        creator.controls['upload_type'].set_value(['AUDIO'])
        creator._parameter_changed(None)
        self.assertFalse(creator.parameter_containers['allowed_file_types'].visible)
        creator.set_enabled(False)
        self.assertTrue(creator.controls['upload_type'].props['disable'])
        self.assertTrue(creator.controls['attachment_cardinality'].props['disable'])

    def test_option_rows_and_cross_parameter_validation_create_command(self):
        creator = self.creator()
        creator.controls['field_type'].set_value('enumerated')
        creator._parameter_changed(None)
        options = next(editor for editor in creator.editors if editor.definition.key == 'option_list')
        self.assertFalse(options.activation.visible)
        self.assertTrue(creator.add_button.props['disable'])
        rows = options.children[2]
        rows._change_rows(None, 0)
        rows.rows[0][0].control.set_value('yes')
        rows.rows[0][1].control.set_value('Yes')
        creator._parameter_changed(None)
        self.assertFalse(creator.add_button.props['disable'])
        creator.controls['field_type'].set_value('calculated')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        creator.controls['calculation_formula'].set_value('1 + 2')
        creator._parameter_changed(None)
        self.assertFalse(creator.add_button.props['disable'])
        self.assertNotIn('option_list', creator.values())


class ExpandedWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_suggestions_ignore_stale_reads_and_preserve_custom_formulas(self):
        service = AsyncMock(spec=ActivityInfoService)
        service.get_databases.return_value = {'db1': 'Database (db1)'}
        service.get_forms.return_value = {'form1': 'Survey (form1)', 'other': 'Other (other)'}
        pending = asyncio.Future()
        started = asyncio.Event()

        async def get_schema(form_id):
            if form_id == 'slow':
                started.set()
                return await pending
            if form_id == 'form1':
                return schema()
            return {'elements': [{'code': 'Name'}, {'code': 'Code'}]}

        service.get_form_schema.side_effect = get_schema
        captured = []

        def create_renderer(*args, **kwargs):
            creator = CommandCreatorRenderer(*args, **kwargs)
            captured.append(creator)
            return creator

        with patch('activity_manager.page.ActivityInfoService', return_value=service), \
             patch('activity_manager.page.CommandCreatorRenderer', side_effect=create_renderer), \
             patch('activity_manager.command_creator.events.handle_event'):
            with Client(ui.page('/')), ui.column() as page:
                connection_page()
            creator = captured[0]
            token = next(e for e in page.descendants() if isinstance(e, ui.input) and e.label == 'API token')
            token.value = 'test-token'
            button = next(e for e in page.descendants() if isinstance(e, ui.button) and e.text == 'Connect')
            connect = next(iter(button._event_listeners.values())).handler.__closure__[0].cell_contents
            await connect()
            databases, forms = [e for e in page.descendants() if isinstance(e, DatabaseListbox)]
            await next(iter(databases._event_listeners.values())).handler(SimpleNamespace(args='db1'))
            await next(iter(forms._event_listeners.values())).handler(SimpleNamespace(args='form1'))
            creator.command_select.set_value('add_field')
            creator._command_changed(None)
            creator.controls['field_type'].set_value('reference')
            creator._parameter_changed(None)
            self.assertIn('other', creator.controls['referenced_form'].options)
            lookup = next(editor for editor in creator.editors if editor.definition.key == 'lookup_fields')
            lookup.activation.set_value(True)
            lookup._change_rows(None, 0)
            lookup.rows[0][0].control.set_value('CONCAT(Name, Code)')
            creator.controls['referenced_form'].set_value('slow')
            old = asyncio.create_task(creator.on_parameter_change(creator))
            await asyncio.wait_for(started.wait(), timeout=2)
            creator.controls['referenced_form'].set_value('other')
            await creator.on_parameter_change(creator)
            pending.set_result({'elements': [{'code': 'Stale'}]})
            await old
            self.assertEqual(lookup.rows[0][0].control.options, ['Name', 'Code'])
            self.assertEqual(lookup.rows[0][0].control.value, 'CONCAT(Name, Code)')
            creator.controls['referenced_form'].set_value('form1')
            await creator.on_parameter_change(creator)
            self.assertEqual(lookup.rows[0][0].control.options, ['old'])
            self.assertEqual(service.get_form_schema.await_count, 3)

    async def test_reverse_reference_resolves_code_on_target_form_at_write(self):
        command = AddFieldCommand('backlink', 'Backlink', field_type='reversereference',
                                  referenced_form='other', reverse_reference_field='parent')
        preview = command.execute(schema())
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        external = {'id': 'other', 'elements': [{'id': 'referenceid', 'code': 'parent', 'type': 'reference',
                                              'typeParameters': {'range': [{'formId': 'form1'}]}}]}
        service = SimpleNamespace(get_form_schema=AsyncMock(return_value=external))
        target = await prepare_schema_for_write(service, preview)
        service.get_form_schema.assert_awaited_once_with('other')
        self.assertEqual(target['elements'][-1]['typeParameters'], {'formId': 'other', 'fieldId': 'referenceid'})
        self.assertEqual(FormSchema.from_dict(target).to_dict(), target)
        await prepare_schema_for_write(service, target)
        service.get_form_schema.assert_awaited_once()
        external['elements'][0]['typeParameters']['range'][0]['formId'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'point back'):
            await prepare_schema_for_write(service, preview)

    async def test_invalid_code_resolution_stops_without_write_or_recovery_prompt(self):
        loaded = schema()
        preview = AddFieldCommand('new', 'New', translation=True, translation_field='missing',
                                  translation_language='fr').execute(loaded)
        service = SimpleNamespace(get_form_schema=AsyncMock(return_value=loaded), put_form_schema=AsyncMock())
        choose = AsyncMock()
        outcome = await apply_form_schema_change(service, 'form1', loaded, preview, choose)
        self.assertEqual(outcome.status, 'read_error')
        self.assertIn('missing', outcome.messages[0])
        service.put_form_schema.assert_not_awaited()
        choose.assert_not_awaited()

    async def test_nested_write_verifies_materialized_target_without_mutating_preview(self):
        source = schema()
        preview = AddFieldCommand('choice', 'Choice', field_type='enumerated',
                                  option_list=OPTIONS, default_value=['yes']).execute(source)
        written = None

        async def write(form_id, target):
            nonlocal written
            written = deepcopy(target)

        async def read(form_id):
            return source if written is None else written

        service = SimpleNamespace(get_form_schema=AsyncMock(side_effect=read),
                                  put_form_schema=AsyncMock(side_effect=write))
        outcome = await apply_form_schema_change(service, 'form1', source, preview, AsyncMock())
        self.assertEqual(outcome.status, 'success')
        self.assertTrue(preview['elements'][-1]['id'].startswith('pending-field:'))
        field = written['elements'][-1]
        self.assertEqual(field['defaultValue'], [field['typeParameters']['values'][0]['id']])
