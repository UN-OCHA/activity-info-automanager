"""Creation, local context, batching and recovery through the production pipeline."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from nicegui import ui
from nicegui.client import Client

from activity_manager.applier import Applier
from activity_manager.api import ActivityInfoService
from activity_manager.command_creator import CommandCreatorRenderer
from activity_manager.command_parser import parse_command_text, CommandTextError
from activity_manager.commands.add_form import ADD_FORM, AddFormCommand
from activity_manager.confirmed_plan import AttemptBinding, ConfirmationAttempt
from activity_manager.endpoint_evidence import Observation, EndpointFailure, FailureKind, WriteEvidence
from activity_manager.executor import Executor
from activity_manager.immutable import thaw
from activity_manager.planning import ActionKind
from activity_manager.resource_state import ResourceKey, ResourceState, FormPayload, DatabaseResourcesPayload, LoadStatus, Existence
from activity_manager.schema_resolution import resolve_command_schema
from test.test_move_form import FolderService
from test.test_multi_form import form


class CreationService(FolderService):
    def __init__(self):
        super().__init__([form('a', 'A'), form('b', 'B')])
        self.calls = []
        self.creation_error = False
        self.creation_rejected = False
        self.wrong_parent = False
        self.unknown = False
        self.concurrent = False
        self.reject_schema = False

    async def observe_resource(self, key):
        if key.kind.value == 'database_resources':
            payload = DatabaseResourcesPayload(deepcopy(self.resources[key.identity]))
        elif self.unknown:
            return Observation(key, Existence.UNKNOWN, failure=EndpointFailure(FailureKind.UNAVAILABLE, 'offline'))
        elif key.identity not in self.schemas:
            return Observation(key, Existence.ABSENT, failure=EndpointFailure(FailureKind.MISSING, 'missing'))
        else:
            payload = FormPayload(deepcopy(self.schemas[key.identity]))
        return Observation(key, Existence.PRESENT, ResourceState(key, payload, load_status=LoadStatus.LOADED))

    async def write_add_form_evidence(self, database, payload):
        self.calls.append(('create', deepcopy(payload)))
        assert payload['formResource']['visibility'] == 'PRIVATE'
        if self.creation_rejected:
            return WriteEvidence(failure=EndpointFailure(FailureKind.REJECTED, 'rejected'))
        schema, entry = deepcopy(payload['formClass']), deepcopy(payload['formResource'])
        self.schemas[schema['id']] = schema
        if self.wrong_parent:
            entry['parentId'] = 'south'
        self.resources[database][entry['id']] = entry
        return WriteEvidence(failure=EndpointFailure(FailureKind.UNAVAILABLE, 'response lost') if self.creation_error else None)

    async def write_schema_evidence(self, identity, schema):
        self.calls.append(('schema', identity))
        if self.reject_schema:
            if self.concurrent:
                created = next(key for key in self.schemas if key.startswith('c'))
                self.schemas[created]['label'] = 'Concurrent'
            return WriteEvidence(failure=EndpointFailure(FailureKind.REJECTED, 'rejected'))
        await self.put_form_schema(identity, schema)
        return WriteEvidence()

    async def write_resources_evidence(self, database, payload):
        self.calls.append(('resources', deepcopy(payload)))
        for identity in payload.get('resourceDeletions', []):
            self.schemas.pop(identity, None)
            self.resources[database].pop(identity, None)
        for entry in payload.get('resourceUpdates', []):
            self.resources[database][entry['id']].update(entry)
        return WriteEvidence()


class AddFormTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, text, *, initial=True, database='db'):
        service = CreationService()
        applier = None
        async def resolve(schema):
            return await resolve_command_schema(service, schema, applier.related_schemas, {}, {}, {},
                source_schema=applier.workspace.baseline_forms.get(schema['id']))
        applier = Applier(form('a', 'A') if initial else None, parse_command_text(text),
                          initial_database_id=database, form_service=service, resolve_schema=resolve)
        await applier.evaluate()
        self.assertTrue(all(e.status != 'error' for e in applier.executions), [e.message for e in applier.executions])
        return applier, service

    def attempt(self, applier):
        candidate = applier.candidate_plan
        binding = AttemptBinding('evaluation', {}, 'session')
        attempt = ConfirmationAttempt(candidate, binding, related_schemas=applier.related_schemas)
        attempt.confirm(attempt.plan.attempt_id, binding, candidate)
        return attempt, binding, candidate

    async def execute(self, applier, service, choice='rollback_all'):
        attempt, binding, candidate = self.attempt(applier)
        async def choose(_):
            return choice
        executor = Executor(service)
        return await executor.execute(attempt, binding, candidate, choose)

    def test_parser_and_creator(self):
        for text in ('AddForm(name="New")', 'addform(NAME="${vars.name}", FOLDER="/")'):
            command = parse_command_text(text)[0]
            self.assertEqual(parse_command_text(command.to_syntax()), [command])
        for args in ('', 'name=" "', 'name="A", form="B"', 'name="A", formPattern=".*"', 'name="A", currentForm=True'):
            with self.subTest(args=args), self.assertRaises(CommandTextError):
                parse_command_text('AddForm(' + args + ')')
        added = []
        with Client(ui.page('/')), ui.column():
            creator = CommandCreatorRenderer((ADD_FORM,), on_add=added.append)
            self.assertFalse(creator.target_mode.visible)
            creator.controls['form_name'].set_value('New')
            creator._add_command()
        self.assertEqual(added, [AddFormCommand('New')])

    async def test_creation_snapshot_and_folded_schema_rename_move(self):
        app, service = await self.apply('''SetVariable(name="title", method="literal", value="New")
AddForm(name="${vars.title}")
AddField(fieldCode="NAME", label="Name")
ModifyField(fieldCode="NAME", fieldType="FREE_TEXT", fieldVariant="text", label="Updated")
ModifyForm(currentForm=True, name="Renamed", recordLabelFieldId="NAME")
MoveForm(currentForm=True, folder="North/Archive")
''')
        first = app.executions[1]
        pending = first.schema['id']
        self.assertTrue(pending.startswith('pending-form:'))
        self.assertEqual(first.schema['elements'], [])
        self.assertEqual(first.display_state.form_payloads()[pending]['elements'], [])
        self.assertIn(pending, first.display_state.tab_payloads()['database:db'][-1]['id'])
        self.assertEqual(len(app.candidate_plan.actions), 1)
        action = app.candidate_plan.actions[0]
        self.assertEqual(action.kind, ActionKind.FORM_CREATE)
        self.assertEqual(len(action.application_ids), 5)
        attempt, _, _ = self.attempt(app)
        request = thaw(attempt.plan.actions[0].request.schema)
        self.assertEqual(request['formResource']['parentId'], 'archive')
        self.assertEqual(request['formResource']['label'], 'Renamed')
        self.assertEqual(request['formClass']['recordLabelFieldId'], request['formClass']['elements'][0]['id'])
        self.assertNotIn('pending-', str(request))
        self.assertEqual(app.executions[1].schema['id'], pending)
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'success', report.messages)
        self.assertEqual([call[0] for call in service.calls], ['create'])

    async def test_switch_seals_creation_and_references_materialize(self):
        app, service = await self.apply('''AddForm(name="New")
AddField(fieldCode="NAME", label="Name")
AddForm(name="Other")
AddField(fieldCode="REF", fieldType="reference", label="Reference", referencedForm="New")
WithForm(form="New")
AddField(fieldCode="EXTRA", label="Extra")
''')
        attempt, _, _ = self.attempt(app)
        actions = attempt.plan.actions
        self.assertEqual([item.action.kind for item in actions], [ActionKind.FORM_CREATE, ActionKind.FORM_CREATE, ActionKind.FORM_SCHEMA_UPDATE])
        first_id = actions[0].action.key.identity
        self.assertEqual(actions[1].request.schema['formClass']['elements'][0]['typeParameters']['range'][0]['formId'], first_id)
        self.assertEqual(actions[2].action.key.identity, first_id)
        self.assertIn(actions[0].action.action_id, actions[1].action.dependency_ids)
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'success', report.messages)

    async def test_creation_without_initial_form_and_missing_database(self):
        app, service = await self.apply('AddForm(name="Empty")', initial=False)
        self.assertEqual(app.executions[0].status, 'applied')
        self.assertEqual((await self.execute(app, service)).status, 'success')
        app, _ = await self.apply('AddForm(name="Empty")', initial=False, database=None)
        self.assertEqual(app.executions[0].status, 'warning')
        self.assertFalse(app.candidate_plan.actions)

    async def test_creation_ends_prior_iteration_even_when_it_skips(self):
        app, _ = await self.apply('''WithForm(formPattern="A|B")
AddForm(name="New")
AddField(fieldCode="NEW", label="New")''')
        self.assertIsNone(app.scope)
        self.assertFalse(app.workspace.state.active_scope)
        self.assertEqual(len(app.workspace.latest['a']['elements']), 0)
        self.assertEqual(len(app.workspace.latest['b']['elements']), 0)
        self.assertEqual(len(app.executions[-1].schema['elements']), 1)
        app, _ = await self.apply('''WithForm(formPattern="A|B")
AddForm(name="New", folder="missing")
AddField(fieldCode="NEW", label="New")''')
        self.assertEqual(app.executions[1].status, 'warning')
        self.assertIsNone(app.scope)
        self.assertIsNone(app.workspace.active_schema)
        self.assertEqual(app.executions[2].diagnostics[0].code, 'missing_form')
        self.assertEqual(len(app.workspace.latest['a']['elements']), 0)
        self.assertEqual(len(app.workspace.latest['b']['elements']), 0)
        self.assertEqual(len(app.workspace.latest), 2)

    async def test_duplicate_name_allowed_but_selector_ambiguous(self):
        app, _ = await self.apply('AddForm(name="A")\nWithForm(form="A")')
        self.assertEqual(app.executions[0].status, 'applied')
        self.assertEqual(app.executions[1].status, 'warning')

    async def test_lost_response_rolls_back_with_single_deletion(self):
        app, service = await self.apply('AddForm(name="New")\nAddField(fieldCode="N", label="Name")')
        service.creation_error = True
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        identity = report.plan.actions[0].action.key.identity
        self.assertEqual(service.calls[-1], ('resources', {'resourceDeletions': [identity]}))
        self.assertNotIn(identity, service.schemas)
        self.assertEqual(report.journal[0].recovery[-1].status, 'verified_restored')

    async def test_rejected_creation_is_unchanged_and_not_deleted(self):
        app, service = await self.apply('AddForm(name="New")')
        service.creation_rejected = True
        report = await self.execute(app, service)
        self.assertEqual(report.journal[0].outcome, 'unchanged')
        self.assertTrue(any('Add form "New"' in message and 'API error: rejected' in message for message in report.messages))
        self.assertFalse(any(report.journal[0].reviewed.action.action_id in message for message in report.messages))
        self.assertEqual(len(service.calls), 1)

    async def test_reported_sequence_sends_one_complete_create_request(self):
        app, service = await self.apply('''AddForm(name="New")
MoveForm(currentForm=True, folder="North")
AddField(fieldCode="ONE", label="One")
AddField(fieldCode="TWO", label="Two")
MoveField(fieldCode="TWO", position="Beginning")''')
        attempt, binding, candidate = self.attempt(app)
        self.assertEqual(len(attempt.plan.actions), 1)
        api = ActivityInfoService('https://example.test/resources', 'test-token')
        database = dict(databaseId='db', userId='u', version='1', label='DB', ownerRef=None,
                        suspended=False, role={'id': 'owner'}, roles=[], storage='0', publishedTemplate=False,
                        securityCategories=[], resources=[], billingAccountId=1, billingPlan='test')
        writes = []
        async def server(**kwargs):
            method, url = kwargs['method'], kwargs['url']
            request = httpx.Request(method, 'https://example.test' + url)
            if method == 'post':
                body = kwargs['json']
                writes.append(body)
                resource, schema = body['formResource'], body['formClass']
                required = {'id', 'type', 'parentId', 'label', 'visibility'}
                if required - resource.keys():
                    return httpx.Response(400, json={'message': 'Missing required resource property'}, request=request)
                self.assertEqual(resource['visibility'], 'PRIVATE')
                self.assertEqual(resource['parentId'], 'north')
                self.assertEqual(resource['id'], schema['id'])
                self.assertEqual([f['code'] for f in schema['elements']], ['TWO', 'ONE'])
                self.assertNotIn('pending-', str(body))
                service.schemas[schema['id']] = deepcopy(schema)
                service.resources['db'][resource['id']] = deepcopy(resource)
                database['resources'] = list(service.resources['db'].values())
                return httpx.Response(200, json={'database': database, 'forms': []}, request=request)
            if url == '/databases/db':
                database['resources'] = list(service.resources['db'].values())
                return httpx.Response(200, json=database, request=request)
            identity = url.split('/')[2]
            if identity not in service.schemas:
                return httpx.Response(404, json={'message': 'Not found'}, request=request)
            return httpx.Response(200, json=service.schemas[identity], request=request)
        try:
            with patch.object(api._http_client, 'request', AsyncMock(side_effect=server)):
                report = await Executor(api).execute(attempt, binding, candidate, AsyncMock(return_value='leave'))
            self.assertEqual(report.status, 'success', report.messages)
            self.assertEqual(len(writes), 1)
        finally:
            await api.aclose()

    async def test_unknown_creation_reconciles_before_recovery_and_failed_deletion_stops(self):
        app, service = await self.apply('AddForm(name="New")')
        original_read = service.observe_resource
        original_write = service.write_add_form_evidence
        unknown_once = False
        async def create(database, payload):
            nonlocal unknown_once
            result = await original_write(database, payload)
            unknown_once = True
            return result
        async def read(key):
            nonlocal unknown_once
            if unknown_once and key.kind.value == 'form_schema':
                unknown_once = False
                return Observation(key, Existence.UNKNOWN, failure=EndpointFailure(FailureKind.UNAVAILABLE, 'offline'))
            return await original_read(key)
        service.write_add_form_evidence, service.observe_resource = create, read
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        self.assertEqual(report.journal[0].outcome, 'unknown')
        self.assertEqual(report.journal[0].recovery[0].status, 'reconciled')
        app, service = await self.apply('AddForm(name="New")')
        service.creation_error = True
        service.write_resources_evidence = AsyncMock(return_value=WriteEvidence())
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'recovery_failed', report.messages)
        self.assertEqual(report.journal[0].recovery[-1].status, 'failed')

    async def test_sealed_creation_move_is_separate_and_recovery_reverses_dependencies(self):
        app, service = await self.apply('AddForm(name="New")\nWithForm(form="A")\nWithForm(form="New")\nMoveForm(currentForm=True, folder="North")')
        self.assertEqual([a.kind for a in app.candidate_plan.actions], [ActionKind.FORM_CREATE, ActionKind.DATABASE_RESOURCE_PATCH])
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'success', report.messages)
        self.assertTrue(report.plan.actions[1].request.schema['resourceUpdates'][0]['id'].startswith('c'))
        app, service = await self.apply('AddForm(name="New")\nAddForm(name="Other")\nAddField(fieldCode="REF", fieldType="reference", referencedForm="New", label="Reference")')
        original = service.write_add_form_evidence
        async def lose_second_response(database, payload):
            result = await original(database, payload)
            if payload['formClass']['label'] == 'Other':
                return WriteEvidence(failure=EndpointFailure(FailureKind.UNAVAILABLE, 'lost'))
            return result
        service.write_add_form_evidence = lose_second_response
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'rolled_back_all', report.messages)
        ids = [item.action.key.identity for item in report.plan.actions]
        self.assertEqual([call[1]['resourceDeletions'][0] for call in service.calls if call[0] == 'resources'], ids[::-1])

    async def test_field_deletion_and_self_reference_stay_in_creation(self):
        app, service = await self.apply('AddForm(name="New")\nAddField(fieldCode="N", label="Name")\nDeleteField(fieldCode="N")\nAddField(fieldCode="SELF", fieldType="reference", referencedForm="New", label="Self")')
        self.assertEqual(len(app.candidate_plan.actions), 1)
        self.assertEqual(app.candidate_plan.actions[0].kind, ActionKind.FORM_CREATE)
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'success', report.messages)
        schema = service.calls[0][1]['formClass']
        self.assertEqual(schema['elements'][0]['typeParameters']['range'][0]['formId'], schema['id'])

    async def test_folder_type_change_blocks_preflight_and_unavailable_resources_skip(self):
        app, service = await self.apply('AddForm(name="New", folder="North")')
        service.resources['db']['north']['type'] = 'FORM'
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'preflight_failed', report.messages)
        self.assertEqual(service.calls, [])
        service = CreationService()
        service.get_database_resources = AsyncMock(side_effect=ValueError('offline'))
        app = Applier(form('a', 'A'), parse_command_text('AddForm(name="New")'),
                      form_service=service, resolve_schema=AsyncMock())
        await app.evaluate()
        self.assertEqual(app.executions[0].status, 'warning')
        self.assertIsNone(app.workspace.active_schema)
        self.assertFalse(app.candidate_plan.actions)
        self.assertFalse(any(key.startswith('pending-form:') for key in app.workspace.latest))

    async def test_unknown_and_collision_block_preflight(self):
        for unknown in (True, False):
            app, service = await self.apply('AddForm(name="New")')
            attempt, binding, candidate = self.attempt(app)
            identity = attempt.plan.actions[0].action.key.identity
            service.unknown = unknown
            if not unknown:
                service.schemas[identity] = form(identity, 'Existing')
            report = await Executor(service).execute(attempt, binding, candidate, AsyncMock(return_value='leave'))
            self.assertEqual(report.status, 'preflight_failed')
            self.assertEqual(service.calls, [])

    async def test_wrong_folder_and_concurrent_schema_block_recovery(self):
        app, service = await self.apply('AddForm(name="New", folder="North")')
        service.wrong_parent = True
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'recovery_blocked', report.messages)
        self.assertEqual(report.journal[0].outcome, 'divergent')
        app, service = await self.apply('AddForm(name="New")\nWithForm(form="A")\nAddField(fieldCode="N", label="Name")')
        service.reject_schema = service.concurrent = True
        report = await self.execute(app, service)
        self.assertEqual(report.status, 'recovery_blocked', report.messages)
        self.assertFalse(any(call[0] == 'resources' for call in service.calls))


class AddFormEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_required_visibility_is_rejected_locally_and_http_details_survive(self):
        service = ActivityInfoService('https://example.test/resources', 'test-token')
        payload = {'formResource': dict(id='new', label='New', type='FORM', parentId='db'),
                   'formClass': form('new', 'New')}
        try:
            with patch.object(service._http_client, 'request', AsyncMock()) as request:
                evidence = await service.write_add_form_evidence('db', payload)
                request.assert_not_awaited()
                self.assertEqual(evidence.failure.kind, FailureKind.INVALID)
                self.assertIn('visibility', evidence.failure.message)
            payload['formResource']['visibility'] = 'PRIVATE'
            response = httpx.Response(400, json={'message': 'Invalid field definition'})
            with patch.object(service._http_client, 'request', AsyncMock(return_value=response)):
                evidence = await service.write_add_form_evidence('db', payload)
            self.assertIn('HTTP 400: Invalid field definition', evidence.failure.message)
            self.assertIn('Invalid field definition', evidence.receipt.content)
        finally:
            await service.aclose()

    async def test_generated_create_and_delete_request_encoding(self):
        service = ActivityInfoService('https://example.test/resources', 'test-token')
        try:
            payload = {'formResource': dict(id='new', label='New', type='FORM', parentId='db', visibility='PRIVATE'),
                       'formClass': form('new', 'New')}
            database = dict(databaseId='db', userId='u', version='1', label='DB', ownerRef=None,
                            suspended=False, role={'id': 'owner'}, roles=[], storage='0', publishedTemplate=False,
                            securityCategories=[], resources=[], billingAccountId=1, billingPlan='test')
            response = httpx.Response(200, json={'database': database, 'forms': []}, request=httpx.Request('POST', 'https://example.test'))
            with patch.object(service._http_client, 'request', AsyncMock(return_value=response)) as request:
                evidence = await service.write_add_form_evidence('db', payload)
                self.assertIsNone(evidence.failure)
                self.assertEqual(request.call_args.kwargs['method'], 'post')
                self.assertEqual(request.call_args.kwargs['url'], '/databases/db/forms')
                self.assertEqual(request.call_args.kwargs['json'], payload)
            response = httpx.Response(200, json=database, request=httpx.Request('POST', 'https://example.test'))
            with patch.object(service._http_client, 'request', AsyncMock(return_value=response)) as request:
                evidence = await service.write_resources_evidence('db', {'resourceDeletions': ['new']})
                self.assertIsNone(evidence.failure)
                self.assertEqual(request.call_args.kwargs['json'], {'resourceDeletions': ['new']})
        finally:
            await service.aclose()
