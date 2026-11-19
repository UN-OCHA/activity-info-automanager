"""Canonical resource revisions and successful remote-read evidence.

The mutable owner publishes immutable values. SDK objects and legacy schema visits
are boundary representations, never the source of canonical state.
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from .immutable import FrozenMap, freeze, thaw


class ResourceKind(StrEnum):
    FORM_SCHEMA = 'form_schema'
    DATABASE_RESOURCES = 'database_resources'


class LoadStatus(StrEnum):
    UNLOADED = 'unloaded'
    LOADED = 'loaded'
    UNAVAILABLE = 'unavailable'


class Lifecycle(StrEnum):
    EXISTING = 'existing'
    NEW = 'new'
    DELETED = 'deleted'


class Existence(StrEnum):
    PRESENT = 'present'
    ABSENT = 'absent'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class ResourceKey:
    kind: ResourceKind
    identity: str

    def __post_init__(self):
        object.__setattr__(self, 'kind', ResourceKind(self.kind))
        if not isinstance(self.identity, str) or not self.identity:
            raise ValueError('A resource key requires a nonempty identity.')

    @classmethod
    def form(cls, identity):
        return cls(ResourceKind.FORM_SCHEMA, identity)

    @classmethod
    def database(cls, identity):
        return cls(ResourceKind.DATABASE_RESOURCES, identity)


@dataclass(frozen=True)
class FormPayload:
    schema: FrozenMap

    def __post_init__(self):
        schema = freeze(self.schema)
        if not isinstance(schema, FrozenMap):
            raise TypeError('Form payload must be a schema mapping.')
        object.__setattr__(self, 'schema', schema)

    def to_dict(self):
        return thaw(self.schema)


@dataclass(frozen=True)
class DatabaseResourcesPayload:
    entries: FrozenMap
    order: tuple[str, ...] = ()
    tombstones: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self):
        entries = freeze(self.entries)
        order = tuple(self.order) or tuple(entries)
        if len(set(order)) != len(order) or set(order) != set(entries):
            raise ValueError('Resource order must contain every entry exactly once.')
        if any(entry.get('id') != identity for identity, entry in entries.items()):
            raise ValueError('Resource entries must be keyed by their identity.')
        object.__setattr__(self, 'entries', entries)
        object.__setattr__(self, 'order', order)
        object.__setattr__(self, 'tombstones', freeze(self.tombstones))

    def to_list(self):
        return [thaw(self.entries[identity]) for identity in self.order]


@dataclass(frozen=True)
class ResourceState:
    key: ResourceKey
    payload: FormPayload | DatabaseResourcesPayload | None = None
    revision: int = 0
    load_status: LoadStatus = LoadStatus.UNLOADED
    lifecycle: Lifecycle = Lifecycle.EXISTING
    provenance: tuple[str, ...] = ()
    server_version: object = None

    def __post_init__(self):
        object.__setattr__(self, 'load_status', LoadStatus(self.load_status))
        object.__setattr__(self, 'lifecycle', Lifecycle(self.lifecycle))
        object.__setattr__(self, 'provenance', tuple(self.provenance))
        object.__setattr__(self, 'server_version', freeze(self.server_version))
        if self.revision < 0:
            raise ValueError('A resource revision cannot be negative.')
        if (self.payload is not None) != (self.load_status == LoadStatus.LOADED):
            raise ValueError('Only loaded resources have payloads.')
        if self.payload is not None:
            expected = FormPayload if self.key.kind == ResourceKind.FORM_SCHEMA else DatabaseResourcesPayload
            if not isinstance(self.payload, expected):
                raise TypeError('Payload does not match the resource kind.')
            if isinstance(self.payload, FormPayload) and self.payload.schema.get('id') != self.key.identity:
                local_draft = (self.payload.schema.get('id') is None and self.lifecycle == Lifecycle.NEW
                               and self.key.identity == 'preview:initial-form')
                if not local_draft:
                    raise ValueError('Schema identity does not match its resource key.')


@dataclass(frozen=True)
class BaselineEvidence:
    key: ResourceKey
    existence: Existence
    state: ResourceState | None = None
    version: object = None

    def __post_init__(self):
        object.__setattr__(self, 'existence', Existence(self.existence))
        object.__setattr__(self, 'version', freeze(self.version))
        if self.existence == Existence.UNKNOWN:
            raise ValueError('Unavailable reads are not successful baseline evidence.')
        if self.existence == Existence.PRESENT:
            if self.state is None or self.state.key != self.key or self.state.load_status != LoadStatus.LOADED:
                raise ValueError('Presence requires loaded state for the same resource.')
        elif self.state is not None:
            raise ValueError('Verified absence must not contain a resource payload.')


class BaselineStore:
    def __init__(self):
        self._entries = {}

    @property
    def entries(self):
        return MappingProxyType(self._entries)

    def record(self, evidence):
        previous = self._entries.get(evidence.key)
        if previous is not None and previous != evidence:
            raise ValueError('A baseline cannot be replaced during an evaluation.')
        self._entries[evidence.key] = evidence
        return evidence


class ApplicationState:
    """One evaluation's current values, with explicit display encounter order."""

    def __init__(self):
        self.baselines = BaselineStore()
        self._resources = {}
        self._affected = []
        self.active_database_id = None
        self.active_form_key = None
        self.variables = FrozenMap()
        self.scoped_variables = FrozenMap()
        self.active_scope = ()
        self.reserved_identities = FrozenMap()

    @property
    def resources(self):
        return MappingProxyType(self._resources)

    @property
    def resource_order(self):
        return tuple(self._resources)

    def note_unavailable(self, key):
        # A failed observation never replaces a successful local projection.
        return self._resources.setdefault(key, ResourceState(key, load_status=LoadStatus.UNAVAILABLE))

    @property
    def affected_keys(self):
        return tuple(self._affected)

    def mark_affected(self, key):
        if key not in self._resources:
            raise KeyError(key)
        if key not in self._affected:
            self._affected.append(key)

    def load(self, evidence, *, affected=False):
        self.baselines.record(evidence)
        if evidence.state is not None:
            previous = self._resources.get(evidence.key)
            if previous is None or previous.load_status != LoadStatus.LOADED:
                self._resources[evidence.key] = replace(evidence.state,
                    revision=previous.revision + 1 if previous else evidence.state.revision)
        if affected and evidence.key in self._resources:
            self.mark_affected(evidence.key)
        return self._resources.get(evidence.key)

    def put(self, key, payload, *, lifecycle=None, provenance=(), affected=True):
        previous = self._resources.get(key)
        value = ResourceState(key, payload, 0, LoadStatus.LOADED,
                              lifecycle or (previous.lifecycle if previous else Lifecycle.EXISTING),
                              tuple(provenance), previous.server_version if previous else None)
        if previous is not None:
            changed = (value.payload, value.lifecycle, value.load_status) != (
                previous.payload, previous.lifecycle, previous.load_status)
            value = replace(value, revision=previous.revision + int(changed),
                            provenance=previous.provenance + tuple(provenance))
        self._resources[key] = value
        if affected:
            self.mark_affected(key)
        return value

    def load_form(self, schema, *, affected=False):
        key = ResourceKey.form(schema['id'])
        if key not in self.baselines.entries:
            state = ResourceState(key, FormPayload(schema), load_status=LoadStatus.LOADED,
                                  provenance=('remote_read',), server_version=schema.get('schemaVersion'))
            self.load(BaselineEvidence(key, Existence.PRESENT, state, state.server_version))
        if affected:
            self.mark_affected(key)
        return self._resources[key]

    def put_form(self, schema, **kwargs):
        return self.put(ResourceKey.form(schema['id']), FormPayload(schema), **kwargs)

    def activate(self, key):
        resource = self._resources[key]
        if (key.kind != ResourceKind.FORM_SCHEMA or resource.lifecycle == Lifecycle.DELETED
                or resource.load_status != LoadStatus.LOADED):
            raise ValueError('Only a live form can become the active form.')
        self.active_form_key = key
        self.active_database_id = resource.payload.schema.get('databaseId')

    @property
    def forms(self):
        return {key.identity: state.payload.to_dict() for key, state in self._resources.items()
                if key.kind == ResourceKind.FORM_SCHEMA and state.load_status == LoadStatus.LOADED
                and state.lifecycle != Lifecycle.DELETED}
