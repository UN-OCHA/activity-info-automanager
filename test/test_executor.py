from copy import deepcopy
import asyncio
from dataclasses import replace, FrozenInstanceError
import unittest
from unittest.mock import AsyncMock

from activity_manager.confirmed_plan import AttemptBinding, ConfirmationAttempt
from activity_manager.endpoint_evidence import EndpointFailure, EndpointReceipt, FailureKind, Observation, WriteEvidence
from activity_manager.executor import Executor, Outcome
from activity_manager.planning import PlanBuilder, PlanError
from activity_manager.resource_state import BaselineEvidence, Existence, FormPayload, LoadStatus, ResourceKey, ResourceState
from test.test_planning import schema_state, schema_effect
from test.test_confirmed_plan import field


def setup_plan(sequence=('a', 'b'), *, deletion=False):
    initial = {identity: schema_state(identity, fields=[field('original')]) for identity in set(sequence)}
    current = dict(initial)
    builder = PlanBuilder()
    builder.observe([BaselineEvidence(state.key, Existence.PRESENT, state) for state in initial.values()])
    for index, identity in enumerate(sequence):
        before = current[identity]
        fields = [] if deletion else [*before.payload.to_dict()['elements'], field(f'new{index}')]
        effect = schema_effect(before, fields, f'edit{index}')
        builder.add(effect)
        current[identity] = effect.after
    candidate = builder.snapshot()
    binding = AttemptBinding('evaluation', {'source': 'a'}, 'session')
    attempt = ConfirmationAttempt(candidate, binding)
    attempt.confirm(attempt.plan.attempt_id, binding, candidate)
    return initial, candidate, binding, attempt


