import unittest

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.commands import ADD_FIELD
from activity_manager.command_parser import parse_command_text
from activity_manager.field_parameters import parameter_applies
from activity_manager.schema_identity import prepare_schema_for_put


class FieldApplicabilityTests(unittest.TestCase):
    def creator(self, kind):
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((ADD_FIELD,))
        creator.controls['field_type'].set_value(kind)
        creator._parameter_changed(None)
        if kind in ('FREE_TEXT', 'reference', 'enumerated'):
            creator.controls['field_variant'].set_value({'FREE_TEXT': 'text', 'reference': 'reference', 'enumerated': 'single'}[kind])
        creator.controls['field_code'].set_value('new')
        creator.controls['label'].set_value('New')
        creator._parameter_changed(None)
        return creator

    def test_common_controls_match_confirmed_profiles(self):
        # Expected profiles are independent of the implementation's type sets.
        profiles = {
            'FREE_TEXT': (True, True, True, True, True, True, True, True, True, True, True),
            'NARRATIVE': (False, False, True, True, True, True, True, True, True, True, False),
            'quantity': (False, False, True, True, True, True, True, True, True, True, False),
            'date': (True, True, True, True, True, True, True, True, True, True, False),
            'month': (True, True, True, True, True, True, True, True, True, False, False),
            'epiweek': (True, True, True, True, True, True, True, True, True, False, False),
            'fortnight': (True, True, True, True, True, True, True, True, True, False, False),
            'reference': (True, True, True, True, True, True, True, True, True, False, False),
            'multiselectreference': (False, False, True, True, True, True, True, False, False, False, False),
            'attachment': (False, False, True, True, False, True, True, False, False, False, False),
            'geopoint': (False, False, True, True, True, True, True, True, True, False, False),
            'serial': (True, True, False, True, True, False, False, False, False, False, False),
            'calculated': (False, True, False, True, True, True, False, False, False, False, False),
            'reversereference': (False, False, False, False, True, False, False, False, False, False, False),
            'section': (False, False, False, False, False, True, False, False, False, False, False),
            'note': (False, False, False, False, False, True, False, False, False, False, False),
        }
        keys = ('key', 'unique', 'required', 'hide_from_entry', 'hide_in_table',
                'relevance_condition', 'validation_condition', 'read_only',
                'default_value', 'default_formula', 'translation')
        for kind, expected in profiles.items():
            creator = self.creator(kind)
            for key, supported in zip(keys, expected, strict=True):
                with self.subTest(kind=kind, parameter=key):
                    self.assertEqual(creator.parameter_containers[key].visible, supported)
            self.assertEqual(creator.parameter_containers['validation_message'].visible,
                             creator.parameter_containers['validation_condition'].visible)
            creator.controls['required'].set_value(True)
            creator._parameter_changed(None)
            self.assertEqual(creator.parameter_containers['required_condition'].visible,
                             creator.parameter_containers['required'].visible)

    def test_barcode_discards_translation_and_keeps_defaults(self):
        creator = self.creator('FREE_TEXT')
        creator.activations['translation'].set_value(True)
        creator.controls['translation'].set_value(True)
        creator._parameter_changed(None)
        creator.controls['translation_field'].set_value('translated')
        creator.controls['translation_language'].set_value('fr')
        creator.controls['field_variant'].set_value('barcode')
        creator._parameter_changed(None)
        for key in ('translation', 'translation_field', 'translation_language'):
            self.assertFalse(creator.parameter_containers[key].visible)
            self.assertNotIn(key, creator.values())
        self.assertTrue(creator.parameter_containers['default_value'].visible)
        self.assertTrue(creator.parameter_containers['default_formula'].visible)

    def test_serial_flags_are_checked_locked_and_written(self):
        creator = self.creator('serial')
        for key in ('key', 'unique'):
            self.assertTrue(creator.controls[key].value)
            self.assertTrue(creator.controls[key].props['disable'])
            self.assertFalse(creator.activations[key].visible)
        field = ADD_FIELD.create_command(creator.values()).execute({'elements': []}).schema['elements'][0]
        self.assertIs(field['key'], True)
        self.assertIs(field['unique'], True)
        creator.controls['field_type'].set_value('FREE_TEXT')
        creator._parameter_changed(None)
        creator.controls['field_variant'].set_value('text')
        creator._parameter_changed(None)
        for key in ('key', 'unique'):
            self.assertNotIn(key, creator.values())
            self.assertTrue(creator.activations[key].visible)

    def test_structural_fields_create_without_codes_and_have_unique_pending_ids(self):
        source = {'elements': []}
        for kind in ('section', 'note', 'note'):
            creator = self.creator(kind)
            self.assertFalse(creator.parameter_containers['field_code'].visible)
            command = ADD_FIELD.create_command(creator.values())
            self.assertIsNone(command.field_code)
            self.assertEqual(parse_command_text(command.to_syntax()), [command])
            result = command.execute(source)
            self.assertFalse(result.has_errors, result.message)
            source = result.schema
            self.assertNotIn('code', source['elements'][-1])
        self.assertEqual(len({element['id'] for element in source['elements']}), 3)
        target = prepare_schema_for_put(source)
        self.assertEqual(len({element['id'] for element in target['elements']}), 3)
        self.assertTrue(all(not element['id'].startswith('pending-') for element in target['elements']))
        self.assertTrue(all('code' not in element for element in target['elements']))
        duplicate = {'elements': [dict(source['elements'][0]), dict(source['elements'][0])]}
        with self.assertRaises(ValueError):
            prepare_schema_for_put(duplicate)

    def test_enumerated_cardinality_changes_settings_and_presentation(self):
        creator = self.creator('enumerated')
        options = next(editor for editor in creator.editors if editor.definition.key == 'option_list')
        cardinality, presentation, rows = options.children
        rows._change_rows(None, 0)
        rows.rows[0][0].control.set_value('Yes')
        creator._parameter_changed(None)
        self.assertFalse(presentation.container.visible)
        self.assertTrue(creator.parameter_containers['key'].visible)
        command = ADD_FIELD.create_command(creator.values())
        self.assertEqual(command.option_list['presentation'], 'automatic')
        creator.controls['field_variant'].set_value('multiple')
        creator._parameter_changed(None)
        self.assertTrue(presentation.container.visible)
        for key in ('key', 'unique', 'default_formula'):
            self.assertFalse(creator.parameter_containers[key].visible)
            self.assertNotIn(key, creator.values())
        presentation.control.set_value(True)
        creator._parameter_changed(None)
        self.assertEqual(ADD_FIELD.create_command(creator.values()).option_list['presentation'], 'dropdown')
        creator.controls['field_variant'].set_value('single')
        creator._parameter_changed(None)
        self.assertEqual(ADD_FIELD.create_command(creator.values()).option_list['presentation'], 'automatic')

    def test_user_reference_excludes_lookups(self):
        creator = self.creator('reference')
        creator.controls['field_variant'].set_value('user')
        creator._parameter_changed(None)
        self.assertFalse(creator.parameter_containers['lookup_fields'].visible)
        self.assertTrue(creator.parameter_containers['unique'].visible)
        self.assertTrue(creator.parameter_containers['default_value'].visible)
        creator.controls['field_variant'].set_value('reference')
        creator._parameter_changed(None)
        self.assertTrue(creator.parameter_containers['lookup_fields'].visible)

    def test_metadata_only_exposes_identity_and_hides(self):
        creator = self.creator('reference')
        creator.controls['field_variant'].set_value('metadata')
        creator._parameter_changed(None)
        creator.controls['metadata'].set_value('ADDED_BY')
        creator._parameter_changed(None)
        visible = {key for key, container in creator.parameter_containers.items() if container.visible}
        self.assertEqual(visible, {'position', 'field_type', 'field_variant', 'metadata', 'label', 'field_code',
                                   'hide_from_entry', 'hide_in_table'})

    def test_shared_policy_does_not_require_creation_defaults(self):
        self.assertFalse(parameter_applies('translation', {'field_type': 'NARRATIVE'}))
        self.assertFalse(parameter_applies('default_formula', {'field_type': 'reference'}))


if __name__ == '__main__':
    unittest.main()
