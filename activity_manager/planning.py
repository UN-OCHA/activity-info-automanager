"""Candidate action planning. No endpoint calls, materialization or general read analysis."""

from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import uuid4

from .immutable import freeze
from .resource_state import (
    BaselineEvidence, DatabaseResourcesPayload, FormPayload, LoadStatus, Lifecycle, Existence, ResourceKey, ResourceKind, ResourceState,
)


class PlanError(ValueError):
    pass


class ActionKind(StrEnum):
    FORM_DELETE = 'form_delete'
    FORM_CREATE = 'form_create'
    FORM_SCHEMA_UPDATE = 'form_schema_update'
    PERSISTED_FIELD_DELETE = 'persisted_field_delete'
    DATABASE_RESOURCE_PATCH = 'database_resource_patch'


@dataclass(frozen=True)
class MemberRef:
    key: ResourceKey
    member_id: str

    def __post_init__(self):
        if not isinstance(self.key, ResourceKey) or not isinstance(self.member_id, str) or not self.member_id:
            raise TypeError('A member reference needs a resource key and member identity.')


@dataclass(frozen=True)
class Footprint:
    key: ResourceKey
    member_id: str | None = None
    path: tuple[str, ...] = ()
    whole_resource: bool = False
    existence: bool = False
    phase: str = 'before'

    def __post_init__(self):
        object.__setattr__(self, 'path', tuple(self.path))
        if self.phase not in ('before', 'after') or any(not isinstance(part, str) for part in self.path):
            raise ValueError('Invalid footprint phase or property path.')


@dataclass(frozen=True)
class ResourceProjection:
    """A secondary representation changed by the primary endpoint request."""
    before: ResourceState
    after: ResourceState
    footprints: tuple[Footprint, ...]

    def __post_init__(self):
        object.__setattr__(self, 'footprints', tuple(self.footprints))
        if self.before.key != self.after.key or any(f.key != self.before.key for f in self.footprints):
            raise PlanError('Projection identities disagree.')
        if self.before.load_status != LoadStatus.LOADED or self.after.load_status != LoadStatus.LOADED:
            raise PlanError('A projection requires loaded states.')


@dataclass(frozen=True)
class SchemaEffect:
    effect_id: str
    application_id: str
    key: ResourceKey
    before: ResourceState
    after: ResourceState
    prerequisites: tuple[MemberRef, ...] = ()
    dependency_ids: tuple[str, ...] = ()
    projections: tuple[ResourceProjection, ...] = ()
    creation: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'prerequisites', tuple(self.prerequisites))
        object.__setattr__(self, 'dependency_ids', tuple(self.dependency_ids))
        object.__setattr__(self, 'projections', tuple(self.projections))


@dataclass(frozen=True)
class ResourcePatchEffect:
    effect_id: str
    application_id: str
    key: ResourceKey
    before: ResourceState
    after: ResourceState
    changes: tuple[tuple[str, str], ...]
    subjects: tuple[ResourceKey, ...] = ()
    prerequisites: tuple[MemberRef, ...] = ()
    dependency_ids: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ('subjects', 'prerequisites', 'dependency_ids'):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, 'changes', tuple(tuple(change) for change in self.changes))


@dataclass(frozen=True)
class FormDeleteEffect:
    effect_id: str
    application_id: str
    key: ResourceKey
    before: ResourceState
    after: ResourceState
    subjects: tuple[ResourceKey, ...]


@dataclass(frozen=True)
class PropertyPatch:
    member_id: str
    property_name: str
    before_present: bool
    before_value: object
    after_present: bool
    after_value: object

    def __post_init__(self):
        object.__setattr__(self, 'before_value', freeze(self.before_value))
        object.__setattr__(self, 'after_value', freeze(self.after_value))


@dataclass(frozen=True)
class MemberProvenance:
    member: MemberRef
    creator_action_id: str | None
    application_id: str | None = None


