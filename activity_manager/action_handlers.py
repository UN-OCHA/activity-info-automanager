"""Endpoint preparation and schema/resource comparisons."""

from dataclasses import dataclass
from typing import Protocol

from .endpoint_evidence import Observation, WriteEvidence
from .immutable import freeze, thaw
from .planning import ActionKind, ApiAction, PlanError, Footprint
from .resource_state import Existence, FormPayload, DatabaseResourcesPayload, ResourceState
from .schema_change import SchemaComparison, compare_schemas, _as_form_schema


@dataclass(frozen=True)
class SchemaRequest:
    form_id: str
    schema: object

    def __post_init__(self):
        object.__setattr__(self, 'schema', freeze(self.schema))


class ActionHandler(Protocol):
    async def read_state(self, action: ApiAction) -> Observation: ...
    def compare(self, expected: ResourceState, observed: Observation) -> SchemaComparison: ...
    def encode_write(self, action: ApiAction) -> SchemaRequest: ...
    async def write(self, request: SchemaRequest) -> WriteEvidence: ...
    def recovery_request(self, action: ApiAction, preimage: ResourceState) -> SchemaRequest: ...


class SchemaHandler:
    def __init__(self, service, *, related_schemas=None, reference_defaults=None):
        self.service = service
        self.related = freeze(related_schemas or {})
        self.defaults = freeze(reference_defaults or {})

    async def read_state(self, action):
        return await self.service.observe_resource(action.key)

    def compare(self, expected, observed):
        if observed.key != expected.key or observed.existence != Existence.PRESENT or observed.failure:
            return SchemaComparison(False, 'Required schema observation is unavailable or absent.')
        return compare_schemas(_as_form_schema(expected.payload.to_dict(), 'expected'),
                               _as_form_schema(observed.state.payload.to_dict(), 'observed'),
                               thaw(self.related), thaw(self.defaults))

    def encode_write(self, action):
        if action.kind not in (ActionKind.FORM_SCHEMA_UPDATE, ActionKind.PERSISTED_FIELD_DELETE):
            raise PlanError('This handler accepts schema actions only.')
        if not isinstance(action.after.payload, FormPayload):
            raise PlanError('Schema action requires a form payload.')
        schema = action.after.payload.to_dict()
        _as_form_schema(schema, 'target')
        return SchemaRequest(action.key.identity, schema)

    async def write(self, request):
        return await self.service.write_schema_evidence(request.form_id, thaw(request.schema))

    def recovery_request(self, action, preimage):
        return SchemaRequest(preimage.key.identity, preimage.payload.to_dict())


class PersistedFieldDeleteHandler(SchemaHandler):
    def encode_write(self, action):
        if action.kind != ActionKind.PERSISTED_FIELD_DELETE or not action.deleted_member_id:
            raise PlanError('A persisted deletion requires its identified field.')
        before = {field['id'] for field in action.before.payload.schema['elements']}
        after = {field['id'] for field in action.after.payload.schema['elements']}
        if before - after != {action.deleted_member_id}:
            raise PlanError('A persisted deletion must remove exactly its identified field.')
        return super().encode_write(action)


@dataclass(frozen=True)
class ResourceRequest:
    database_id: str
    schema: object

    def __post_init__(self):
        object.__setattr__(self, 'schema', freeze(self.schema))


class ResourceHandler:
    def __init__(self, service, action):
        self.service, self.action = service, action

    def compare(self, expected, observed):
        footprints = (*self.action.read_set, *(Footprint(ref.key, ref.member_id, ('type',))
                      for ref in self.action.prerequisites))
        return compare_resource_footprints(expected, observed, footprints)

    def encode_write(self, action):
        entries = {}
        for patch in action.patches:
            if patch.property_name != 'parentId' or not patch.after_present:
                raise PlanError('Resource writes support parent updates only; form labels are written through the schema endpoint.')
            entries.setdefault(patch.member_id, {'id': patch.member_id})[patch.property_name] = thaw(patch.after_value)
        return ResourceRequest(action.key.identity, {'resourceUpdates': list(entries.values())})

    async def write(self, request):
        return await self.service.write_resources_evidence(request.database_id, thaw(request.schema))

    def recovery_request(self, action, preimage):
        entries = {}
        for patch in action.patches:
            old = preimage.payload.entries[patch.member_id]
            if patch.property_name not in old and patch.property_name != 'parentId':
                raise PlanError('Absent-property compensation lacks an established clearing contract.')
            entries.setdefault(patch.member_id, {'id': patch.member_id})[patch.property_name] = thaw(
                old.get(patch.property_name, action.key.identity))
        return ResourceRequest(action.key.identity, {'resourceUpdates': list(entries.values())})


def compare_resource_footprints(expected, observed, footprints):
    """Property presence and scalar types matter; unrelated entries do not."""
    if expected.key != observed.key or observed.existence != Existence.PRESENT or observed.failure:
        return SchemaComparison(False, 'Required resource observation is unavailable or absent.')
    differences = []
    missing = object()
    for footprint in footprints:
        if footprint.key != expected.key:
            raise PlanError('Footprint belongs to another resource.')
        left, right = expected.payload.entries, observed.state.payload.entries
        if footprint.member_id is not None:
            left, right = left.get(footprint.member_id, missing), right.get(footprint.member_id, missing)
        if footprint.existence:
            matches = (left is missing) == (right is missing)
        else:
            for part in footprint.path:
                left = left.get(part, missing) if hasattr(left, 'get') else missing
                right = right.get(part, missing) if hasattr(right, 'get') else missing
            matches = type(left) is type(right) and left == right
        if not matches:
            differences.append(f'{footprint.member_id or expected.key.identity}:{".".join(footprint.path)} differs')
    return SchemaComparison(not differences, '\n'.join(differences))
