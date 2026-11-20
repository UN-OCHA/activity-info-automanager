from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import unittest

from activity_manager.application_results import DisplaySnapshotBuilder
from activity_manager.immutable import AnnotatedText, freeze, thaw
from activity_manager.resource_state import (
    ApplicationState, BaselineEvidence, DatabaseResourcesPayload, Existence,
    FormPayload, Lifecycle, LoadStatus, ResourceKey, ResourceState,
)
from activity_manager.schema_names import SubstitutedText
from test.test_multi_form import form


class ResourceStateTests(unittest.TestCase):
    def test_inputs_outputs_and_substitution_annotations_are_isolated(self):
        label = SubstitutedText('Alice', [(0, 5, 'record')])
        source = {'elements': [{'label': label, 'options': ['one']}]}
        frozen = freeze(source)
        source['elements'][0]['options'].append('two')
        label.spans = ()
        detached = thaw(frozen)
        self.assertEqual(detached['elements'][0]['options'], ['one'])
        self.assertEqual(detached['elements'][0]['label'].spans, ((0, 5, 'record'),))
        detached['elements'][0]['options'].clear()
        detached['elements'][0]['label'].spans = ()
        self.assertTrue(thaw(frozen)['elements'][0]['label'].spans)
        with self.assertRaises(TypeError):
            frozen['elements'][0]['label'] = 'changed'
        with self.assertRaises(FrozenInstanceError):
            frozen._items = ()
        spans = [[0, 5, 'record']]
        annotation = AnnotatedText('Alice', spans)
        spans[0][0] = 4
        self.assertEqual(annotation.spans, ((0, 5, 'record'),))

    def test_sdk_and_arbitrary_mutable_objects_are_rejected(self):
        with self.assertRaises(TypeError):
            freeze({'model': SimpleNamespace(id='x')})
        with self.assertRaises(TypeError):
            FormPayload(['not', 'a', 'schema'])

    def test_revisions_preserve_baseline_and_local_predecessors(self):
        source = form('a', 'A')
        state = ApplicationState()
        baseline = state.load_form(source, affected=True)
        source['label'] = 'mutated input'
        edited = baseline.payload.to_dict()
        edited['label'] = 'Local'
        self.assertEqual(state.put_form(edited).revision, 1)
        self.assertEqual(baseline.payload.schema['label'], 'A')
        self.assertEqual(state.load_form(form('a', 'A')).payload.schema['label'], 'Local')
        self.assertEqual(state.put_form(dict(reversed(list(edited.items())))).revision, 1)
        self.assertEqual(state.baselines.entries[baseline.key].state, baseline)
        boolean = state.put_form({**edited, 'customValue': False})
        numeric = state.put_form({**edited, 'customValue': 0})
        self.assertEqual(numeric.revision, boolean.revision + 1)
        with self.assertRaises(TypeError):
            state.resources[baseline.key] = baseline
        with self.assertRaises(ValueError):
            changed = replace(baseline, payload=FormPayload(form('a', 'Remote')))
            state.load(BaselineEvidence(baseline.key, Existence.PRESENT, changed))

    def test_unavailable_is_not_absence_and_can_later_load(self):
        state = ApplicationState()
        key = ResourceKey.form('a')
        self.assertEqual(state.note_unavailable(key).load_status, LoadStatus.UNAVAILABLE)
        self.assertNotIn(key, state.baselines.entries)
        state.mark_affected(key)
        unknown = DisplaySnapshotBuilder(1).capture(state, {}, {}, {})
        self.assertEqual(unknown.form_payloads(), {})
        self.assertEqual(unknown.resources[0].state.load_status, LoadStatus.UNAVAILABLE)
        with self.assertRaises(ValueError):
            BaselineEvidence(key, Existence.UNKNOWN)
        state.load_form(form('a', 'A'))
        self.assertEqual(state.resources[key].load_status, LoadStatus.LOADED)
        projected = state.put_form(form('a', 'Local'))
        self.assertEqual(state.note_unavailable(key), projected)
        absent = BaselineEvidence(ResourceKey.form('new'), Existence.ABSENT)
        state.load(absent)
        self.assertEqual(state.baselines.entries[absent.key].existence, Existence.ABSENT)
        with self.assertRaises(ValueError):
            BaselineEvidence(key, Existence.ABSENT, projected)

    def test_database_entries_are_scoped_ordered_and_preserve_properties(self):
        source = {'folder': {'id': 'folder', 'type': 'FOLDER', 'permissions': {'edit': True}},
                  'a': {'id': 'a', 'type': 'FORM', 'parentId': 'folder'}}
        payload = DatabaseResourcesPayload(source, ('a', 'folder'))
        state = ApplicationState()
        first = state.put(ResourceKey.database('one'), payload)
        state.put(ResourceKey.database('two'), DatabaseResourcesPayload(source))
        source['folder']['permissions']['edit'] = False
        self.assertTrue(payload.to_list()[1]['permissions']['edit'])
        self.assertEqual([item['id'] for item in payload.to_list()], ['a', 'folder'])
        display = DisplaySnapshotBuilder(1).capture(state, {}, {}, {})
        self.assertEqual(display.resources[0].state, first)
        self.assertEqual(display.affected_keys, (ResourceKey.database('one'), ResourceKey.database('two')))
        self.assertEqual(display.form_payloads(), {})
        with self.assertRaises(ValueError):
            DatabaseResourcesPayload(source, ('a', 'a'))
        with self.assertRaises(TypeError):
            ResourceState(ResourceKey.form('a'), payload, load_status=LoadStatus.LOADED)

    def test_related_resources_are_available_without_becoming_tabs(self):
        state = ApplicationState()
        state.load_form(form('a', 'A'), affected=True)
        state.load_form(form('related', 'Related'))
        state.activate(ResourceKey.form('a'))
        display = DisplaySnapshotBuilder(4).capture(state, state.forms, {}, {}, active=ResourceKey.form('a'))
        self.assertEqual(state.resource_order, (ResourceKey.form('a'), ResourceKey.form('related')))
        self.assertEqual(display.affected_keys, (ResourceKey.form('a'),))
        self.assertIn('related', display.related_schemas)
        display.form_payloads()['a']['label'] = 'mutated'
        self.assertEqual(state.forms['a']['label'], 'A')
        self.assertEqual(state.active_form_key, ResourceKey.form('a'))

    def test_offline_preview_has_local_identity_without_inventing_remote_presence(self):
        from activity_manager.multi_form import FormWorkspace
        source = {'elements': []}
        workspace = FormWorkspace(source)
        resource = workspace.state.resources[workspace.state.active_form_key]
        self.assertEqual(resource.lifecycle, Lifecycle.NEW)
        self.assertEqual(resource.payload.to_dict(), source)
        self.assertEqual(dict(workspace.state.baselines.entries), {})
        self.assertNotIn('id', source)
        self.assertEqual(workspace.active_schema, source)

    def test_deleted_lifecycle_retains_historical_payload_and_version(self):
        state = ApplicationState()
        loaded = state.load_form(form('a', 'A'), affected=True)
        before = DisplaySnapshotBuilder(1).capture(state, {}, {}, {})
        state.put(loaded.key, loaded.payload, lifecycle=Lifecycle.DELETED)
        after = DisplaySnapshotBuilder(1).capture(state, {}, {}, {})
        self.assertEqual(before.resources[0].state.lifecycle, Lifecycle.EXISTING)
        self.assertEqual(after.resources[0].state.lifecycle, Lifecycle.DELETED)
        self.assertEqual(after.resources[0].state.payload.schema['label'], 'A')
        self.assertEqual(loaded.server_version, 1)
        self.assertNotIn('a', state.forms)
        with self.assertRaises(ValueError):
            state.activate(loaded.key)
