"""Confirmed-plan execution with immutable evidence and verified compensation.

Read/check/write is optimistic: the endpoints provide no conditional write that
closes the concurrency window. No response receipt alone establishes ownership.
"""

from dataclasses import dataclass, replace
import asyncio
from datetime import datetime, timezone
from enum import StrEnum

from .action_handlers import SchemaHandler, PersistedFieldDeleteHandler, ResourceHandler, compare_resource_footprints
from .endpoint_evidence import EndpointFailure, FailureKind, Observation, WriteEvidence
from .immutable import thaw
from .planning import ActionKind, PlanError
from .resource_state import Existence, ResourceKind


class Outcome(StrEnum):
    INTENDED = 'intended'
    UNCHANGED = 'unchanged'
    DIVERGENT = 'divergent'
    UNKNOWN = 'unknown'


def timestamp():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RecoveryRecord:
    status: str
    observed: Observation
    evidence: WriteEvidence | None = None
    message: str = ''
    at: str = ''
    request: object = None


@dataclass(frozen=True)
class JournalEntry:
    reviewed: object
    status: str
    preimage: Observation
    started_at: str
    outcome: Outcome = Outcome.UNKNOWN
    evidence: WriteEvidence | None = None
    postimage: Observation | None = None
    finished_at: str | None = None
    recovery: tuple[RecoveryRecord, ...] = ()


@dataclass(frozen=True)
class ExecutionReport:
    plan: object
    status: str
    journal: tuple[JournalEntry, ...]
    observations: tuple[Observation, ...]
    messages: tuple[str, ...]
    preflight: tuple[Observation, ...]

    @property
    def action_statuses(self):
        reached = {entry.reviewed.action.action_id: entry.status for entry in self.journal}
        return tuple((item.action.action_id, reached.get(item.action.action_id, 'not_started'))
                     for item in self.plan.actions)

    @property
    def actual_schemas(self):
        # Never fill unreadable state with an intended payload or older snapshot.
        return {item.key.identity: item.state.payload.to_dict() for item in self.observations
                if item.key.kind == ResourceKind.FORM_SCHEMA and item.existence == Existence.PRESENT}


