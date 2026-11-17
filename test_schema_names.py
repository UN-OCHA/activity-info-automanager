from copy import deepcopy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from nicegui.elements.markdown import prepare_content

from activity_info_api_client.models.form_schema import FormSchema
from activity_manager.schema_change import compare_schemas
from activity_manager.schema_names import (
    readable_schema, schemas_from_tree, substitute_record_id,
    load_default_record_labels,
)
from activity_manager.schema_display import underline_substitutions
import json


class SchemaNamesTests(unittest.TestCase):
    def setUp(self):
        self.remote = {'id': 'remote', 'label': 'People', 'databaseId': 'db2', 'elements': [
            {'id': 'same', 'code': 'NAME', 'type': 'FREE_TEXT', 'label': 'Name'},
            {'id': 'next', 'code': 'TEAM', 'type': 'reference', 'label': 'Team',
             'typeParameters': {'range': [{'formId': 'third'}]}},
        ]}
        self.third = {'id': 'third', 'label': 'Teams', 'databaseId': 'db1', 'elements': [
            {'id': 'same', 'code': 'TITLE', 'type': 'FREE_TEXT', 'label': 'Title'}]}
        self.schema = {'id': 'local', 'schemaVersion': 1, 'label': 'Local', 'databaseId': 'db1', 'elements': [
            {'id': 'same', 'code': 'PERSON', 'type': 'reference', 'label': 'Person',
             'typeParameters': {'range': [{'formId': 'remote'}], 'lookupConfigs': [
                 {'id': 'lookup', 'formula': 'same', 'lookupLabel': 'Name'}]}},
            {'id': 'calc', 'code': 'CALC', 'type': 'calculated', 'label': 'Calculated',
             'typeParameters': {'formula': 'same.next.same + PERSON.NAME + "same.next.same" + unknown'}},
            {'id': 'text', 'code': 'TEXT', 'type': 'FREE_TEXT', 'label': 'Text',
             'typeParameters': {'translationConfig': {'fieldId': 'same', 'languages': ['en']}}},
            {'id': 'back', 'code': 'BACK', 'type': 'reversereference', 'label': 'Back',
             'typeParameters': {'formId': 'remote', 'fieldId': 'same'}},
            {'id': 'options', 'code': 'OPTIONS', 'type': 'enumerated', 'label': 'Options',
             'typeParameters': {'values': [{'id': 'same', 'label': 'Option'}]}},
        ]}
        for element in self.schema['elements']:
            element['required'] = False
        self.related = {'local': {'elements': []}, 'remote': self.remote, 'third': self.third}

    def test_contextual_resolution_and_preservation(self):
        before = deepcopy(self.schema)
        names = readable_schema(self.schema, self.related, {'db2': 'Remote DB'})
        fields = names['elements']
        self.assertEqual(fields[1]['typeParameters']['formula'],
                         'PERSON.TEAM.TITLE + PERSON.NAME + "same.next.same" + unknown')
        self.assertEqual(fields[0]['typeParameters']['lookupConfigs'][0]['formula'], 'NAME')
        self.assertEqual(fields[0]['typeParameters']['lookupConfigs'][0]['id'], 'lookup')
        self.assertEqual(fields[0]['typeParameters']['range'][0]['formId'], 'Remote DB/People')
        self.assertEqual(fields[2]['typeParameters']['translationConfig']['fieldId'], 'PERSON')
        self.assertEqual(fields[3]['typeParameters']['fieldId'], 'NAME')
        self.assertEqual(fields[4]['typeParameters']['values'][0]['id'], 'same')
        self.assertEqual(fields[0]['id'], 'same')
        self.assertEqual(self.schema, before)

    def test_comparison_equates_only_formulas(self):
        codes = readable_schema(self.schema, self.related, formulas_only=True)
        comparison = compare_schemas(FormSchema.from_dict(self.schema),
                                     FormSchema.from_dict(codes), self.related)
        self.assertTrue(comparison.matches, comparison.summary)
        codes['elements'][1]['typeParameters']['formula'] += ' + 1'
        self.assertFalse(compare_schemas(FormSchema.from_dict(self.schema),
                                        FormSchema.from_dict(codes), self.related).matches)

    def test_unresolved_and_literals(self):
        self.schema['elements'][1]['typeParameters']['formula'] = "same.unknown.same + 'same' + [same]"
        names = readable_schema(self.schema)
        self.assertEqual(names['elements'][1]['typeParameters']['formula'],
                         "PERSON.unknown.same + 'same' + PERSON")
        self.assertEqual(names['elements'][0]['typeParameters']['range'][0]['formId'], 'remote')

    def test_tree_extraction(self):
        self.assertEqual(schemas_from_tree({'forms': {'remote': {'schema': self.remote}}}),
                         {'remote': self.remote})

    def test_defaults_and_underlines_are_scoped_and_escaped(self):
        self.schema['elements'][4]['defaultValue'] = ['same', 'missing']
        self.schema['elements'][4]['typeParameters']['values'][0]['label'] = '<Option> & "label"'
        self.schema['elements'][0]['defaultValue'] = ['remote:record1', 'remote:unknown']
        names = readable_schema(self.schema, self.related, {'db2': 'Remote DB'},
                                record_labels={('remote', 'record1'): '<Person>'})
        self.assertEqual(names['elements'][4]['defaultValue'], ['<Option> & "label"', 'missing'])
        self.assertEqual(names['elements'][0]['defaultValue'],
                         ['Remote DB/People:<Person>', 'Remote DB/People:unknown'])
        highlighted = prepare_content('```json\n' + json.dumps(names, indent=2, ensure_ascii=False)
                                      + '\n```', extras='fenced-code-blocks')
        rendered = underline_substitutions(highlighted, names)
        self.assertIn('<u>', rendered)
        self.assertIn('class="codehilite"', rendered)
        self.assertIn('class="s2"', rendered)
        self.assertNotIn('title=', rendered)
        self.assertNotIn('<u>unknown', rendered)
        self.assertNotIn('<u>missing', rendered)
        self.assertNotIn('<Person>', rendered)
        self.assertNotIn('<Option>', rendered)
        self.assertIn('&lt;Person&gt;', rendered)
        # Each formula segment retains its own ID, even with repeated field IDs.
        formula = names['elements'][1]['typeParameters']['formula']
        self.assertEqual(formula.spans[:3], ((0, 6, 'same'), (7, 11, 'next'), (12, 17, 'same')))
        self.assertEqual(self.schema['elements'][0]['defaultValue'][0], 'remote:record1')


class RecordLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_id_is_quoted_as_one_filter_literal(self):
        record_id = 'record"\\id\n'
        service = SimpleNamespace(query_rows=AsyncMock(return_value=[{'name': 'Label'}]))
        self.assertEqual(await substitute_record_id(
            service, 'people', record_id, {'people': {'recordLabelFieldId': 'name'}}), 'Label')
        query = service.query_rows.await_args.args[0]
        self.assertTrue(query['filter'].startswith('_id == '))
        self.assertEqual(json.loads(query['filter'].removeprefix('_id == ')), record_id)

    async def test_label_lookup_cache_and_missing_label_field(self):
        service = SimpleNamespace(query_rows=AsyncMock(return_value=[{'name': 'Alice'}]))
        forms = {'people': {'recordLabelFieldId': 'name'}, 'unlabelled': {}}
        cache = {}
        for _ in range(2):
            self.assertEqual(await substitute_record_id(service, 'people', 'record', forms, cache), 'Alice')
        service.query_rows.assert_awaited_once_with({
            'rowSources': [{'rootFormId': 'people'}],
            'columns': [{'id': 'name', 'formula': 'name'}],
            'filter': '_id == \"record\"',
        })
        self.assertEqual(await substitute_record_id(service, 'unlabelled', 'record', forms, cache), 'record')
        service.query_rows.assert_awaited_once()

    async def test_unavailable_or_missing_values_keep_ids(self):
        for result in ([], [{}], [{'name': None}], [{'name': []}], [{'name': 'A'}, {'name': 'B'}]):
            service = SimpleNamespace(query_rows=AsyncMock(return_value=result))
            self.assertEqual(await substitute_record_id(service, 'f', 'r', {'f': {'recordLabelFieldId': 'name'}}), 'r')
        service = SimpleNamespace(query_rows=AsyncMock(side_effect=ValueError('HTTP 403')))
        self.assertEqual(await substitute_record_id(service, 'f', 'r', {'f': {'recordLabelFieldId': 'name'}}), 'r')

    async def test_scalar_and_multiple_reference_defaults_and_local_schema(self):
        schema = {'id': 'f', 'recordLabelFieldId': 'name', 'elements': [
            {'type': 'reference', 'defaultValue': 'f:r'},
            {'type': 'multiselectreference', 'defaultValue': ['f:r', 'other:r', 'invalid']},
            {'type': 'FREE_TEXT', 'defaultValue': 'f:ignored'},
        ]}
        service = SimpleNamespace(query_rows=AsyncMock(return_value=[{'name': 'Label'}]))
        labels = await load_default_record_labels(service, schema, {'f': {}}, {})
        self.assertEqual(labels, {('f', 'r'): 'Label', ('other', 'r'): 'r'})
        service.query_rows.assert_awaited_once_with({
            'rowSources': [{'rootFormId': 'f'}],
            'columns': [{'id': 'name', 'formula': 'name'}],
            'filter': '_id == \"r\"',
        })
