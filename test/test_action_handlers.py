from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from activity_manager.api import ActivityInfoService
from activity_manager.action_handlers import SchemaHandler, PersistedFieldDeleteHandler, compare_resource_footprints
from activity_manager.confirmed_plan import AttemptBinding, materialize_plan
from activity_manager.endpoint_evidence import FailureKind, Observation
from activity_manager.planning import Footprint, PlanBuilder, PlanError
from activity_manager.resource_state import Existence, ResourceKey
from activity_manager.immutable import thaw
from test.test_planning import database_state, resource_effect, schema_state, schema_effect
from test.test_confirmed_plan import field


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = ActivityInfoService('https://example.test/resources', 'test-token')

    async def asyncTearDown(self):
        await self.service.aclose()

    async def observe(self, status, body):
        response = httpx.Response(status, content=body, headers={'ETag': 'receipt'},
                                  request=httpx.Request('GET', 'https://example.test'))
        with patch.object(self.service._http_client, 'request', AsyncMock(return_value=response)):
            return await self.service.observe_resource(ResourceKey.form('a'))

    async def test_reads_distinguish_missing_forbidden_unavailable_and_invalid(self):
        for status, content, kind, existence in (
                (404, b'missing', FailureKind.MISSING, Existence.ABSENT),
                (410, b'gone', FailureKind.MISSING, Existence.ABSENT),
                (401, b'auth', FailureKind.FORBIDDEN, Existence.UNKNOWN),
                (403, b'denied', FailureKind.FORBIDDEN, Existence.UNKNOWN),
                (503, b'busy', FailureKind.UNAVAILABLE, Existence.UNKNOWN),
                (200, b'{', FailureKind.INVALID, Existence.UNKNOWN),
                (200, b'{}', FailureKind.INVALID, Existence.UNKNOWN)):
            with self.subTest(status=status, content=content):
                observation = await self.observe(status, content)
                self.assertEqual(observation.existence, existence)
                self.assertEqual(observation.failure.kind, kind)
                self.assertEqual(observation.receipt.content, content.decode())
                self.assertEqual(observation.receipt.status, status)

    async def test_network_failure_and_closed_session_never_prove_absence(self):
        with patch.object(self.service._http_client, 'request', AsyncMock(side_effect=httpx.ReadTimeout('timeout'))):
            result = await self.service.observe_resource(ResourceKey.form('a'))
        self.assertEqual(result.existence, Existence.UNKNOWN)
        self.assertIsNone(result.receipt)
        await self.service.aclose()
        result = await self.service.observe_resource(ResourceKey.form('a'))
        self.assertEqual(result.failure.kind, FailureKind.UNAVAILABLE)

    async def test_schema_observation_retains_version_and_checks_identity(self):
        state = schema_state()
        data = state.payload.to_dict()
        result = await self.observe(200, json.dumps(data).encode())
        self.assertEqual(result.existence, Existence.PRESENT)
        self.assertEqual(result.receipt.version, 1)
        self.assertEqual(result.state.server_version, 1)
        data['id'] = 'wrong'
        result = await self.observe(200, json.dumps(data).encode())
        self.assertEqual(result.failure.kind, FailureKind.INVALID)

    async def test_exact_generated_schema_payload_no_resolution_or_writes_during_preparation(self):
        builder = PlanBuilder()
        builder.add(schema_effect(schema_state(), [field('pending-field:x', 'X')], 'add'))
        with patch.object(self.service._http_client, 'request', AsyncMock()) as send:
            plan = materialize_plan(builder.snapshot(), AttemptBinding('eval', {}, 'session'))
            send.assert_not_awaited()
        request = plan.actions[0].request
        response = httpx.Response(200, content=b'{"bad":"response"}', headers={'X-Receipt': 'present'})
        with patch.object(self.service._http_client, 'request', AsyncMock(return_value=response)) as send:
            result = await SchemaHandler(self.service).write(request)
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs['method'], 'post')
        self.assertEqual(kwargs['url'], '/form/a/schema')
        self.assertEqual(kwargs['json']['elements'][0]['id'], request.schema['elements'][0]['id'])
        self.assertEqual(kwargs['json'], thaw(request.schema))
        self.assertEqual(result.failure.kind, FailureKind.INVALID)
        self.assertEqual(result.receipt.content, '{"bad":"response"}')

    async def test_valid_write_receipt_and_database_version_are_retained_without_verifying(self):
        # The generated response model is still used; supply its minimal required database fields.
        database = dict(databaseId='db', userId='u', version='42', label='DB', ownerRef=None,
                        suspended=False, role={'id': 'owner'}, roles=[], storage='0', publishedTemplate=False,
                        securityCategories=[], resources=[], billingAccountId=1, billingPlan='test')
        response = httpx.Response(200, json={'database': database, 'forms': []})
        with patch.object(self.service._http_client, 'request', AsyncMock(return_value=response)):
            result = await self.service.write_schema_evidence('a', schema_state().payload.to_dict())
        self.assertIsNone(result.failure)
        self.assertEqual(result.receipt.version, '42')
        self.assertEqual(result.receipt.body['database']['version'], '42')

    def test_schema_equivalence_and_explicit_deletion_contract(self):
        builder = PlanBuilder()
        before = schema_state(fields=[field('x')])
        builder.add(schema_effect(before, [], 'delete'))
        action = builder.snapshot().actions[0]
        handler = PersistedFieldDeleteHandler(self.service)
        self.assertEqual(handler.encode_write(action).schema['elements'], ())
        changed_version = replace(action.after, server_version='new')
        observation = Observation(action.key, Existence.PRESENT, changed_version)
        self.assertTrue(handler.compare(action.after, observation).matches)
        with self.assertRaises(PlanError):
            handler.encode_write(replace(action, deleted_member_id='other'))

    def test_resource_comparison_uses_owned_properties_and_strict_presence_types(self):
        before = database_state()
        footprint = Footprint(before.key, 'a', ('parentId',))
        changed_other = resource_effect(before, {('b', 'label'): 'Other'}, 'other').after
        observation = Observation(before.key, Existence.PRESENT, changed_other)
        self.assertTrue(compare_resource_footprints(before, observation, (footprint,)).matches)
        for wanted, found in ((None, False), (False, 0), (None, 'absent')):
            left = resource_effect(before, {('a', 'parentId'): wanted}, 'left').after
            right = resource_effect(before, {('a', 'parentId'): found}, 'right').after
            self.assertFalse(compare_resource_footprints(
                left, Observation(before.key, Existence.PRESENT, right), (footprint,)).matches)

    def test_rename_without_resource_verification_projection_is_schema_only(self):
        builder = PlanBuilder()
        effect = schema_effect(schema_state(), [], 'rename')
        data = effect.after.payload.to_dict()
        data['label'] = 'Renamed'
        effect = replace(effect, after=replace(effect.after, payload=type(effect.after.payload)(data)))
        builder.add(effect)
        plan = materialize_plan(builder.snapshot(), AttemptBinding('eval', {}, 'session'))
        self.assertEqual(plan.actions[0].request.schema['label'], 'Renamed')
        self.assertEqual(plan.actions[0].action.projections, ())

    def test_missing_resource_property_differs_from_null_and_existence_is_independent(self):
        before = database_state()
        changed = resource_effect(before, {('a', 'optional'): None}, 'null').after
        observed = Observation(before.key, Existence.PRESENT, changed)
        self.assertFalse(compare_resource_footprints(
            before, observed, (Footprint(before.key, 'a', ('optional',)),)).matches)
        self.assertTrue(compare_resource_footprints(
            before, observed, (Footprint(before.key, 'a', existence=True),)).matches)
