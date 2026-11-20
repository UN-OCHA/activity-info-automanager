"""Local materialization and revocable confirmation of one exact execution attempt.

No service, parser, variable evaluator or name resolver is used here. The UI can
adopt this contract when stage 5 supplies the complete executor safety path.
"""

from dataclasses import dataclass, replace
from copy import deepcopy
from uuid import uuid4
import secrets

from .action_handlers import PersistedFieldDeleteHandler, SchemaHandler, ResourceHandler, AddFormHandler
from .immutable import freeze
from .planning import ActionKind, PlanError
from .resource_state import FormPayload, DatabaseResourcesPayload, ResourceKey
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
            continue
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
        if action.kind == ActionKind.DATABASE_RESOURCE_PATCH:
            continue
        materialized_related[action.key.identity] = remap_schema(
            prepared[(action.action_id, 'after')], action.key.identity)

    def member(ref):
        return replace(ref, member_id=mapped(ref.key.identity, ref.member_id))

    actions = []
    for action in candidate.actions:
        if action.kind == ActionKind.DATABASE_RESOURCE_PATCH:
            actions.append(action)
            continue
        owner = action.key.identity
        before, after = (replace(getattr(action, phase), payload=FormPayload(
            schema_with_ids(remap_schema(readable[(action.action_id, phase)], owner), materialized_related)))
            for phase in ('before', 'after'))
        # Update labels are local projections. Creation retains the resource
        # projection to verify placement and deletion as well as its schema.
        projection_keys = {projection.before.key for projection in action.projections}
        actions.append(replace(action, before=before, after=after, projections=action.projections if action.kind == ActionKind.FORM_CREATE else (),
            start_member_ids=tuple(mapped(owner, value) for value in action.start_member_ids),
            created_member_ids=tuple(mapped(owner, value) for value in action.created_member_ids),
            deleted_member_id=mapped(owner, action.deleted_member_id),
            prerequisites=tuple(member(ref) for ref in action.prerequisites),
            read_set=tuple(replace(f, member_id=mapped(f.key.identity, f.member_id)) for f in action.read_set if action.kind == ActionKind.FORM_CREATE or f.key not in projection_keys),
            write_set=tuple(replace(f, member_id=mapped(f.key.identity, f.member_id)) for f in action.write_set if action.kind == ActionKind.FORM_CREATE or f.key not in projection_keys)))
    # Form identities are allocated once, after context-aware field/formula conversion.
    # Only identity-bearing properties are rewritten; labels and formula literals survive.
    form_ids = {}
    reserved = set(related) | set(identity_map.values())
    for action in actions:
        for state in (action.before, action.after, *(p.before for p in action.projections), *(p.after for p in action.projections)):
            if isinstance(state.payload, DatabaseResourcesPayload):
                reserved.update(state.payload.entries)
    for action in actions:
        if action.kind == ActionKind.FORM_CREATE:
            while True:
                identity = 'c' + secrets.token_hex(12)
                if identity not in reserved:
                    break
            reserved.add(identity)
            form_ids[action.key.identity] = identity
            identity_map[(action.key.identity, action.key.identity)] = identity

    def form_key(key):
        return ResourceKey(key.kind, form_ids.get(key.identity, key.identity))

    def form_schema(data):
        data = deepcopy(data)
        def visit(node):
            if isinstance(node, dict):
                for name, value in list(node.items()):
                    if name in ('id', 'formId', 'parentFormId') and isinstance(value, str):
                        node[name] = form_ids.get(value, value)
                    else:
                        visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)
        visit(data)
        return data

    def state_ids(state):
        if isinstance(state.payload, FormPayload):
            payload = FormPayload(form_schema(state.payload.to_dict()))
        else:
            entries = state.payload.to_list()
            for entry in entries:
                for prop in ('id', 'parentId'):
                    if prop in entry:
                        entry[prop] = form_ids.get(entry[prop], entry[prop])
            payload = DatabaseResourcesPayload({entry['id']: entry for entry in entries},
                tombstones=state.payload.tombstones)
        return replace(state, key=form_key(state.key), payload=payload)

    def footprint_ids(f):
        return replace(f, key=form_key(f.key), member_id=form_ids.get(f.member_id, f.member_id))

    def ref_ids(ref):
        return replace(ref, key=form_key(ref.key), member_id=form_ids.get(ref.member_id, ref.member_id))

    actions = [replace(action, key=form_key(action.key), before=state_ids(action.before), after=state_ids(action.after),
        subjects=tuple(form_key(key) for key in action.subjects),
        prerequisites=tuple(ref_ids(ref) for ref in action.prerequisites),
        read_set=tuple(footprint_ids(f) for f in action.read_set), write_set=tuple(footprint_ids(f) for f in action.write_set),
        patches=tuple(replace(patch, member_id=form_ids.get(patch.member_id, patch.member_id),
                             before_value=form_ids.get(patch.before_value, patch.before_value) if isinstance(patch.before_value, str) else patch.before_value,
                             after_value=form_ids.get(patch.after_value, patch.after_value) if isinstance(patch.after_value, str) else patch.after_value)
                      for patch in action.patches),
        projections=tuple(replace(p, before=state_ids(p.before), after=state_ids(p.after),
                                  footprints=tuple(footprint_ids(f) for f in p.footprints)) for p in action.projections))
        for action in actions]
    materialized_related = {form_ids.get(identity, identity): form_schema(schema)
                            for identity, schema in materialized_related.items()}
    materialized = replace(candidate, actions=tuple(actions),
        baselines=tuple(replace(b, key=form_key(b.key), state=state_ids(b.state) if b.state else None)
                        for b in candidate.baselines),
        member_provenance=tuple(replace(row, member=ref_ids(member(row.member))) for row in candidate.member_provenance))
    reviewed = []
    for action in actions:
        if action.kind == ActionKind.DATABASE_RESOURCE_PATCH:
            handler = ResourceHandler(None, action)
            reviewed.append(ReviewedAction(action, handler.encode_write(action),
                tuple((patch.member_id, patch.after_value) for patch in action.patches if patch.property_name == 'parentId'),
                (), action.recovery_capability))
            continue
        handler = (AddFormHandler if action.kind == ActionKind.FORM_CREATE else PersistedFieldDeleteHandler if action.kind == ActionKind.PERSISTED_FIELD_DELETE else SchemaHandler)(
            None, related_schemas=related, reference_defaults=reference_defaults)
        reviewed.append(ReviewedAction(action, handler.encode_write(action), (),
            (action.deleted_member_id,) if action.deleted_member_id else (), action.recovery_capability))
    for action in actions:
        if isinstance(action.after.payload, FormPayload):
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
        self._started = False

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

    def begin_execution(self, binding, candidate):
        plan = self.require_confirmed(binding, candidate)
        if self._started:
            raise PlanError('This execution attempt has already started.')
        self._started = True
        return plan