class Executor:
    def __init__(self, service, *, handler_factory=None):
        self.service = service
        self.handler_factory = handler_factory
        self.journal = ()
        self._observations = {}
        self._preflight = ()
        self._messages = []
        self._used = False
        self._cancelled = False

    def handler(self, action):
        if self.handler_factory:
            return self.handler_factory(action)
        if action.kind == ActionKind.DATABASE_RESOURCE_PATCH:
            return ResourceHandler(self.service, action)
        cls = PersistedFieldDeleteHandler if action.kind == ActionKind.PERSISTED_FIELD_DELETE else SchemaHandler
        return cls(self.service, related_schemas=thaw(self.plan.related_schemas),
                   reference_defaults=thaw(self.plan.reference_defaults))

    async def observe(self, key):
        try:
            result = await self.service.observe_resource(key)
        except asyncio.CancelledError:
            self._cancelled = True
            result = Observation(key, Existence.UNKNOWN, failure=EndpointFailure(
                FailureKind.UNAVAILABLE, 'Execution interrupted during observation.'))
        except Exception as exc:
            result = Observation(key, Existence.UNKNOWN, failure=EndpointFailure(
                FailureKind.UNAVAILABLE, f'{type(exc).__name__}: {exc}'))
        self._observations[key] = result
        return result

    async def observe_action(self, action):
        primary = await self.observe(action.key)
        related = tuple([await self.observe(projection.before.key) for projection in action.projections])
        return replace(primary, related=related)

    def matches_related(self, action, expected, observed):
        found = {item.key: item for item in observed.related}
        for projection, state in zip(action.projections, expected):
            observation = found.get(state.key)
            if observation is None or not compare_resource_footprints(
                    state, observation, projection.footprints).matches:
                return False
        return len(expected) == len(action.projections)

    def matches_bundle(self, handler, action, expected, observed, related):
        return self.matches(handler, expected, observed) and self.matches_related(action, related, observed)

    def matches_observation(self, handler, action, expected, observed):
        return (expected.complete and self.matches_bundle(handler, action, expected.state, observed,
                tuple(item.state for item in expected.related)))

    async def write(self, handler, request):
        try:
            return await handler.write(request)
        except asyncio.CancelledError:
            self._cancelled = True
            return WriteEvidence(failure=EndpointFailure(FailureKind.UNAVAILABLE,
                                                         'Execution interrupted during write; read-back required.'))
        except Exception as exc:
            return WriteEvidence(failure=EndpointFailure(FailureKind.UNAVAILABLE, f'{type(exc).__name__}: {exc}'))

    def matches(self, handler, expected, observed):
        try:
            return handler.compare(expected, observed).matches
        except (KeyError, TypeError, ValueError):
            return False

    def classify(self, handler, action, before, observed):
        if not observed.complete:
            return Outcome.UNKNOWN
        if self.matches_bundle(handler, action, action.after, observed, tuple(p.after for p in action.projections)):
            return Outcome.INTENDED
        if self.matches_observation(handler, action, before, observed):
            return Outcome.UNCHANGED
        return Outcome.DIVERGENT

    def report(self, status):
        return ExecutionReport(self.plan, status, self.journal, tuple(self._observations.values()),
                               tuple(self._messages), self._preflight)

    def record_recovery(self, index, status, observed, evidence=None, message='', request=None):
        entry = self.journal[index]
        record = RecoveryRecord(status, observed, evidence, message, timestamp(), request)
        self.journal = (*self.journal[:index], replace(entry, recovery=(*entry.recovery, record)),
                        *self.journal[index + 1:])

    async def execute(self, attempt, binding, candidate, choose_recovery, *, current_context=None):
        if self._used:
            raise PlanError('An Executor instance serves only one execution attempt.')
        self.plan = attempt.begin_execution(binding, candidate)
        self._used = True
        baselines = {item.key: item for item in self.plan.candidate.baselines}
        actions = self.plan.actions
        required = tuple(dict.fromkeys(key for item in actions for key in (
            item.action.key, *(f.key for f in item.action.read_set if f.phase == 'before'))))
        # Read every required baseline before considering a write, including
        # resources first touched by later actions. Keep all observations on failure.
        invalid = False
        for key in required:
            observed = await self.observe(key)
            self._preflight = (*self._preflight, observed)
            baseline = baselines.get(key)
            owner = next((item.action for item in actions if item.action.key == key), None)
            if baseline is None or baseline.existence != Existence.PRESENT:
                invalid = True
            elif key.kind == ResourceKind.FORM_SCHEMA:
                handler = self.handler(owner or actions[0].action)
                invalid |= not self.matches(handler, baseline.state, observed)
            else:
                footprints = tuple(f for item in actions for f in item.action.read_set if f.key == key)
                invalid |= not compare_resource_footprints(baseline.state, observed, footprints).matches
                for item in actions:
                    if item.action.key == key:
                        invalid |= not self.matches(self.handler(item.action), baseline.state, observed)
        if invalid:
            self._messages.append('Execution stopped before writes: required baseline is unavailable or stale.')
            return self.report('preflight_failed')

        failed = False
        for reviewed in actions:
            action = reviewed.action
            handler = self.handler(action)
            observed = await self.observe_action(action)
            if not self.matches_bundle(handler, action, action.before, observed, tuple(p.before for p in action.projections)):
                self._messages.append(f'{action.action_id}: immediate predecessor is unavailable or stale.')
                failed = True
                break
            # Prerequisites explicitly declared by the planner only. No inferred
            # cross-form dependency scan or execution-time command reevaluation.
            prerequisites_ok = True
            for footprint in action.read_set:
                if not footprint.existence or footprint.phase != 'before':
                    continue
                dependency = observed if footprint.key == action.key else await self.observe(footprint.key)
                if dependency.existence != Existence.PRESENT or dependency.failure:
                    prerequisites_ok = False
                    break
                payload = dependency.state.payload
                members = (payload.order if hasattr(payload, 'order') else
                           tuple(field['id'] for field in payload.schema['elements']))
                if footprint.member_id not in members:
                    prerequisites_ok = False
                    break
            if not prerequisites_ok:
                self._messages.append(f'{action.action_id}: required member no longer exists.')
                failed = True
                break
            if current_context is not None:
                try:
                    attempt.require_confirmed(*current_context())
                except PlanError as exc:
                    self._messages.append(str(exc))
                    failed = True
                    break
            if self.matches_bundle(handler, action, action.after, observed, tuple(p.after for p in action.projections)):
                self.journal = (*self.journal, JournalEntry(reviewed, 'skipped', observed, timestamp(),
                    Outcome.UNCHANGED, postimage=observed, finished_at=timestamp()))
                continue
            # Publish the attempt before invoking the endpoint. All retained state
            # and request snapshots are recursively immutable canonical values.
            entry = JournalEntry(reviewed, 'attempted', observed, timestamp())
            self.journal = (*self.journal, entry)
            evidence = await self.write(handler, reviewed.request)
            self.journal = (*self.journal[:-1], replace(entry, evidence=evidence))
            actual = await self.observe_action(action)
            outcome = self.classify(handler, action, observed, actual)
            status = {Outcome.INTENDED: 'verified_applied', Outcome.UNCHANGED: 'verified_unchanged',
                      Outcome.DIVERGENT: 'failed_with_observed_state', Outcome.UNKNOWN: 'unknown_outcome'}[outcome]
            self.journal = (*self.journal[:-1], replace(entry, status=status, outcome=outcome,
                evidence=evidence, postimage=actual, finished_at=timestamp()))
            self._messages.append(f'{action.action_id}: {outcome.value}' +
                                  (f'; API error: {evidence.failure.message}' if evidence.failure else ''))
            if outcome == Outcome.DIVERGENT:
                summary = handler.compare(action.after, actual).summary
                self._messages.append(summary or 'The related resource representation differs from the intended shared label.')
            if outcome != Outcome.INTENDED or evidence.failure is not None:
                failed = True
                break
        limits = [item.recovery_limit for item in actions]
        if 'schema_only_data_unverified' in limits:
            self._messages.append('Persisted field deletion: schema-only compensation; record-data restoration is unverified.')
        if not failed:
            self._messages.append('All requested updates verified.' if self.journal else 'No updates required.')
            return self.report('success')
        if not any(entry.status != 'skipped' for entry in self.journal):
            return self.report('stopped')
        if self._cancelled:
            self._messages.append('Execution interrupted; write/read-back evidence retained without automatic recovery.')
            return self.report('interrupted')
        try:
            choice = await choose_recovery('\n'.join(self._messages))
        except (Exception, asyncio.CancelledError) as exc:
            self._messages.append(f'Recovery decision interrupted: {type(exc).__name__}; changes left in place.')
            return self.report('interrupted')
        if choice not in ('rollback_last', 'rollback_all'):
            self._messages.append('Changes left in place; unresolved outcomes remain explicit.')
            return self.report('left')
        return await self.recover(choice)

    async def recover(self, choice):
        restored = 0
        data_unverified = False
        for index in range(len(self.journal) - 1, -1, -1):
            entry = self.journal[index]
            if entry.status == 'skipped' or entry.outcome == Outcome.UNCHANGED:
                continue
            action = entry.reviewed.action
            handler = self.handler(action)
            current = await self.observe_action(action)
            if entry.outcome == Outcome.DIVERGENT:
                reason = 'Divergent state does not establish ownership; automatic recovery blocked.'
                self.record_recovery(index, 'blocked', current, message=reason)
                self._messages.append(reason)
                return self.report('recovery_blocked')
            if entry.outcome == Outcome.UNKNOWN:
                reconciled = self.classify(handler, action, entry.preimage, current)
                self.record_recovery(index, 'reconciled', current, message=reconciled.value)
                if reconciled == Outcome.UNCHANGED:
                    continue
                safe = reconciled == Outcome.INTENDED
            else:
                safe = self.matches_observation(handler, action, entry.postimage, current)
            if not safe:
                reason = 'Recovery stopped: outcome is unresolved or the observed postimage changed.'
                self.record_recovery(index, 'blocked', current, message=reason)
                self._messages.append(reason)
                return self.report('recovery_blocked')
            # Restore the immediate observed preimage, never a stale plan baseline.
            request = handler.recovery_request(action, entry.preimage.state)
            self.record_recovery(index, 'attempted', current, request=request)
            evidence = await self.write(handler, request)
            actual = await self.observe_action(action)
            verified = self.matches_observation(handler, action, entry.preimage, actual)
            status = 'verified_restored' if verified else (
                'unknown' if not actual.complete else 'failed')
            self.record_recovery(index, status, actual, evidence, request=request)
            if evidence.failure or not verified:
                self._messages.append('Recovery stopped after an API error or unverified restoration; read-back evidence retained.')
                return self.report('recovery_failed')
            restored += 1
            data_unverified |= entry.reviewed.recovery_limit == 'schema_only_data_unverified'
            if choice == 'rollback_last':
                break
        self._messages.append(f'{restored} update(s) restored and verified at the schema level.')
        if data_unverified:
            self._messages.append('Record-data restoration remains unverified.')
        return self.report('schema_restored_data_unverified' if data_unverified else
                           'rolled_back_all' if choice == 'rollback_all' else 'rolled_back_last')