class MemoryEvidenceService:
    def __init__(self, initial, modes=()):
        self.schemas = {identity: state.payload.to_dict() for identity, state in initial.items()}
        self.writes = []
        self.reads = []
        self.modes = list(modes)
        self.fail_reads = set()
        self.before_read = None
        self.before_write = None

    async def observe_resource(self, key):
        self.reads.append(key.identity)
        count = len(self.reads)
        if self.before_read:
            self.before_read(count, key)
        if count in self.fail_reads:
            return Observation(key, Existence.UNKNOWN, failure=EndpointFailure(FailureKind.UNAVAILABLE, 'offline'))
        schema = self.schemas[key.identity]
        state = ResourceState(key, FormPayload(schema), load_status=LoadStatus.LOADED,
                              server_version=schema.get('schemaVersion'))
        return Observation(key, Existence.PRESENT, state)

    async def write_schema_evidence(self, identity, schema):
        if self.before_write:
            self.before_write(identity, schema)
        self.writes.append((identity, deepcopy(schema)))
        mode = self.modes.pop(0) if self.modes else 'commit'
        if mode.startswith('commit'):
            self.schemas[identity] = deepcopy(schema)
        elif mode.startswith('divergent'):
            self.schemas[identity] = dict(deepcopy(schema), label='Unowned change')
        receipt = EndpointReceipt(200, {'ETag': 'receipt'}, '{}', {'version': '2'}, '2')
        if mode.endswith('error'):
            return WriteEvidence(receipt, EndpointFailure(FailureKind.UNAVAILABLE, 'lost response', receipt))
        return WriteEvidence(receipt)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def run_plan(self, *, sequence=('a', 'b'), modes=(), choice='leave', configure=None, deletion=False):
        initial, candidate, binding, attempt = setup_plan(sequence, deletion=deletion)
        service = MemoryEvidenceService(initial, modes)
        executor = Executor(service)
        if configure:
            configure(service, executor)
        choose = AsyncMock(return_value=choice)
        report = await executor.execute(attempt, binding, candidate, choose)
        return report, service, executor, choose

    async def test_all_baselines_read_before_first_write_and_attempt_published_before_call(self):
        def configure(service, executor):
            def check(identity, schema):
                self.assertEqual(executor.journal[-1].status, 'attempted')
                self.assertEqual(set(service.reads[:2]), {'a', 'b'})
            service.before_write = check
        report, service, executor, choose = await self.run_plan(configure=configure)
        self.assertEqual(report.status, 'success')
        self.assertEqual([entry.outcome for entry in report.journal], [Outcome.INTENDED] * 2)
        self.assertEqual(report.journal[0].evidence.receipt.version, '2')
        choose.assert_not_awaited()

    async def test_later_stale_baseline_prevents_every_write(self):
        def configure(service, executor):
            service.schemas['b']['label'] = 'Concurrent'
        report, service, _, choose = await self.run_plan(configure=configure)
        self.assertEqual(report.status, 'preflight_failed')
        self.assertEqual(service.writes, [])
        self.assertEqual(len(report.preflight), 2)
        choose.assert_not_awaited()

    async def test_unavailable_preflight_never_substitutes_intended_state(self):
        report, service, _, _ = await self.run_plan(configure=lambda service, _: service.fail_reads.add(2))
        self.assertEqual(report.status, 'preflight_failed')
        self.assertNotIn('b', report.actual_schemas)
        self.assertEqual(service.writes, [])

    async def test_immediate_predecessor_staleness_stops_before_write(self):
        def configure(service, executor):
            def mutate(count, key):
                if count == 3:
                    service.schemas[key.identity]['label'] = 'Concurrent'
            service.before_read = mutate
        report, service, _, _ = await self.run_plan(configure=configure)
        self.assertEqual(report.status, 'stopped')
        self.assertEqual(service.writes, [])

    async def test_revisits_use_prior_verified_state_and_reverse_immediate_preimages(self):
        report, service, _, _ = await self.run_plan(sequence=('a', 'b', 'a'),
            modes=('commit', 'commit', 'commit_error'), choice='rollback_all')
        self.assertEqual(report.status, 'rolled_back_all')
        self.assertEqual([identity for identity, _ in service.writes], ['a', 'b', 'a', 'a', 'b', 'a'])
        self.assertEqual([f['id'] for f in service.writes[3][1]['elements']], ['original', 'new0'])
        self.assertEqual([f['id'] for f in report.actual_schemas['a']['elements']], ['original'])
        self.assertEqual(report.journal[-1].recovery[-1].request.schema,
                         report.journal[-1].preimage.state.payload.schema)
        self.assertTrue(all(entry.outcome == Outcome.INTENDED for entry in report.journal))

    async def test_error_after_commit_stops_and_can_be_recovered(self):
        report, service, _, choose = await self.run_plan(modes=('commit_error',), choice='rollback_last')
        self.assertEqual(report.status, 'rolled_back_last')
        self.assertEqual(len(service.writes), 2)
        self.assertEqual(report.journal[0].outcome, Outcome.INTENDED)
        self.assertIsNotNone(report.journal[0].evidence.failure)
        choose.assert_awaited_once()

    async def test_error_before_commit_rollback_last_skips_unchanged_attempt(self):
        report, service, _, _ = await self.run_plan(modes=('commit', 'unchanged_error'), choice='rollback_last')
        self.assertEqual(report.status, 'rolled_back_last')
        self.assertEqual([identity for identity, _ in service.writes], ['a', 'b', 'a'])
        self.assertEqual(report.journal[-1].outcome, Outcome.UNCHANGED)
        self.assertEqual(report.journal[-1].recovery, ())

    async def test_wrong_state_success_is_divergent_and_blocks_both_recovery_choices(self):
        for choice in ('rollback_last', 'rollback_all'):
            report, service, _, _ = await self.run_plan(modes=('commit', 'divergent'), choice=choice)
            self.assertEqual(report.status, 'recovery_blocked')
            self.assertEqual(len(service.writes), 2)
            self.assertEqual(report.journal[-1].outcome, Outcome.DIVERGENT)
            self.assertEqual(report.journal[-1].recovery[-1].status, 'blocked')

    async def test_unknown_reconciles_to_intended_before_recovery(self):
        report, service, _, _ = await self.run_plan(sequence=('a',), choice='rollback_all',
            configure=lambda service, _: service.fail_reads.add(3))
        self.assertEqual(report.status, 'rolled_back_all')
        self.assertEqual(report.journal[0].outcome, Outcome.UNKNOWN)
        self.assertEqual(report.journal[0].recovery[0].message, 'intended')
        self.assertEqual(len(service.writes), 2)

    async def test_unknown_reconciles_unchanged_and_rollback_last_selects_prior_change(self):
        report, service, _, _ = await self.run_plan(modes=('commit', 'unchanged_error'), choice='rollback_last',
            configure=lambda service, _: service.fail_reads.add(6))
        self.assertEqual(report.status, 'rolled_back_last')
        self.assertEqual([identity for identity, _ in service.writes], ['a', 'b', 'a'])
        self.assertEqual(report.journal[-1].outcome, Outcome.UNKNOWN)
        self.assertEqual(report.journal[-1].recovery[0].message, 'unchanged')

    async def test_unknown_or_divergent_reconciliation_blocks_earlier_recovery(self):
        for mode, failures in (('commit', {6, 7}), ('divergent', {6})):
            report, service, _, _ = await self.run_plan(modes=('commit', mode), choice='rollback_all',
                configure=lambda service, _, failures=failures: service.fail_reads.update(failures))
            self.assertEqual(report.status, 'recovery_blocked')
            self.assertEqual(len(service.writes), 2)

    async def test_leave_preserves_unknown_state_and_performs_no_recovery(self):
        report, service, _, _ = await self.run_plan(sequence=('a',), choice='leave',
            configure=lambda service, _: service.fail_reads.add(3))
        self.assertEqual(report.status, 'left')
        self.assertEqual(report.actual_schemas, {})
        self.assertEqual(len(service.writes), 1)

    async def test_concurrent_change_before_recovery_is_preserved(self):
        def configure(service, executor):
            def mutate(count, key):
                if count == 4:
                    service.schemas['a']['label'] = 'Concurrent'
            service.before_read = mutate
        report, service, _, _ = await self.run_plan(sequence=('a',), modes=('commit_error',),
            choice='rollback_all', configure=configure)
        self.assertEqual(report.status, 'recovery_blocked')
        self.assertEqual(len(service.writes), 1)
        self.assertEqual(report.actual_schemas['a']['label'], 'Concurrent')

    async def test_recovery_error_reads_back_and_stops_even_when_restored(self):
        report, service, _, _ = await self.run_plan(modes=('commit', 'commit_error', 'commit_error'), choice='rollback_all')
        self.assertEqual(report.status, 'recovery_failed')
        self.assertEqual(len(service.writes), 3)
        self.assertEqual(report.journal[-1].recovery[-1].status, 'verified_restored')
        self.assertEqual(report.journal[0].recovery, ())

    async def test_unverified_recovery_success_stops_without_restoring_earlier_actions(self):
        report, service, _, _ = await self.run_plan(modes=('commit', 'commit_error', 'unchanged'), choice='rollback_all')
        self.assertEqual(report.status, 'recovery_failed')
        self.assertEqual(report.journal[-1].recovery[-1].status, 'failed')
        self.assertEqual(len(service.writes), 3)

    async def test_recovery_read_failure_retains_unknown_actual(self):
        report, service, _, _ = await self.run_plan(sequence=('a',), modes=('commit_error',), choice='rollback_all',
            configure=lambda service, _: service.fail_reads.add(5))
        self.assertEqual(report.status, 'recovery_failed')
        self.assertEqual(report.actual_schemas, {})
        self.assertEqual(report.journal[-1].recovery[-1].status, 'unknown')

    async def test_persisted_deletion_only_promises_schema_restoration(self):
        report, service, _, _ = await self.run_plan(sequence=('a',), modes=('commit_error',),
            choice='rollback_all', deletion=True)
        self.assertEqual(report.status, 'schema_restored_data_unverified')
        self.assertIn('record-data restoration', ' '.join(report.messages))
        self.assertEqual(report.journal[0].reviewed.deletions, ('original',))

    async def test_final_report_and_preimages_cannot_be_changed_by_cache_or_service_edits(self):
        report, service, _, _ = await self.run_plan()
        service.schemas['a']['label'] = 'Later'
        shown = report.actual_schemas
        shown['a']['label'] = 'Mutated view'
        self.assertEqual(report.actual_schemas['a']['label'], 'A')
        with self.assertRaises(FrozenInstanceError):
            report.journal[0].status = 'different'
        with self.assertRaises(TypeError):
            report.journal[0].preimage.state.payload.schema['label'] = 'different'

    async def test_confirmation_is_required_and_single_use(self):
        initial, candidate, binding, attempt = setup_plan()
        service = MemoryEvidenceService(initial)
        attempt.cancel()
        with self.assertRaises(PlanError):
            await Executor(service).execute(attempt, binding, candidate, AsyncMock())
        self.assertEqual(service.reads, [])
        initial, candidate, binding, attempt = setup_plan()
        service = MemoryEvidenceService(initial)
        await Executor(service).execute(attempt, binding, candidate, AsyncMock())
        with self.assertRaises(PlanError):
            await Executor(service).execute(attempt, binding, candidate, AsyncMock())

    async def test_context_change_during_preflight_blocks_first_write(self):
        initial, candidate, binding, attempt = setup_plan()
        service = MemoryEvidenceService(initial)
        report = await Executor(service).execute(attempt, binding, candidate, AsyncMock(),
            current_context=lambda: (replace(binding, session_id='different'), candidate))
        self.assertEqual(report.status, 'stopped')
        self.assertEqual(service.writes, [])

    async def test_schema_equivalent_noop_is_recorded_without_a_write(self):
        before = schema_state()
        effect = schema_effect(before, [], 'version-only')
        data = effect.after.payload.to_dict()
        data['schemaVersion'] = 2
        effect = replace(effect, after=replace(effect.after, payload=FormPayload(data)))
        builder = PlanBuilder()
        builder.observe([BaselineEvidence(before.key, Existence.PRESENT, before)])
        builder.add(effect)
        candidate = builder.snapshot()
        binding = AttemptBinding('evaluation', {}, 'session')
        attempt = ConfirmationAttempt(candidate, binding)
        attempt.confirm(attempt.plan.attempt_id, binding, candidate)
        service = MemoryEvidenceService({'a': before})
        report = await Executor(service).execute(attempt, binding, candidate, AsyncMock())
        self.assertEqual(report.journal[0].status, 'skipped')
        self.assertEqual(service.writes, [])

    async def test_absent_baseline_is_not_inferred_from_action_before_state(self):
        initial, candidate, binding, _ = setup_plan()
        candidate = replace(candidate, baselines=())
        attempt = ConfirmationAttempt(candidate, binding)
        attempt.confirm(attempt.plan.attempt_id, binding, candidate)
        service = MemoryEvidenceService(initial)
        report = await Executor(service).execute(attempt, binding, candidate, AsyncMock())
        self.assertEqual(report.status, 'preflight_failed')
        self.assertEqual(service.writes, [])

    async def test_task_interruption_after_commit_retains_evidence_and_stops_without_dialog(self):
        initial, candidate, binding, attempt = setup_plan()
        service = MemoryEvidenceService(initial)
        original = service.write_schema_evidence
        async def interrupted(identity, schema):
            await original(identity, schema)
            raise asyncio.CancelledError()
        service.write_schema_evidence = interrupted
        choose = AsyncMock()
        report = await Executor(service).execute(attempt, binding, candidate, choose)
        self.assertEqual(report.status, 'interrupted')
        self.assertEqual(report.journal[0].outcome, Outcome.INTENDED)
        self.assertEqual(report.action_statuses[-1][1], 'not_started')
        self.assertEqual(len(service.writes), 1)
        choose.assert_not_awaited()
