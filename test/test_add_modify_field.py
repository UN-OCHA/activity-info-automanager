from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from nicegui import ui
from nicegui.client import Client

from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_execution import apply_command_locally
from activity_manager.command_list import CommandListManager
from activity_manager.command_parser import CommandTextError, parse_command_text
from activity_manager.commands import ADD_FIELD, ADD_MODIFY_FIELD
from activity_manager.schema_resolution import resolve_command_schema


def form():
    return {'id': 'survey', 'databaseId': 'db', 'label': 'Survey', 'elements': [
        {'id': 'targetid', 'code': 'target', 'type': 'FREE_TEXT', 'label': 'Target', 'required': True},
        {'id': 'otherid', 'code': 'other', 'type': 'FREE_TEXT', 'label': 'Other'},
    ]}


def command(arguments):
    return parse_command_text(f'AddModifyField({arguments})')[0]


class AddModifyFieldTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, arguments, source=None, service=None, related=None):
        related = {} if related is None else related
        service = service or SimpleNamespace()

        async def resolve(candidate):
            return await resolve_command_schema(service, candidate, related, {}, {}, {})

        with Client(ui.page('/')), ui.column():
            manager = CommandListManager(resolve_schema=resolve)
        manager.set_schema(form() if source is None else source)
        manager.advanced_draft = '\n'.join(f'AddModifyField({line})' for line in arguments)
        manager._apply_text()
        await manager._evaluation_task
        return manager

    async def test_failed_edit_does_not_discard_move(self):
        manager = await self.evaluate(['fieldCode="target", label="", position="End"'])
        result = manager.executions[0]
        self.assertEqual(result.status, 'warning', result.message)
        self.assertIn('ModifyField', result.message)
        self.assertEqual([field['id'] for field in result.schema['elements']], ['otherid', 'targetid'])
        self.assertEqual(result.schema['elements'][1]['label'], 'Target')
        self.assertEqual(len(manager.candidate_plan.actions), 1)

    async def test_failed_move_does_not_discard_edit(self):
        manager = await self.evaluate([
            'fieldCode="target", description="Changed", position="After field", beforeAfterField="absent"',
        ])
        result = manager.executions[0]
        self.assertEqual(result.status, 'warning', result.message)
        self.assertIn('MoveField', result.message)
        self.assertEqual([field['id'] for field in result.schema['elements']], ['targetid', 'otherid'])
        self.assertEqual(result.schema['elements'][0]['description'], 'Changed')
        self.assertTrue(result.schema['elements'][0]['required'])
        self.assertEqual(len(manager.candidate_plan.actions), 1)

    async def test_failed_edit_resolution_does_not_discard_move(self):
        manager = await self.evaluate([
            'fieldCode="target", translation=True, translationField="missing", '
            'translationLanguage="fr", position="End"',
        ])
        result = manager.executions[0]
        self.assertEqual(result.status, 'warning', result.message)
        self.assertIn('missing', result.message)
        self.assertEqual([field['id'] for field in result.schema['elements']], ['otherid', 'targetid'])
        self.assertNotIn('typeParameters', result.schema['elements'][1])
        self.assertEqual(len(manager.candidate_plan.actions), 1)

    async def test_two_failed_steps_and_mismatch_produce_no_actions(self):
        manager = await self.evaluate([
            'fieldCode="target", label="", position="After field", beforeAfterField="target"',
            'fieldCode="target", fieldType="quantity", label="Wrong", position="End"',
        ])
        first, second = manager.executions
        self.assertEqual([item.severity for item in first.diagnostics], ['warning', 'warning'])
        self.assertEqual(second.diagnostics[0].code, 'target_type_mismatch')
        self.assertEqual(second.schema, form())
        self.assertFalse(manager.candidate_plan.actions)

    async def test_add_and_update_use_current_schema_and_keep_pending_id(self):
        arguments = ('fieldCode="new", label="New", description=Append("End", ", ", "Text"), '
                     'validationCondition=AppendFormula("AND", "((TRUE))"), position="Beginning"')
        manager = await self.evaluate([arguments, arguments,
                                       'fieldCode="new", label="Renamed", position="End"'])
        added, repeated, modified = manager.executions
        self.assertEqual([item.status for item in manager.executions], ['applied'] * 3)
        self.assertEqual(added.schema, repeated.schema)
        self.assertEqual(added.schema['elements'][0]['id'], modified.schema['elements'][-1]['id'])
        self.assertEqual(modified.schema['elements'][-1]['label'], 'Renamed')
        self.assertEqual(len(manager.candidate_plan.actions), 1)

    async def test_reference_name_resolves_before_comparing_identity(self):
        source = form()
        source['elements'][0].update(type='reference', typeParameters={'range': [{'formId': 'people'}]})
        related = {identity: {'id': identity, 'label': label, 'databaseId': 'db', 'elements': []}
                   for identity, label in [('people', 'People'), ('places', 'Places')]}
        service = SimpleNamespace(get_form_names=AsyncMock(return_value={'people': 'People', 'places': 'Places'}))
        manager = await self.evaluate([
            'fieldCode="target", fieldType="reference", referencedForm="People", label="Person", position="End"',
            'fieldCode="target", fieldType="reference", referencedForm="Places", label="Place", position="Beginning"',
        ], source, service, related)
        self.assertEqual([result.status for result in manager.executions], ['applied', 'warning'])
        self.assertEqual(manager.target_schema()['elements'][-1]['label'], 'Person')

    def test_local_dispatch_and_text_validation(self):
        parsed = command('fieldCode="target", description=Append("End", ", ", "Text")')
        self.assertEqual(parse_command_text(parsed.to_syntax()), [parsed])
        result = apply_command_locally(parsed, form())
        self.assertEqual(result.schema['elements'][0]['description'], 'Text')
        for helper in ('Replace', 'RegexReplace'):
            with self.subTest(helper=helper), self.assertRaises(CommandTextError):
                command(f'fieldCode="target", description={helper}("old", "new")')

    def test_label_collision_uses_add_path(self):
        parsed = command('fieldCode="Target", label="New"')
        result = apply_command_locally(parsed, form())
        self.assertEqual(result.status, 'applied', result.message)
        self.assertEqual(result.schema['elements'][0]['label'], 'Target')
        self.assertEqual(result.schema['elements'][-1]['code'], 'Target')
        self.assertEqual(result.schema['elements'][-1]['label'], 'New')

    def test_creator_shares_add_field_rules(self):
        self.assertEqual(
            tuple(replace(parameter, parse_text=None) for parameter in ADD_MODIFY_FIELD.parameters),
            ADD_FIELD.parameters,
        )

    def test_creator_authors_command_that_can_add_or_modify(self):
        authored = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((ADD_MODIFY_FIELD,), on_add=authored.append)
        creator.controls['field_code'].set_value('target')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        creator.controls['label'].set_value('Updated')
        creator.activations['description'].set_value(True)
        creator.controls['description'].set_value('Append("End", ", ", "Text")')
        creator._parameter_changed(None)
        creator._add_command()
        self.assertEqual(len(authored), 1, creator.error.text)
        self.assertEqual(parse_command_text(authored[0].to_syntax()), authored)
        self.assertIsNone(authored[0].position)
        for existing in (True, False):
            with self.subTest(existing=existing):
                source = form()
                if not existing:
                    source['elements'].pop(0)
                result = apply_command_locally(authored[0], source)
                self.assertEqual(result.status, 'applied', result.message)
                expected = ['target', 'other'] if existing else ['other', 'target']
                self.assertEqual([field['code'] for field in result.schema['elements']], expected)
                field = result.schema['elements'][expected.index('target')]
                self.assertEqual(field['code'], 'target')
                self.assertEqual(field['label'], 'Updated')
                self.assertEqual(field['description'], 'Text')

    def test_creator_requires_reference_form_for_ordinary_reference(self):
        authored = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((ADD_MODIFY_FIELD,), on_add=authored.append)
        creator.controls['field_type'].set_value('reference')
        creator._parameter_changed(None)
        creator.controls['field_variant'].set_value('reference')
        creator.controls['field_code'].set_value('person')
        creator.controls['label'].set_value('Person')
        creator._parameter_changed(None)
        self.assertTrue(creator.add_button.props['disable'])
        creator.controls['referenced_form'].set_value('people')
        creator._parameter_changed(None)
        creator._add_command()
        self.assertEqual(len(authored), 1, creator.error.text)
        self.assertEqual(authored[0].referenced_form, 'people')

    def test_creator_serial_flags_allow_both_paths(self):
        authored = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((ADD_MODIFY_FIELD,), on_add=authored.append)
        creator.controls['field_type'].set_value('serial')
        creator.controls['field_code'].set_value('serial_number')
        creator.controls['label'].set_value('Number')
        creator._parameter_changed(None)
        self.assertTrue(creator.values()['key'])
        self.assertTrue(creator.values()['unique'])
        creator._add_command()
        self.assertEqual(len(authored), 1, creator.error.text)
        added = apply_command_locally(authored[0], form())
        modified = apply_command_locally(replace(authored[0], label='Updated'), added.schema)
        self.assertEqual(added.status, 'applied', added.message)
        self.assertEqual(modified.status, 'applied', modified.message)
        field = modified.schema['elements'][-1]
        self.assertEqual(field['label'], 'Updated')
        self.assertTrue(field['key'])
        self.assertTrue(field['unique'])

    def test_selection_update_preserves_option_ids_and_presentation(self):
        source = form()
        source['elements'][0].update(type='enumerated', typeParameters={
            'cardinality': 'multiple', 'presentation': 'dropdown',
            'values': [{'id': 'existing', 'label': 'Existing'}],
        })
        parsed = command('fieldCode="target", fieldType="enumerated", fieldVariant="multiple", '
                         'optionList={"values": [{"label": "Existing"}, {"label": "New"}]}')
        result = apply_command_locally(parsed, source)
        self.assertEqual(result.status, 'applied', result.message)
        options = result.schema['elements'][0]['typeParameters']
        self.assertEqual(options['presentation'], 'dropdown')
        self.assertEqual(options['values'][0]['id'], 'existing')
        self.assertEqual(options['values'][1]['label'], 'New')
