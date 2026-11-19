"""Ordered form visits, with local schemas taking precedence over remote context."""

from copy import deepcopy

from .resource_state import ApplicationState, FormPayload, Lifecycle, ResourceKey

from .schema_names import resolve_form_names, readable_schema
from .schema_identity import prepare_schema_for_put, schema_identities


class FormWorkspace:
    def __init__(self, initial):
        self.state = ApplicationState()
        if initial is not None:
            if initial.get('id'):
                self.state.load_form(initial, affected=True)
            else:
                self.state.put(self.key_for(initial), FormPayload(initial), lifecycle=Lifecycle.NEW)
        self.reserved = {}
        self.issues = {}
        self.opschema = [deepcopy(initial)] if initial is not None else []
        self.originals = {initial.get('id'): deepcopy(initial)} if initial is not None else {}
        self.active_slot = 0

    @staticmethod
    def key_for(schema):
        # Offline previews keep their original schema and never establish remote presence.
        return ResourceKey.form(schema.get('id') or 'preview:initial-form')

    @property
    def active_slot(self):
        return self._active_slot

    @active_slot.setter
    def active_slot(self, value):
        self._active_slot = value
        if self.opschema:
            self.state.activate(self.key_for(self.opschema[value]))

    @property
    def active_schema(self):
        return self.latest[self.opschema[self.active_slot].get('id')] if self.opschema else None

    @property
    def latest(self):
        return {schema.get('id'): schema for schema in self.state.forms.values()}

    def update(self, schema):
        self.state.put(self.key_for(schema), FormPayload(schema),
                       lifecycle=Lifecycle.NEW if not schema.get('id') else None,
                       provenance=('local_application',))
        self.state.activate(self.key_for(schema))
        self.opschema[self.active_slot] = deepcopy(schema)
        self.reserved.setdefault(schema.get("id"), set()).update(schema_identities(schema))

    async def switch(self, form, service, current, related):
        probe = {**current, 'elements': [{'type': 'reference', 'typeParameters': {
            'range': [{'formId': form}]}}]}
        resolved = await resolve_form_names(service, probe, related, load_related=False)
        identity = resolved['elements'][0]['typeParameters']['range'][0]['formId']
        if identity == current['id']:
            return deepcopy(current)
        schema = self.latest.get(identity)
        if schema is None:
            schema = await service.get_form_schema(identity)
            if (schema.get('id') != identity or not isinstance(schema.get('elements'), list)
                    or not schema.get('databaseId')):
                raise ValueError('The selected form has an invalid schema or no database ID.')
            self.originals[identity] = deepcopy(schema)
            self.state.load_form(schema, affected=True)
        self.opschema.append(deepcopy(schema))
        self.active_slot = len(self.opschema) - 1
        return deepcopy(schema)

    async def visit(self, identity, service):
        """Start a fixed slot for one member of a form iteration scope."""
        schema = self.latest.get(identity)
        if schema is None:
            schema = await service.get_form_schema(identity)
            if schema.get('id') != identity or not isinstance(schema.get('elements'), list) or not schema.get('databaseId'):
                raise ValueError('The selected form has an invalid schema or no database ID.')
            self.originals[identity] = deepcopy(schema)
            self.state.load_form(schema, affected=True)
        self.opschema.append(deepcopy(schema))
        self.active_slot = len(self.opschema) - 1
        return deepcopy(schema)


def prepare_operations(opschema, originals, related):
    """Allocate stable IDs across visits, resolving formulas at each visit's context."""
    preview = {**deepcopy(related), **deepcopy(originals)}
    materialized = deepcopy(preview)
    identities = {}
    operations = []
    for schema in opschema:
        identity = schema['id']
        preview[identity] = schema
        # Decode pending formula IDs with the historical preview context before
        # resolving codes against real IDs in previously materialized forms.
        named = readable_schema(schema, preview, formulas_only=True)
        for field in named.get('elements', []):
            params = field.get('typeParameters') or {}
            if field.get('type') == 'reversereference':
                token = params.get('fieldId')
                params['fieldId'] = identities.get((params.get('formId'), token), token)
        target = prepare_schema_for_put(named, materialized, identity_map=identities)
        materialized[identity] = target
        operations.append((target, deepcopy(materialized)))
    return operations