@dataclass(frozen=True)
class ApiAction:
    action_id: str
    kind: ActionKind
    group_id: str
    key: ResourceKey
    before: ResourceState
    after: ResourceState
    application_ids: tuple[str, ...]
    effect_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...] = ()
    subjects: tuple[ResourceKey, ...] = ()
    start_member_ids: tuple[str, ...] = ()
    created_member_ids: tuple[str, ...] = ()
    deleted_member_id: str | None = None
    prerequisites: tuple[MemberRef, ...] = ()
    read_set: tuple[Footprint, ...] = ()
    write_set: tuple[Footprint, ...] = ()
    patches: tuple[PropertyPatch, ...] = ()
    recovery_capability: str = 'metadata_compensation'
    warnings: tuple[str, ...] = ()
    projections: tuple[ResourceProjection, ...] = ()
    deleted_forms: tuple[ResourceState, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'kind', ActionKind(self.kind))
        for name in ('application_ids', 'effect_ids', 'dependency_ids', 'subjects', 'start_member_ids',
                     'created_member_ids', 'prerequisites', 'read_set', 'write_set', 'patches', 'warnings', 'projections', 'deleted_forms'):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def is_noop(self):
        return self.before.payload == self.after.payload and self.before.lifecycle == self.after.lifecycle


@dataclass(frozen=True)
class ActionGroup:
    group_id: str
    reason: str
    scope_id: str | None
    action_ids: tuple[str, ...]
    sealed: bool

    def __post_init__(self):
        object.__setattr__(self, 'action_ids', tuple(self.action_ids))


@dataclass(frozen=True)
class CandidatePlan:
    plan_id: str
    actions: tuple[ApiAction, ...]
    groups: tuple[ActionGroup, ...]
    baselines: tuple[BaselineEvidence, ...]
    member_provenance: tuple[MemberProvenance, ...]
    application_order: tuple[str, ...]
    effect_order: tuple[str, ...]
    omitted_actions: tuple[ApiAction, ...] = ()

    def __post_init__(self):
        for name in ('actions', 'groups', 'baselines', 'member_provenance', 'application_order',
                     'effect_order', 'omitted_actions'):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def action_ids_for_effects(self, effect_ids):
        wanted = set(effect_ids)
        return tuple(action.action_id for action in self.actions if wanted.intersection(action.effect_ids))

    def action_ids_for_application(self, application_id):
        return tuple(action.action_id for action in self.actions if application_id in action.application_ids)


def _unique(values):
    return tuple(dict.fromkeys(values))


def _members(state):
    if isinstance(state.payload, FormPayload):
        return tuple(field['id'] for field in state.payload.schema.get('elements', ())
                     if isinstance(field.get('id'), str))
    if isinstance(state.payload, DatabaseResourcesPayload):
        return state.payload.order
    return ()


def _matches(left, right):
    return (left.key, left.payload, left.lifecycle, left.load_status) == (
        right.key, right.payload, right.lifecycle, right.load_status)


