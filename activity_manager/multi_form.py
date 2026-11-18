"""Ordered form visits, with local schemas taking precedence over remote context."""

from copy import deepcopy

from .schema_names import resolve_form_names, readable_schema
from .schema_identity import prepare_schema_for_put, schema_identities


class FormWorkspace:
    def __init__(self, initial):
        self.reserved = {}
        self.issues = {}
        self.opschema = [deepcopy(initial)] if initial is not None else []
        self.originals = {initial.get('id'): deepcopy(initial)} if initial is not None else {}

    @property
    def latest(self):
        return {schema.get('id'): schema for schema in self.opschema}

    def update(self, schema):
        self.opschema[-1] = deepcopy(schema)
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
        self.opschema.append(deepcopy(schema))
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
