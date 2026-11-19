"""Build confirmed plans for low-level regression fixtures (never a second executor).

Older fixture services expose dict reads/puts; this adapter records those through
the same structured observation/write boundary used by the production Executor.
Resolution here is explicit pre-confirmation fixture preparation.
"""
from copy import deepcopy
from types import SimpleNamespace

from activity_manager.confirmed_plan import AttemptBinding, ConfirmationAttempt, materialize_plan
from activity_manager.endpoint_evidence import Observation, WriteEvidence
from activity_manager.executor import Executor
from activity_manager.planning import PlanBuilder, SchemaEffect
from activity_manager.resource_state import BaselineEvidence, Existence, ResourceKey, ResourceState, FormPayload, LoadStatus
from activity_manager.schema_identity import prepare_schema_for_write
from activity_manager.schema_names import resolve_reference_defaults, schema_with_ids


def state(schema):
    return ResourceState(ResourceKey.form(schema['id']), FormPayload(schema), load_status=LoadStatus.LOADED)


def candidate_for(originals, targets):
    builder = PlanBuilder()
    current = {key: state(value) for key, value in originals.items()}
    builder.observe([BaselineEvidence(value.key, Existence.PRESENT, value) for value in current.values()])
    for index, target in enumerate(targets):
        after = state(target)
        # Fixtures specify sequential endpoint actions; an explicit boundary keeps
        # repeated same-form steps separate without interpreting commands.
        builder._seal('fixture_step')
        builder.add(SchemaEffect(str(index), str(index), after.key, current[target['id']], after))
        current[target['id']] = after
    return builder.snapshot()


def materialized_targets(targets, originals, related):
    normalized = deepcopy(targets)
    for schema in normalized:
        for field in schema.get('elements', []):
            field.setdefault('required', False)
            if field.get('type') == 'text':
                field['type'] = 'FREE_TEXT'
    plan = materialize_plan(candidate_for(originals, normalized), AttemptBinding('fixture', {}, 'session'),
                            related_schemas=related)
    return [(item.action.after.payload.to_dict(), deepcopy(dict(related))) for item in plan.actions]


class FixtureAdapter:
    def __init__(self, service):
        self.service = service

    async def observe_resource(self, key):
        data = await self.service.get_form_schema(key.identity)
        return Observation(key, Existence.PRESENT, state(data))

    async def write_schema_evidence(self, identity, schema):
        write = getattr(self.service, 'put_resolved_form_schema', None) or self.service.put_form_schema
        await write(identity, schema)
        return WriteEvidence()


async def execute_sequence_fixture(service, originals, targets, choose_recovery, *, related_schemas=None):
    candidate = candidate_for(originals, targets)
    binding = AttemptBinding('fixture', {}, 'session')
    attempt = ConfirmationAttempt(candidate, binding, related_schemas=related_schemas)
    attempt.confirm(attempt.plan.attempt_id, binding, candidate)
    return await Executor(FixtureAdapter(service)).execute(attempt, binding, candidate, choose_recovery)


async def execute_schema_fixture(service, identity, loaded, target, choose_recovery, *, resolved=False,
                                 related_schemas=None, reference_defaults=None):
    related, defaults = deepcopy(related_schemas or {}), deepcopy(reference_defaults or {})
    try:
        if not resolved:
            if hasattr(service, 'get_form_tree'):
                related.update(await service.get_form_tree(identity))
            target = await prepare_schema_for_write(service, target, related, defaults)
            await resolve_reference_defaults(service, loaded, related, defaults, strict=True)
        loaded = schema_with_ids(loaded, related, defaults)
        candidate = candidate_for({identity: loaded}, [target])
        binding = AttemptBinding('fixture', {}, 'session')
        attempt = ConfirmationAttempt(candidate, binding, related_schemas=related, reference_defaults=defaults)
        attempt.confirm(attempt.plan.attempt_id, binding, candidate)
    except (KeyError, TypeError, ValueError) as exc:
        return SimpleNamespace(status='preparation_failed', actual_schema=None, messages=(str(exc),))
    async def choose(message):
        value = await choose_recovery(message)
        return 'rollback_all' if value == 'rollback' else value
    report = await Executor(FixtureAdapter(service)).execute(attempt, binding, candidate, choose)
    return SimpleNamespace(status=report.status, actual_schema=report.actual_schemas.get(identity),
                           messages=report.messages, report=report)