class PlanBuilder:
    """Mutable compilation owner; every exposed plan/action is an immutable value."""

    def __init__(self, *, active_form=None, plan_id=None):
        self.plan_id = plan_id or uuid4().hex
        self._actions = []
        self._action_sequence = 0
        self._candidates = {}
        self._groups = {}
        self._sealed = set()
        self._epoch = 0
        self._reason = 'initial'
        self._endpoint = None
        self._active_form = active_form
        self._scope_id = None
        self._scope_keys = ()
        self._baselines = {}
        self._projected = {}
        self._origins = {}
        self._application_order = ()
        self._effect_order = ()

    def observe(self, evidence):
        baselines = dict(self._baselines)
        for item in evidence:
            previous = baselines.get(item.key)
            if previous is not None and previous != item:
                raise PlanError('Baseline evidence changed during planning.')
            baselines[item.key] = item
        origins = dict(self._origins)
        for item in baselines.values():
            if item.state is not None:
                for identity in _members(item.state):
                    ref = MemberRef(item.key, identity)
                    origins.setdefault(ref, MemberProvenance(ref, None))
        self._baselines, self._origins = baselines, origins

    def record_application(self, application_id):
        self._application_order = _unique((*self._application_order, application_id))

    def _seal(self, reason):
        group_id = self._group_id()
        if group_id in self._groups:
            self._sealed.add(group_id)
        self._epoch += 1
        self._reason = reason
        self._candidates = {}
        self._endpoint = None

    def _group_id(self):
        return f'{self.plan_id}:g{self._epoch}'

    def begin_scope(self, scope_id, keys):
        keys = tuple(keys)
        if not scope_id or len(keys) != len(set(keys)) or any(key.kind != ResourceKind.FORM_SCHEMA for key in keys):
            raise PlanError('A scope requires a stable identity and distinct form keys.')
        if self._scope_id != scope_id:
            self._seal('form_scope')
            self._scope_id, self._scope_keys = scope_id, keys
        elif keys != self._scope_keys:
            raise PlanError('A scope target set cannot change after it is frozen.')

    def select_form(self, key, *, scope_id=None):
        if scope_id is not None:
            if self._scope_id != scope_id or key not in self._scope_keys:
                raise PlanError('The form is outside the active scope.')
        else:
            if self._scope_id is not None or (self._active_form is not None and self._active_form != key):
                self._seal('scope_exit' if self._scope_id is not None else 'form_switch')
            self._scope_id, self._scope_keys = None, ()
        self._active_form = key

    def _prepare_endpoint(self, endpoint):
        if self._endpoint is not None and self._endpoint != endpoint:
            self._seal('endpoint_boundary')
        self._endpoint = endpoint

    def snapshot(self):
        active = tuple(action for action in self._actions if not action.is_noop)
        omitted = tuple(action for action in self._actions if action.is_noop)
        groups = tuple(ActionGroup(group_id, reason, scope_id,
            tuple(action.action_id for action in self._actions if action.group_id == group_id),
            group_id in self._sealed) for group_id, (reason, scope_id) in self._groups.items())
        required_keys = {footprint.key for action in active for footprint in (*action.read_set, *action.write_set)}
        return CandidatePlan(self.plan_id, active, groups,
            tuple(value for key, value in self._baselines.items() if key in required_keys),
            tuple(self._origins.values()), self._application_order, self._effect_order, omitted)

    def add(self, effect):
        """Commit a complete compilation change or leave this builder untouched."""
        candidate = deepcopy(self)
        candidate._add(effect)
        self.__dict__.update(candidate.__dict__)
        return self.snapshot().action_ids_for_effects((effect.effect_id,))

    def _add(self, effect):
        if not isinstance(effect, (SchemaEffect, ResourcePatchEffect)):
            raise PlanError('Unsupported effect type.')
        if effect.effect_id in self._effect_order:
            raise PlanError('An effect cannot be compiled twice.')
        if effect.before.key != effect.key or effect.after.key != effect.key:
            raise PlanError('Effect resource identities disagree.')
        if effect.before.load_status != LoadStatus.LOADED or effect.after.load_status != LoadStatus.LOADED:
            raise PlanError('Planning requires loaded predecessor and projected state.')
        baseline = self._baselines.get(effect.key)
        expected = self._projected.get(effect.key, baseline.state if baseline and baseline.state else effect.before)
        if not _matches(expected, effect.before):
            raise PlanError('Effect does not start at its projected predecessor.')
        self._projected.setdefault(effect.key, effect.before)
        for identity in _members(effect.before):
            ref = MemberRef(effect.key, identity)
            self._origins.setdefault(ref, MemberProvenance(ref, None))
        if isinstance(effect, SchemaEffect):
            if effect.creation and (baseline is None or baseline.existence != Existence.ABSENT
                    or effect.before.lifecycle != Lifecycle.DELETED or effect.after.lifecycle != Lifecycle.NEW
                    or any(action.key == effect.key for action in self._actions)):
                raise PlanError('Form creation requires a new identity and an absent baseline.')
            if effect.creation:
                database = ResourceKey.database(effect.after.payload.schema['databaseId'])
                if len(effect.projections) != 1 or effect.projections[0].before.key != database:
                    raise PlanError('Form creation requires its database resource projection.')
                projection = effect.projections[0]
                old, new = projection.before.payload.entries, projection.after.payload.entries
                entry = new.get(effect.key.identity)
                if (effect.key.identity in old or entry is None or entry.get('type') != 'FORM'
                        or entry.get('label') != effect.after.payload.schema['label']
                        or {key: value for key, value in new.items() if key != effect.key.identity} != dict(old)):
                    raise PlanError('Form creation must add exactly its new database resource.')
            if effect.key.kind != ResourceKind.FORM_SCHEMA:
                raise PlanError('A schema effect requires a form resource.')
            if effect.projections:
                if len({p.before.key for p in effect.projections}) != len(effect.projections):
                    raise PlanError('An action cannot project the same resource twice.')
            self._schema(effect)
            if effect.projections:
                action = self._candidate(effect.key, ActionKind.FORM_CREATE) or self._candidate(effect.key, ActionKind.FORM_SCHEMA_UPDATE)
                projections = {p.before.key: p for p in action.projections}
                dependencies = list(action.dependency_ids)
                for projection in effect.projections:
                    expected = self._state_for(projection.before.key)
                    if expected is None or not _matches(expected, projection.before):
                        raise PlanError('Projection does not start at its projected predecessor.')
                    # Local label projections do not order independent schema writes.
                    # Preserve dependencies on actual database resource actions.
                    previous = next((item.action_id for item in reversed(self._actions)
                                     if item.key == projection.before.key and not item.is_noop), None)
                    if previous is not None:
                        dependencies.append(previous)
                    self._projected[projection.before.key] = projection.after
                    prior = projections.get(projection.before.key)
                    projections[projection.before.key] = replace(projection, before=prior.before) if prior else projection
                projections = tuple(projections.values())
                if action.kind == ActionKind.FORM_CREATE:
                    projections = tuple(replace(p, footprints=tuple(Footprint(p.before.key, action.key.identity, (prop,))
                        for prop in ('id', 'type', 'label', 'parentId', 'visibility')) + tuple(
                        Footprint(ref.key, ref.member_id, ('type',)) for ref in action.prerequisites
                        if ref.key == p.before.key)) for p in projections)
                reads = tuple(f for projection in projections for f in projection.footprints)
                self._replace_action(replace(action, projections=projections,
                    dependency_ids=_unique(dependencies), read_set=(*action.read_set, *reads),
                    write_set=(*action.write_set, *(replace(f, phase='after') for f in reads))))
        else:
            if effect.key.kind != ResourceKind.DATABASE_RESOURCES:
                raise PlanError('A resource patch requires a database resource collection.')
            self._resource_patch(effect)
        self._projected[effect.key] = effect.after
        self.record_application(effect.application_id)
        self._effect_order += (effect.effect_id,)

    def _state_for(self, key):
        if key in self._projected:
            return self._projected[key]
        evidence = self._baselines.get(key)
        return evidence.state if evidence is not None else None

    def _dependencies(self, effect, candidate=None):
        dependencies = list(effect.dependency_ids)
        if isinstance(effect.after.payload, FormPayload):
            from .schema_names import referenced_form_ids
            targets = set(referenced_form_ids(effect.after.payload.to_dict())) | {effect.key.identity}
            dependencies.extend(action.action_id for action in self._actions
                                if action.kind == ActionKind.FORM_CREATE and action.key.identity in targets)
        for ref in effect.prerequisites:
            state = self._state_for(ref.key)
            if state is None or ref.member_id not in _members(state):
                raise PlanError('A declared member prerequisite is unavailable.')
            origin = self._origins.get(ref)
            if origin is not None and origin.creator_action_id is not None:
                dependencies.append(origin.creator_action_id)
        active_ids = {action.action_id for action in self._actions if not action.is_noop}
        if candidate is not None:
            active_ids.add(candidate.action_id)
        for identity in dependencies:
            if identity not in active_ids:
                raise PlanError('An explicit action prerequisite is unavailable.')
        return _unique(identity for identity in dependencies
                       if candidate is None or identity != candidate.action_id)

    def _last_action(self, key):
        return next((action.action_id for action in reversed(self._actions)
                     if (action.key == key or any(p.before.key == key for p in action.projections))
                     and not action.is_noop), None)

    def open_creation(self, key):
        return self._candidate(key, ActionKind.FORM_CREATE)

    def _candidate(self, key, kind):
        index = self._candidates.get((key, kind))
        return self._actions[index] if index is not None else None

    def _can_extend(self, action, effect, dependencies):
        if not _matches(action.after, effect.before):
            raise PlanError('An open candidate has the wrong projected predecessor.')
        index = next(i for i, item in enumerate(self._actions) if item.action_id == action.action_id)
        positions = {item.action_id: i for i, item in enumerate(self._actions)}
        if any(positions[identity] > index for identity in dependencies):
            return False
        if action.before.payload == effect.after.payload and any(
                action.action_id in item.dependency_ids for item in self._actions if not item.is_noop):
            return False
        return True

    def _new_action(self, effect, kind, *, deleted=None, dependencies=()):
        predecessor = self._last_action(effect.key)
        dependencies = _unique((*((predecessor,) if predecessor else ()), *dependencies))
        self._action_sequence += 1
        action_id = f'{self.plan_id}:a{self._action_sequence}'
        group_id = self._group_id()
        self._groups.setdefault(group_id, (self._reason, self._scope_id))
        action = ApiAction(action_id, kind, group_id, effect.key, effect.before, effect.after,
            (effect.application_id,), (effect.effect_id,), dependencies,
            tuple(getattr(effect, 'subjects', ())) or (effect.key,),
            start_member_ids=_members(effect.before), deleted_member_id=deleted,
            prerequisites=effect.prerequisites,
            recovery_capability=('form_deletion_compensation' if kind == ActionKind.FORM_CREATE else
                                 'schema_only_data_unverified' if deleted else
                                 'owned_properties_compensation' if kind == ActionKind.DATABASE_RESOURCE_PATCH else
                                 'metadata_compensation'),
            warnings=('Record-data restoration is unverified.',) if deleted else ())
        self._actions.append(action)
        return action

    def _replace_action(self, action):
        index = next(i for i, item in enumerate(self._actions) if item.action_id == action.action_id)
        self._actions[index] = action
        self._candidates[(action.key, action.kind)] = index

    def _contribute(self, action, effect, dependencies):
        return replace(action, after=effect.after,
            application_ids=_unique((*action.application_ids, effect.application_id)),
            effect_ids=(*action.effect_ids, effect.effect_id),
            dependency_ids=_unique((*action.dependency_ids, *dependencies)),
            subjects=_unique((*action.subjects, *getattr(effect, 'subjects', ()))),
            prerequisites=_unique((*action.prerequisites, *effect.prerequisites)))

    def _requirement_footprints(self, action):
        result = []
        for ref in action.prerequisites:
            origin = self._origins.get(ref)
            phase = 'after' if origin and origin.creator_action_id == action.action_id else 'before'
            result.append(Footprint(ref.key, ref.member_id, existence=True, phase=phase))
        return tuple(result)

    def _schema(self, effect):
        if self._scope_id is None:
            self.select_form(effect.key)
        elif effect.key not in self._scope_keys:
            raise PlanError('Schema effect is outside the active scope.')
        self._prepare_endpoint('schema')
        before, after = _members(effect.before), _members(effect.after)
        removed = tuple(identity for identity in before if identity not in after)
        added = tuple(identity for identity in after if identity not in before)
        if len(removed) > 1:
            raise PlanError('Persisted field deletions must be staged one field at a time.')
        action = self._candidate(effect.key, ActionKind.FORM_CREATE) or self._candidate(effect.key, ActionKind.FORM_SCHEMA_UPDATE)
        dependencies = self._dependencies(effect, action)
        if removed:
            identity = removed[0]
            ref = MemberRef(effect.key, identity)
            own_creation = (action is not None and identity in action.created_member_ids
                            and identity not in action.start_member_ids)
            required_elsewhere = any(ref in item.prerequisites for item in self._actions if not item.is_noop)
            if not (own_creation and not required_elsewhere and self._can_extend(action, effect, dependencies)):
                self._seal('persisted_field_delete')
                dependencies = self._dependencies(effect)
                origin = self._origins.get(ref)
                if origin and origin.creator_action_id:
                    dependencies = _unique((*dependencies, origin.creator_action_id))
                action = self._new_action(effect, ActionKind.PERSISTED_FIELD_DELETE,
                                          deleted=identity, dependencies=dependencies)
                if added:
                    raise PlanError('A deletion effect cannot also create fields.')
                full = Footprint(effect.key, whole_resource=True)
                action = replace(action, read_set=(full, *self._requirement_footprints(action)),
                                 write_set=(replace(full, phase='after'),))
                self._replace_action(action)
                self._seal('after_persisted_field_delete')
                return
        if action is not None and not self._can_extend(action, effect, dependencies):
            self._seal('explicit_prerequisite_boundary')
            action = None
        if action is None:
            action = self._new_action(effect, ActionKind.FORM_CREATE if effect.creation else ActionKind.FORM_SCHEMA_UPDATE, dependencies=dependencies)
        else:
            action = self._contribute(action, effect, dependencies)
        for identity in added:
            ref = MemberRef(effect.key, identity)
            if ref in self._origins:
                raise PlanError('A removed member identity cannot be reused.')
            self._origins[ref] = MemberProvenance(ref, action.action_id, effect.application_id)
        action = replace(action, created_member_ids=_unique((*action.created_member_ids, *added)))
        full = Footprint(effect.key, whole_resource=True)
        projected_reads = tuple(f for p in action.projections for f in p.footprints)
        action = replace(action, read_set=(full, *self._requirement_footprints(action), *projected_reads),
                         write_set=(replace(full, phase='after'), *(replace(f, phase='after') for f in projected_reads)))
        self._replace_action(action)

    def _resource_patch(self, effect):
        self._prepare_endpoint('resources')
        before, after = effect.before.payload, effect.after.payload
        if not isinstance(before, DatabaseResourcesPayload) or not isinstance(after, DatabaseResourcesPayload):
            raise PlanError('Resource patches need database payloads.')
        if before.order != after.order or set(before.entries) != set(after.entries) or before.tombstones != after.tombstones:
            raise PlanError('Resource creation/deletion/reordering is outside a property patch.')
        changes = _unique(effect.changes)
        projected = {identity: dict(entry) for identity, entry in before.entries.items()}
        for identity, prop in changes:
            if identity not in projected or prop == 'id':
                raise PlanError('A patch must address properties of existing resource entries.')
            if prop in after.entries[identity]:
                projected[identity][prop] = after.entries[identity][prop]
            else:
                projected[identity].pop(prop, None)
        if DatabaseResourcesPayload(projected, before.order, before.tombstones) != after:
            raise PlanError('Resource changes exceed the declared property footprint.')
        action = self._candidate(effect.key, ActionKind.DATABASE_RESOURCE_PATCH)
        dependencies = self._dependencies(effect, action)
        if action is not None and not self._can_extend(action, effect, dependencies):
            self._seal('explicit_prerequisite_boundary')
            action = None
        if action is None:
            action = self._new_action(effect, ActionKind.DATABASE_RESOURCE_PATCH, dependencies=dependencies)
        else:
            action = self._contribute(action, effect, dependencies)
        touched = _unique((*((patch.member_id, patch.property_name) for patch in action.patches), *changes))
        patches = []
        for identity, prop in touched:
            old, new = action.before.payload.entries[identity], action.after.payload.entries[identity]
            old_present, new_present = prop in old, prop in new
            if old_present == new_present and (not old_present or
                    freeze({'value': old[prop]}) == freeze({'value': new[prop]})):
                continue
            patches.append(PropertyPatch(identity, prop, old_present, old.get(prop), new_present, new.get(prop)))
        reads = tuple(Footprint(effect.key, patch.member_id, (patch.property_name,)) for patch in patches)
        existence = tuple(Footprint(effect.key, identity, existence=True)
                          for identity in _unique(patch.member_id for patch in patches))
        action = replace(action, patches=tuple(patches),
            read_set=(*reads, *existence, *self._requirement_footprints(action)),
            write_set=tuple(replace(footprint, phase='after') for footprint in reads))
        self._replace_action(action)

    def delete_form(self, application_id, effect_id, before_db, after_db, form):
        """Discard only the current open edit, then batch adjacent database deletions."""
        candidate = deepcopy(self)
        candidate._delete_form(application_id, effect_id, before_db, after_db, form)
        self.__dict__.update(candidate.__dict__)

    def _delete_form(self, application_id, effect_id, before_db, after_db, form):
        open_action = self._candidate(form.key, ActionKind.FORM_CREATE) or self._candidate(form.key, ActionKind.FORM_SCHEMA_UPDATE)
        # An open candidate can coexist with other form candidates in an iterator.
        # Remove only its contribution and leave all other actions intact.
        if open_action is not None:
            self._actions.remove(open_action)
            self._candidates = {(a.key, a.kind): i for i, a in enumerate(self._actions)
                                if a.group_id == self._group_id() and a.group_id not in self._sealed}
            self._origins = {ref: row for ref, row in self._origins.items()
                             if row.creator_action_id != open_action.action_id}
            for i, action in enumerate(self._actions):
                self._actions[i] = replace(action, dependency_ids=tuple(d for d in action.dependency_ids if d != open_action.action_id))
            entries = dict(before_db.payload.entries)
            for projection in open_action.projections:
                if projection.before.key == before_db.key:
                    old = projection.before.payload.entries.get(form.key.identity)
                    if old is None:
                        entries.pop(form.key.identity, None)
                    else:
                        entries[form.key.identity] = old
            before_db = replace(before_db, payload=DatabaseResourcesPayload(entries))
            form = open_action.before
            if open_action.kind == ActionKind.FORM_CREATE:
                self._projected[before_db.key] = after_db
                self._projected[form.key] = replace(form, lifecycle=Lifecycle.DELETED)
                self.record_application(application_id)
                self._effect_order += (effect_id,)
                return
        if self._actions and self._actions[-1].kind == ActionKind.FORM_DELETE and self._actions[-1].key != before_db.key:
            self._seal('deletion_database_boundary')
        self._prepare_endpoint('form_deletion')
        action = self._candidate(before_db.key, ActionKind.FORM_DELETE)
        effect = ResourcePatchEffect(effect_id, application_id, before_db.key, before_db, after_db, (), subjects=(form.key,))
        if action is None:
            action = self._new_action(effect, ActionKind.FORM_DELETE)
        else:
            action = self._contribute(action, effect, ())
        forms = (*action.deleted_forms, form)
        # Include descendants already represented in the database tree.
        removed = tuple(i for i in action.before.payload.entries if i not in after_db.payload.entries)
        footprints = tuple(Footprint(before_db.key, i, whole_resource=True) for i in removed)
        action = replace(action, after=after_db, deleted_forms=forms, recovery_capability='database_version_revert',
                         read_set=(*footprints, *(Footprint(state.key, whole_resource=True) for state in forms)), write_set=tuple(replace(f, phase='after') for f in footprints))
        self._replace_action(action)
        self._projected[before_db.key] = after_db
        self._projected[form.key] = replace(form, lifecycle=Lifecycle.DELETED)
        self.record_application(application_id)
        self._effect_order += (effect_id,)

    def exit_deleted_scope(self):
        self._scope_id, self._scope_keys = None, ()

    def finish_deletions(self):
        if self._endpoint == 'form_deletion':
            self._seal('after_form_deletion')
