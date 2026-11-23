from copy import deepcopy
import unittest
from unittest.mock import patch

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands import ADD_FIELD, MODIFY_FIELD, ModifyFieldCommand
from activity_manager.functions import FunctionCall


class ModifyFieldCreatorTests(unittest.TestCase):
    def creator(self, kind='quantity', variant=None, definition=MODIFY_FIELD):
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((definition,))
        creator.controls['field_type'].set_value(kind)
        creator._parameter_changed(None)
        if variant:
            creator.controls['field_variant'].set_value(variant)
        creator.controls['field_code'].set_value('Target label or code')
        if definition is ADD_FIELD:
            creator.controls['field_code'].set_value('new')
            creator.controls['label'].set_value('New')
        creator._parameter_changed(None)
        return creator

    def command(self, creator):
        return creator.definitions[creator.command_select.value].create_command(creator.values())

    def test_target_only_and_schema_independence(self):
        creator = self.creator()
        expected = {'field_code': 'Target label or code', 'field_type': 'quantity'}
        self.assertEqual(creator.values(), expected)
        creator.set_schema({'elements': [{'code': 'Target label or code', 'label': 'Existing', 'type': 'date', 'units': 'kg'}]})
        self.assertEqual(creator.values(), expected)
        creator.activations['units'].set_value(True)
        creator._parameter_changed(None)
        self.assertIsNone(self.command(creator).changes['units'])

    def test_target_selection_populates_editable_selectors(self):
        creator = self.creator()
        creator.set_schema({'elements': [
            {'code': 'choices', 'type': 'enumerated', 'typeParameters': {'cardinality': 'multiple'}},
            {'code': 'barcode', 'type': 'FREE_TEXT', 'typeParameters': {'barcode': True}},
            {'code': 'date', 'type': 'date'},
            {'code': 'owner', 'label': 'Owner', 'type': 'reference',
             'typeParameters': {'range': [{'formId': 'db@users'}]}},
        ]})
        for target, kind, variant in [('choices', 'enumerated', 'multiple'),
                                      ('barcode', 'FREE_TEXT', 'barcode'),
                                      ('Owner', 'reference', 'user'), ('date', 'date', None)]:
            creator.controls['field_code'].set_value(target)
            creator._parameter_changed(None)
            self.assertEqual(creator.controls['field_type'].value, kind)
            self.assertEqual(creator.controls['field_variant'].value, variant)
            self.assertFalse(creator.controls['field_type'].props.get('disable', False))
            if variant:
                self.assertFalse(creator.controls['field_variant'].props.get('disable', False))
        creator.controls['field_type'].set_value('quantity')
        creator._parameter_changed(None)
        self.assertEqual(creator.controls['field_type'].value, 'quantity')
        creator.controls['field_code'].set_value('unknown')
        creator._parameter_changed(None)
        self.assertEqual(creator.controls['field_type'].value, 'quantity')

    def test_target_defaults_prefer_code_and_skip_ambiguous_labels(self):
        from activity_manager.commands.modify_field import target_defaults
        schema = {'elements': [
            {'code': 'x', 'label': 'Duplicate', 'type': 'date'},
            {'code': 'y', 'label': 'Duplicate', 'type': 'quantity'},
            {'code': 'z', 'label': 'x', 'type': 'quantity'},
        ]}
        self.assertEqual(target_defaults(schema, 'x'), {'field_type': 'date', 'field_variant': None})
        self.assertEqual(target_defaults(schema, 'Duplicate'), {})

    def test_creator_recognizes_functions_without_evaluating_them(self):
        creator = self.creator()
        for key in ('new_code', 'description', 'validation_condition', 'default_value'):
            creator.activations[key].set_value(True)
            creator.controls[key].set_value('rEpLaCe("old", "new")')
        creator._parameter_changed(None)
        self.assertFalse(creator.add_button.props.get('disable', False))
        added = []
        creator.on_add = added.append
        with patch('activity_manager.commands.modify_field.evaluate_function',
                   side_effect=AssertionError('Creation must not run functions')):
            creator._add_command()
        self.assertEqual(len(added), 1)
        command = added[0]
        for key in ('new_code', 'description', 'validation_condition', 'default_value'):
            self.assertEqual(command.changes[key], FunctionCall('Replace', ['old', 'new']))
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        self.assertIn('newCode=Replace("old", "new")', command.to_syntax())

    def test_creator_replacement_uses_current_names_and_preserves_source(self):
        creator = self.creator()
        creator.controls['field_code'].set_value('old_code')
        for key in ('new_code', 'validation_condition'):
            creator.activations[key].set_value(True)
            creator.controls[key].set_value('Replace("old", "new")')
        creator._parameter_changed(None)
        source = {'id': 'form', 'elements': [{'id': 'fieldid', 'code': 'old_code',
            'type': 'quantity', 'label': 'Target', 'validationCondition': 'fieldid > 0'}]}
        original = deepcopy(source)
        result = self.command(creator).execute(source)
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(result.schema['elements'][0]['code'], 'new_code')
        self.assertEqual(result.schema['elements'][0]['validationCondition'], 'new_code > 0')
        self.assertEqual(source, original)

    def test_creator_invalid_function_syntax_disables_add(self):
        creator = self.creator()
        creator.activations['description'].set_value(True)
        for text in ('Replace(', 'Replace("old")', 'Replace("old", 1)',
                     'Replace(FindText="old", ReplaceText="new")',
                     'Replace(*["old", "new"])', 'Replace(__import__("os"), "new")',
                     'Replace("old", "new") + "suffix"'):
            with self.subTest(text=text):
                creator.controls['description'].set_value(text)
                creator._parameter_changed(None)
                self.assertTrue(creator.add_button.props['disable'])
                self.assertTrue(creator.error.text)
        creator.controls['description'].set_value('Replace("old", "new")')
        creator._parameter_changed(None)
        self.assertFalse(creator.add_button.props.get('disable', False))
        self.assertEqual(creator.error.text, '')

    def test_creator_preserves_ordinary_formulas_and_explicit_literal_calls(self):
        creator = self.creator()
        creator.activations['validation_condition'].set_value(True)
        for text in ('SUM(old) > 0', '  ordinary text  ', '"old" == "new"'):
            with self.subTest(text=text):
                creator.controls['validation_condition'].set_value(text)
                creator._parameter_changed(None)
                self.assertEqual(self.command(creator).changes['validation_condition'], text)
        creator.controls['validation_condition'].set_value(repr('Replace("old", "new")'))
        creator._parameter_changed(None)
        command = self.command(creator)
        self.assertEqual(command.changes['validation_condition'], 'Replace("old", "new")')
        self.assertEqual(parse_command_text(command.to_syntax()), [command])

    def test_creator_limits_function_parsing_to_permitted_inputs(self):
        creator = self.creator('FREE_TEXT', 'text')
        self.assertIsNone(creator.editors[0].definition.parse_text)
        for key in ('translation_field', 'translation_language'):
            creator.activations[key].set_value(True)
            creator.controls[key].set_value('Replace("old", "new")')
        creator._parameter_changed(None)
        command = self.command(creator)
        self.assertIsInstance(command.changes['translation_field'], FunctionCall)
        self.assertIsInstance(command.changes['translation_language'], FunctionCall)
        added = self.creator('quantity', definition=ADD_FIELD)
        added.activations['description'].set_value(True)
        added.controls['description'].set_value('Replace("old", "new")')
        added._parameter_changed(None)
        self.assertEqual(self.command(added).description, 'Replace("old", "new")')

    def test_clear_false_and_omission_are_distinct_and_roundtrip(self):
        creator = self.creator()
        for key in ('label', 'description', 'new_code', 'geo_accuracy', 'required'):
            creator.activations[key].set_value(True)
        creator.activations['units'].set_value(True)
        creator.controls['units'].set_value('people')
        creator._parameter_changed(None)
        command = self.command(creator)
        self.assertEqual(command.changes, {'new_code': None, 'label': None, 'description': None,
                                           'required': False, 'units': 'people'})
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        self.assertNotIn('key', command.changes)
        self.assertIn('newCode=""', command.to_syntax())

    def test_required_condition_and_translation_children_are_independent(self):
        creator = self.creator('FREE_TEXT', 'text')
        for key in ('required_condition', 'translation_field', 'translation_language'):
            self.assertTrue(creator.parameter_containers[key].visible)
            creator.activations[key].set_value(True)
        creator._parameter_changed(None)
        self.assertEqual(self.command(creator).changes,
                         {'required_condition': None, 'translation_field': None, 'translation_language': None})
        creator.controls['field_variant'].set_value('barcode')
        creator._parameter_changed(None)
        self.assertEqual(self.command(creator).changes, {'required_condition': None})

    def test_variants_required_and_reset_when_type_changes(self):
        creator = self.creator('enumerated')
        self.assertTrue(creator.add_button.props['disable'])
        creator.controls['field_variant'].set_value('multiple')
        creator._parameter_changed(None)
        self.assertFalse(creator.add_button.props['disable'])
        self.assertNotIn('key', creator.values())
        creator.controls['field_type'].set_value('reference')
        creator._parameter_changed(None)
        self.assertIsNone(creator.controls['field_variant'].value)
        self.assertEqual(creator.controls['field_variant'].options, ['reference', 'user', 'metadata'])
        self.assertTrue(creator.add_button.props['disable'])

    def test_immutable_properties_absent_and_metadata_has_only_allowed_edits(self):
        creator = self.creator('reference', 'metadata')
        for key in ('barcode', 'metadata', 'reverse_reference_field', 'attachment_cardinality'):
            self.assertNotIn(key, creator.controls)
        visible = {key for key, container in creator.parameter_containers.items() if container.visible}
        self.assertEqual(visible, {'field_code', 'field_type', 'field_variant', 'new_code', 'label',
                                   'hide_from_entry', 'hide_in_table', 'referenced_form'})
        self.assertIsNone(self.command(creator).referenced_form)
        serial = self.creator('serial')
        self.assertNotIn('key', self.command(serial).changes)
        self.assertFalse(serial.parameter_containers['key'].visible)

    def test_note_target_is_fieldcode_and_label_is_only_an_edit(self):
        creator = self.creator('note')
        self.assertFalse(creator.parameter_containers['new_code'].visible)
        creator.activations['label'].set_value(True)
        creator.controls['label'].set_value('Replacement label')
        creator._parameter_changed(None)
        command = self.command(creator)
        self.assertEqual(command.field_code, 'Target label or code')
        self.assertEqual(command.changes, {'label': 'Replacement label'})

    def test_referenced_form_is_an_optional_match_selector(self):
        creator = self.creator('reference', 'reference')
        creator.activations['referenced_form'].set_value(True)
        creator.controls['referenced_form'].set_value('Directory/People')
        creator._parameter_changed(None)
        command = self.command(creator)
        self.assertEqual(command.referenced_form, 'Directory/People')
        self.assertEqual(command.changes, {})
        self.assertEqual(parse_command_text(command.to_syntax()), [command])

    def test_partial_option_edits_and_empty_collection_clear(self):
        creator = self.creator('enumerated', 'multiple')
        group = next(editor for editor in creator.editors if editor.definition.key == 'option_list')
        group.activation.set_value(True)
        presentation, options = group.children
        presentation.activation.set_value(True)
        presentation.control.set_value(True)
        creator._parameter_changed(None)
        self.assertEqual(self.command(creator).changes, {'option_list': {'presentation': 'dropdown'}})
        options.activation.set_value(True)
        creator._parameter_changed(None)
        self.assertEqual(self.command(creator).changes['option_list']['values'], None)
        options._change_rows(None, 0)
        self.assertTrue(creator.add_button.props['disable'])
        self.assertEqual(len(options.rows[0]), 1)
        options.rows[0][0].control.set_value('Yes')
        creator._parameter_changed(None)
        command = self.command(creator)
        self.assertEqual(command.changes['option_list']['values'], [{'label': 'Yes'}])
        self.assertEqual(parse_command_text(command.to_syntax()), [command])

    def test_lookup_rows_require_both_and_user_has_no_lookups(self):
        creator = self.creator('reference', 'reference')
        lookups = next(editor for editor in creator.editors if editor.definition.key == 'lookup_fields')
        lookups.activation.set_value(True)
        creator._parameter_changed(None)
        self.assertEqual(self.command(creator).changes, {'lookup_fields': None})
        lookups._change_rows(None, 0)
        lookups.rows[0][0].control.set_value('Name')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        lookups.rows[0][1].control.set_value('name')
        creator._parameter_changed(None)
        self.assertEqual(self.command(creator).changes['lookup_fields'], [{'label': 'Name', 'formula': 'name'}])
        creator.controls['field_variant'].set_value('user')
        creator._parameter_changed(None)
        self.assertNotIn('lookup_fields', creator.values())

    def test_add_field_uses_same_variants_and_label_only_options(self):
        creator = self.creator('enumerated', 'single', ADD_FIELD)
        group = next(editor for editor in creator.editors if editor.definition.key == 'option_list')
        rows = next(child for child in group.children if child.definition.key == 'values')
        rows._change_rows(None, 0)
        rows.rows[0][0].control.set_value('Yes')
        creator._parameter_changed(None)
        command = self.command(creator)
        self.assertEqual(command.option_list, {'cardinality': 'single', 'presentation': 'automatic', 'values': [{'label': 'Yes'}]})
        result = command.execute({'elements': []})
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(result.schema['elements'][0]['typeParameters']['values'],
                         [{'id': 'pending-option:new:0', 'label': 'Yes'}])
        self.assertEqual(parse_command_text(command.to_syntax()), [command])
        user = self.command(self.creator('reference', 'user', ADD_FIELD))
        result = user.execute({'elements': [], 'databaseId': 'db'})
        self.assertFalse(result.has_errors, result.message)
        self.assertEqual(result.schema['elements'][0]['typeParameters']['range'], [{'formId': 'db@users'}])

    def test_rejects_bad_literal_shapes_without_applying_schema(self):
        for arguments in (
            'fieldCode="x", fieldType="FREE_TEXT"',
            'fieldCode="x", fieldType="date", fieldVariant="text"',
            'fieldCode="x", fieldType="reference", fieldVariant="user", referencedForm=""',
            'fieldCode="x", fieldType="reference", fieldVariant="user", referencedForm=Append("End", "", "other")',
            'fieldCode="x", fieldType="quantity", required=0',
            'fieldCode="x", fieldType="quantity", newCode=123',
            'fieldCode="x", fieldType="enumerated", fieldVariant="single", optionList={"values": [{"code": "x", "label": "X"}]}',
            'fieldCode="x", fieldType="reference", fieldVariant="reference", lookupFields=[{"label": "Name"}]',
            'fieldCode="x", fieldType="quantity", label=__import__("os")',
            'fieldCode="x", fieldType="geopoint", defaultValue=' + repr('{"latitude": NaN, "longitude": 0}'),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(CommandTextError):
                parse_command_text('ModifyField(' + arguments + ')')

    def test_nonfinite_json_defaults_report_value_error(self):
        arguments = 'fieldCode="x", fieldType="geopoint", defaultValue=' + repr('{"latitude": NaN, "longitude": 0}')
        with self.assertRaisesRegex(CommandTextError, 'finite'):
            parse_command_text('ModifyField(' + arguments + ')')

    def test_nested_empty_collection_text_means_remove(self):
        command = parse_command_text('ModifyField(fieldCode="x", fieldType="enumerated", '
                                     'fieldVariant="multiple", optionList={"values": []})')[0]
        self.assertEqual(command.changes, {'option_list': {'values': None}})
        self.assertEqual(parse_command_text(command.to_syntax()), [command])

    def test_command_list_roundtrip_applies_without_mutating_source(self):
        creator = self.creator()
        creator.activations['description'].set_value(True)
        creator.controls['description'].set_value('Changed')
        creator._parameter_changed(None)
        command = self.command(creator)
        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(on_execute=lambda: None)
        source = {'elements': [{'id': 'q', 'code': 'quantity', 'label': 'Target label or code', 'type': 'quantity'}]}
        before = deepcopy(source)
        manager.set_schema(source)
        manager.add_command(command)
        self.assertEqual(manager.executions[0].status, 'applied')
        self.assertFalse(manager.execute_button.props['disable'])
        self.assertEqual(source, before)
        with patch.object(ModifyFieldCommand, 'execute', side_effect=AssertionError('must not execute')):
            self.assertEqual(parse_command_text(command.to_syntax()), [command])


if __name__ == '__main__':
    unittest.main()
