"""Local materialization and revocable confirmation of one exact execution attempt.

No service, parser, variable evaluator or name resolver is used here. The UI can
adopt this contract when stage 5 supplies the complete executor safety path.
"""

from dataclasses import dataclass, replace
from copy import deepcopy
from uuid import uuid4

from .action_handlers import PersistedFieldDeleteHandler, SchemaHandler
from .immutable import freeze
from .planning import ActionKind, PlanError
from .resource_state import FormPayload
from .schema_identity import prepare_schema_for_put
from .schema_names import readable_schema, schema_with_ids


@dataclass(frozen=True)
class AttemptBinding:
    evaluation_id: str
    source: object
    session_id: str

    def __post_init__(self):
        if not self.evaluation_id or not self.session_id:
            raise ValueError('An attempt requires evaluation and session identities.')
        object.__setattr__(self, 'source', freeze(self.source))


@dataclass(frozen=True)
class ReviewedAction:
    action: object
    request: object
    destinations: tuple
    deletions: tuple
    recovery_limit: str


@dataclass(frozen=True)
class ExecutionPlan:
    attempt_id: str
    binding: AttemptBinding
    candidate: object
    identity_map: object
    actions: tuple[ReviewedAction, ...]
    related_schemas: object
    reference_defaults: object


def materialize_plan(candidate, binding, *, related_schemas=None, reference_defaults=None):
    """Allocate once across every active action snapshot, retaining historical previews."""
    identity_map = {}
    related = deepcopy(dict(related_schemas or {}))
    for action in candidate.actions:
        for state in (action.before, action.after):
            if isinstance(state.payload, FormPayload):
                related[state.key.identity] = state.payload.to_dict()
    prepared = {}
    # Reserve all persisted identities across forms before allocation (including
    # deleted members). The existing allocator checks identity_map values too.
    from .schema_identity import schema_identities
    for form_id, schema in related.items():
        for identity in schema_identities(schema):
            if isinstance(identity, str) and not identity.startswith('pending-'):
                identity_map[(form_id, identity)] = identity
    for evidence in candidate.baselines:
        if evidence.state is not None and isinstance(evidence.state.payload, FormPayload):
            for identity in schema_identities(evidence.state.payload.to_dict()):
                if isinstance(identity, str):
                    identity_map[(evidence.key.identity, identity)] = identity
    for action in candidate.actions:
        if action.kind == ActionKind.DATABASE_RESOURCE_PATCH:
            raise PlanError('Resource request encoding remains a stage-6 capability.')
        for phase in ('before', 'after'):
            state = getattr(action, phase)
            schema = state.payload.to_dict()
            data = prepare_schema_for_put(schema, related, identity_map=identity_map)
            # Metadata IDs use fixed names rather than CUIDs, but still need the
            # same reference/provenance remapping as ordinary field identities.
            for old, new in zip(schema['elements'], data['elements']):
                if old['id'] != new['id']:
                    identity_map[(state.key.identity, old['id'])] = new['id']
            prepared[(action.action_id, phase)] = data

    def mapped(form_id, value):
        return identity_map.get((form_id, value), value) if isinstance(value, str) else value

    def remap_schema(schema, form_id):
        def visit(node, owner=form_id):
            if isinstance(node, dict):
                target = node.get('formId', owner)
                for key, value in list(node.items()):
                    if key in ('id', 'recordLabelFieldId'):
                        node[key] = mapped(owner, value)
                    elif key == 'fieldId':
                        node[key] = mapped(target, value)
                    else:
                        visit(value, target)
            elif isinstance(node, list):
                for value in node:
                    visit(value, owner)
        visit(schema)
        return schema

    # Convert identifiers through the existing context-aware formula walker. In
    # REF.FIELD, FIELD belongs to REF's form, even when both forms use the same
    # preview token. Quoted literals must never be string-replaced.
    readable = {key: readable_schema(data, related, formulas_only=True) for key, data in prepared.items()}
    materialized_related = {identity: remap_schema(dict(schema), identity) for identity, schema in related.items()}
    for action in candidate.actions:
        materialized_related[action.key.identity] = remap_schema(
            prepared[(action.action_id, 'after')], action.key.identity)

    def member(ref):
        return replace(ref, member_id=mapped(ref.key.identity, ref.member_id))

    actions = []
    for action in candidate.actions:
        owner = action.key.identity
        before, after = (replace(getattr(action, phase), payload=FormPayload(
            schema_with_ids(remap_schema(readable[(action.action_id, phase)], owner), materialized_related)))
            for phase in ('before', 'after'))
        actions.append(replace(action, before=before, after=after,
            start_member_ids=tuple(mapped(owner, value) for value in action.start_member_ids),
            created_member_ids=tuple(mapped(owner, value) for value in action.created_member_ids),
            deleted_member_id=mapped(owner, action.deleted_member_id),
            prerequisites=tuple(member(ref) for ref in action.prerequisites),
            read_set=tuple(replace(f, member_id=mapped(f.key.identity, f.member_id)) for f in action.read_set),
            write_set=tuple(replace(f, member_id=mapped(f.key.identity, f.member_id)) for f in action.write_set)))
    materialized = replace(candidate, actions=tuple(actions), member_provenance=tuple(
        replace(row, member=member(row.member)) for row in candidate.member_provenance))
    reviewed = []
    for action in actions:
        handler = (PersistedFieldDeleteHandler if action.kind == ActionKind.PERSISTED_FIELD_DELETE else SchemaHandler)(
            None, related_schemas=related, reference_defaults=reference_defaults)
        reviewed.append(ReviewedAction(action, handler.encode_write(action), (),
            (action.deleted_member_id,) if action.deleted_member_id else (), action.recovery_capability))
    for action in actions:
        materialized_related[action.key.identity] = action.after.payload.to_dict()
    return ExecutionPlan(uuid4().hex, binding, materialized, freeze(identity_map), tuple(reviewed),
                         freeze(materialized_related), freeze(reference_defaults or {}))


class ConfirmationAttempt:
    """Single-attempt gate. Call require_confirmed at the executor entry point.

    Current binding and candidate are supplied again to catch source/session/
    evaluation changes even if the caller missed an explicit invalidate callback.
    """
    def __init__(self, candidate, binding, **context):
        self._candidate = candidate
        self._plan = materialize_plan(candidate, binding, **context)
        self._valid = True
        self._confirmed = False

    @property
    def plan(self):
        return self._plan

    def invalidate(self):
        self._valid = False
        self._confirmed = False

    cancel = invalidate

    def _check(self, binding, candidate):
        if not self._valid or binding != self.plan.binding or candidate != self._candidate:
            self.invalidate()
            raise PlanError('Execution attempt was cancelled or invalidated; prepare a new confirmation.')

    def confirm(self, attempt_id, binding, candidate):
        self._check(binding, candidate)
        if attempt_id != self.plan.attempt_id:
            raise PlanError('Confirmation belongs to another execution attempt.')
        self._confirmed = True
        return self.plan

    def require_confirmed(self, binding, candidate):
        self._check(binding, candidate)
        if not self._confirmed:
            raise PlanError('Execution requires confirmation of the materialized plan.')
        return self.plan
