"""Confirmed-plan execution with immutable evidence and verified compensation.

Read/check/write is optimistic: the endpoints provide no conditional write that
closes the concurrency window. No response receipt alone establishes ownership.
"""

from dataclasses import dataclass, replace
import asyncio
from datetime import datetime, timezone
from enum import StrEnum

from .action_handlers import AddFormHandler, SchemaHandler, PersistedFieldDeleteHandler, ResourceHandler, DeleteFormHandler, compare_resource_footprints
from .endpoint_evidence import EndpointFailure, FailureKind, Observation, WriteEvidence
from .immutable import thaw
from .execution_presentation import recovery_console_message
from .planning import ActionKind, PlanError
from .resource_state import Existence, ResourceKind
from .schema_change import unexpected_schema_changes


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
    console_messages: tuple[str, ...] = ()

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


class StepStopped(Exception):
    """Stop requested at an execution boundary."""


class Executor:
    def __init__(self, service, *, handler_factory=None, step_control=None):
        self.step_control = step_control
        self.service = service
        self.handler_factory = handler_factory
        self.journal = ()
        self._observations = {}
        self._preflight = ()
        self._messages = []
        self.console_messages = []
        self._used = False
        self._cancelled = False

    def handler(self, action):
        if self.handler_factory:
            return self.handler_factory(action)
        if action.kind == ActionKind.FORM_DELETE:
            return DeleteFormHandler(self.service, action)
        if action.kind == ActionKind.DATABASE_RESOURCE_PATCH:
            return ResourceHandler(self.service, action)
        cls = AddFormHandler if action.kind == ActionKind.FORM_CREATE else PersistedFieldDeleteHandler if action.kind == ActionKind.PERSISTED_FIELD_DELETE else SchemaHandler
        return cls(self.service, related_schemas=thaw(self.plan.related_schemas),
                   reference_defaults=thaw(self.plan.reference_defaults))

    @staticmethod
    def action_label(action):
        if action.key.kind == ResourceKind.FORM_SCHEMA:
            label = action.after.payload.schema.get('label') or action.key.identity
            operation = 'Add form' if action.kind == ActionKind.FORM_CREATE else 'Update form'
            return f'{operation} "{label}" ({action.key.identity})'
        if action.kind == ActionKind.FORM_DELETE:
            return 'Delete forms: ' + ', '.join(state.payload.schema.get('label') or state.key.identity for state in action.deleted_forms)
        return f'Update database resources ({action.key.identity})'

    @staticmethod
    def outcome_message(action, outcome):
        if outcome == Outcome.INTENDED:
            return {
                ActionKind.FORM_DELETE: 'Deleted and verified',
                ActionKind.FORM_CREATE: 'Created and verified',
                ActionKind.FORM_SCHEMA_UPDATE: 'Updated and verified',
                ActionKind.PERSISTED_FIELD_DELETE: 'Field deletion verified',
                ActionKind.DATABASE_RESOURCE_PATCH: 'Moved and verified',
            }[action.kind]
        return {
            Outcome.UNCHANGED: 'No changes detected',
            Outcome.DIVERGENT: 'Observed state differs from the requested result',
            Outcome.UNKNOWN: 'Could not verify the result',
        }[outcome]

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
        if action.kind == ActionKind.FORM_DELETE and primary.existence == Existence.PRESENT:
            for state in action.deleted_forms:
                if state.key.identity not in primary.state.payload.entries:
                    self._observations[state.key] = Observation(state.key, Existence.ABSENT,
                        failure=EndpointFailure(FailureKind.MISSING, 'Form absent from database tree.'))
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
        if expected.existence == Existence.ABSENT:
            return (expected.key == observed.key and expected.complete and observed.complete and observed.existence == Existence.ABSENT
                    and self.matches_related(action, tuple(item.state for item in expected.related), observed))
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
                               tuple(self._messages), self._preflight, tuple(self.console_messages))

    def message(self, message):
        self._messages.append(message)
        for entry in reversed(self.journal):
            result = f'{self.action_label(entry.reviewed.action)}: {self.outcome_message(entry.reviewed.action, entry.outcome)}'
            if message.startswith(result) and entry.evidence and entry.evidence.receipt:
                message += f' (HTTP {entry.evidence.receipt.status})'
                break
        self.console_messages.append(message)

    def record_recovery(self, index, status, observed, evidence=None, message='', request=None):
        entry = self.journal[index]
        record = RecoveryRecord(status, observed, evidence, message, timestamp(), request)
        self.console_messages.append(recovery_console_message(record))
        self.journal = (*self.journal[:index], replace(entry, recovery=(*entry.recovery, record)),
                        *self.journal[index + 1:])

    async def step(self, phase, reviewed=None, request=None):
        if self.step_control is not None:
            return await self.step_control(self, phase, reviewed, request)

    async def execute(self, attempt, binding, candidate, choose_recovery, *, current_context=None):
        try:
            return await self._execute(attempt, binding, candidate, choose_recovery,
                                       current_context=current_context)
        except StepStopped:
            self.message('Execution stopped by user; verified evidence retained.')
            return self.report('interrupted')

    async def _execute(self, attempt, binding, candidate, choose_recovery, *, current_context=None):
        if self._used:
            raise PlanError('An Executor instance serves only one execution attempt.')
        self.plan = attempt.begin_execution(binding, candidate)
        self._used = True
        baselines = {item.key: item for item in self.plan.candidate.baselines}
        actions = self.plan.actions
        required = tuple(dict.fromkeys(key for item in actions for key in (
            item.action.key, *(f.key for f in item.action.read_set if f.phase == 'before'),
            *(state.key for state in item.action.deleted_forms))))
        # Read every required baseline before considering a write, including
        # resources first touched by later actions. Keep all observations on failure.
        invalid = False
        for key in required:
            observed = await self.observe(key)
            self._preflight = (*self._preflight, observed)
            baseline = baselines.get(key)
            owner = next((item.action for item in actions if item.action.key == key), None)
            if baseline is not None and baseline.existence == Existence.ABSENT:
                invalid |= not (owner is not None and owner.kind == ActionKind.FORM_CREATE
                                and observed.existence == Existence.ABSENT and observed.complete)
            elif baseline is None or baseline.existence != Existence.PRESENT:
                invalid = True
            elif key.kind == ResourceKind.FORM_SCHEMA:
                handler = self.handler(owner) if owner else SchemaHandler(self.service, related_schemas=thaw(self.plan.related_schemas))
                invalid |= not self.matches(handler, baseline.state, observed)
            else:
                footprints = tuple(f for item in actions for f in item.action.read_set if f.key == key)
                invalid |= not compare_resource_footprints(baseline.state, observed, footprints).matches
                for item in actions:
                    if item.action.key == key:
                        invalid |= not self.matches(self.handler(item.action), baseline.state, observed)
        if invalid:
            self.message('Execution stopped before writes: required baseline is unavailable or stale.')
            return self.report('preflight_failed')

        failed = False
        for reviewed in actions:
            action = reviewed.action
            handler = self.handler(action)
            await self.step('forward', reviewed, reviewed.request)
            observed = await self.observe_action(action)
            if not self.matches_bundle(handler, action, action.before, observed, tuple(p.before for p in action.projections)):
                self.message(f'{self.action_label(action)}: immediate predecessor is unavailable or stale.')
                failed = True
                break
            if action.kind == ActionKind.FORM_DELETE:
                schema_handler = SchemaHandler(self.service, related_schemas=thaw(self.plan.related_schemas))
                schemas_match = True
                for state in action.deleted_forms:
                    schemas_match &= self.matches(schema_handler, state, await self.observe(state.key))
                if not schemas_match:
                    self.message('Deletion stopped: a target schema changed since application.')
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
                self.message(f'{self.action_label(action)}: required member no longer exists.')
                failed = True
                break
            if current_context is not None:
                try:
                    attempt.require_confirmed(*current_context())
                except PlanError as exc:
                    self.message(str(exc))
                    failed = True
                    break
            if self.matches_bundle(handler, action, action.after, observed, tuple(p.after for p in action.projections)):
                self.journal = (*self.journal, JournalEntry(reviewed, 'skipped', observed, timestamp(),
                    Outcome.UNCHANGED, postimage=observed, finished_at=timestamp()))
                if await self.step('applied', reviewed) == 'rollback':
                    return await self.recover('rollback_all')
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
            self.message(f'{self.action_label(action)}: {self.outcome_message(action, outcome)}' +
                                  (f'; API error: {evidence.failure.message}' if evidence.failure else ''))
            if outcome == Outcome.DIVERGENT:
                summary = handler.compare(action.after, actual).summary
                self.message(summary or 'The related resource representation differs from the requested shared label.')
            if outcome == Outcome.INTENDED and evidence.failure is None:
                if await self.step('applied', reviewed) == 'rollback':
                    return await self.recover('rollback_all')
            if outcome != Outcome.INTENDED or evidence.failure is not None:
                failed = True
                break
        if not failed:
            self.message('All requested updates verified.' if self.journal else 'No updates required.')
            return self.report('success')
        if not any(entry.status != 'skipped' for entry in self.journal):
            return self.report('stopped')
        if self._cancelled:
            self.message('Execution interrupted; write/read-back evidence retained without automatic recovery.')
            return self.report('interrupted')
        try:
            choice = ('rollback_all' if self.step_control is not None else
                      await choose_recovery('\n'.join(self._messages)))
        except (Exception, asyncio.CancelledError) as exc:
            self.message(f'Recovery decision interrupted: {type(exc).__name__}; changes left in place.')
            return self.report('interrupted')
        if choice not in ('rollback_last', 'rollback_all'):
            self.message('Changes left in place; unresolved outcomes remain explicit.')
            return self.report('left')
        return await self.recover(choice)

    async def recover(self, choice):
        restored = 0
        for index in range(len(self.journal) - 1, -1, -1):
            entry = self.journal[index]
            if entry.status == 'skipped' or entry.outcome == Outcome.UNCHANGED:
                await self.step('rollback_skip', entry.reviewed)
                continue
            action = entry.reviewed.action
            handler = self.handler(action)
            current = await self.observe_action(action)
            schema_update = action.kind == ActionKind.FORM_SCHEMA_UPDATE
            if entry.outcome == Outcome.DIVERGENT and not schema_update:
                reason = 'Divergent state does not establish ownership; automatic recovery blocked.'
                self.record_recovery(index, 'blocked', current, message=reason)
                self.message(reason)
                return self.report('recovery_blocked')
            outcome = entry.outcome
            if outcome == Outcome.UNKNOWN:
                outcome = self.classify(handler, action, entry.preimage, current)
                self.record_recovery(index, 'reconciled', current, message=outcome.value)
                if outcome == Outcome.UNCHANGED:
                    await self.step('rollback_skip', entry.reviewed)
                    continue
                reason = ('Recovery stopped: outcome is unresolved or the observed postimage changed.'
                          if outcome != Outcome.INTENDED and not (schema_update and outcome == Outcome.DIVERGENT)
                          else '')
            else:
                reason = self.recovery_state_change(handler, action, entry.postimage, current)
            if not reason and schema_update and outcome == Outcome.DIVERGENT:
                reason = self.partial_schema_recovery_block(action, entry.preimage, current)
                if not reason:
                    self.message(f'{self.action_label(action)}: partial update detected. Restoring the previous schema.')
            if reason:
                return self.block_recovery(index, current, reason, schema_update)
            # Restore the immediate observed preimage, never a stale plan baseline.
            try:
                request = (handler.recovery_request(action, entry.preimage.state, entry.evidence)
                           if action.kind == ActionKind.FORM_DELETE else handler.recovery_request(action, entry.preimage.state))
            except PlanError as exc:
                self.record_recovery(index, 'blocked', current, message=str(exc))
                self.message(str(exc))
                return self.report('recovery_blocked')
            await self.step('rollback', entry.reviewed, request)
            if self.step_control is not None:
                latest = await self.observe_action(action)
                reason = self.recovery_state_change(handler, action, current, latest, paused=True)
                if reason:
                    return self.block_recovery(index, latest, reason, schema_update)
            self.record_recovery(index, 'attempted', current, request=request)
            evidence = await self.write(handler, request)
            actual = await self.observe_action(action)
            verified = self.matches_observation(handler, action, entry.preimage, actual)
            if verified and action.kind == ActionKind.FORM_DELETE:
                schema_handler = SchemaHandler(self.service, related_schemas=thaw(self.plan.related_schemas))
                for state in action.deleted_forms:
                    verified &= self.matches(schema_handler, state, await self.observe(state.key))
            status = 'verified_restored' if verified else (
                'unknown' if not actual.complete else 'failed')
            self.record_recovery(index, status, actual, evidence, request=request)
            if (evidence.failure and action.kind != ActionKind.FORM_DELETE) or not verified:
                self.message('Recovery stopped after an API error or unverified restoration; read-back evidence retained.')
                return self.report('recovery_failed')
            self.message(f'{self.action_label(action)}: rollback write and verification completed.')
            await self.step('restored', entry.reviewed, request)
            restored += 1
            if choice == 'rollback_last':
                break
        self.message(f'{restored} update(s) restored and verified.')
        return self.report('rolled_back_all' if choice == 'rollback_all' else 'rolled_back_last')

    def block_recovery(self, index, observed, reason, schema_update):
        self.record_recovery(index, 'blocked', observed, message=reason)
        if schema_update:
            self._messages.append(reason)
        else:
            self.message(reason)
        return self.report('recovery_blocked')

    def recovery_state_change(self, handler, action, expected, observed, *, paused=False):
        if action.kind == ActionKind.FORM_SCHEMA_UPDATE and all(
                item.existence == Existence.PRESENT for item in (expected, observed)):
            before = expected.state.payload.to_dict()
            paths = unexpected_schema_changes(before, before, observed.state.payload.to_dict())
            if paths:
                return 'Recovery stopped: unexpected schema changes since read-back:\n' + '\n'.join(paths)
        if self.matches_observation(handler, action, expected, observed):
            return ''
        return ('Recovery stopped: observed state changed while awaiting execution.' if paused else
                'Recovery stopped: outcome is unresolved or the observed postimage changed.')

    def partial_schema_recovery_block(self, action, before, observed):
        if (not observed.complete or observed.existence != Existence.PRESENT
                or not self.matches_related(action, tuple(item.state for item in before.related), observed)):
            return 'Recovery stopped: the current schema or related state could not be verified.'
        paths = unexpected_schema_changes(before.state.payload.to_dict(), action.after.payload.to_dict(),
                                          observed.state.payload.to_dict())
        if paths:
            return 'Recovery stopped: unexpected schema changes outside the partial update:\n' + '\n'.join(paths)
        return ''
