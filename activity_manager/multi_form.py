"""Canonical form workspace, with detached views and local-over-remote selection."""

from copy import deepcopy

from .resource_state import ApplicationState, FormPayload, Lifecycle, ResourceKey

from .schema_names import resolve_form_names
from .schema_identity import schema_identities


class FormSelectionError(ValueError):
    """A selector could not establish context; the command may be skipped."""


class FormWorkspace:
    def __init__(self, initial, initial_database_id=None):
        self.state = ApplicationState()
        self.state.active_database_id = initial_database_id
        if initial is not None:
            if initial.get('id'):
                self.state.load_form(initial, affected=True)
            else:
                self.state.put(self.key_for(initial), FormPayload(initial), lifecycle=Lifecycle.NEW)
        self.reserved = {}
        self.issues = {}
        if initial is not None:
            self.state.activate(self.key_for(initial))

    @staticmethod
    def key_for(schema):
        # Offline previews keep their original schema and never establish remote presence.
        return ResourceKey.form(schema.get('id') or 'preview:initial-form')

    @property
    def form_snapshots(self):
        """Detached display projection, never an execution schedule."""
        return list(self.latest.values())

    @property
    def baseline_forms(self):
        return {item.key.identity: item.state.payload.to_dict() for item in self.state.baselines.entries.values()
                if item.state is not None and isinstance(item.state.payload, FormPayload)}

    @property
    def active_schema(self):
        state = self.state.resources.get(self.state.active_form_key)
        return state.payload.to_dict() if state is not None else None

    @property
    def latest(self):
        return {schema.get('id'): schema for schema in self.state.forms.values()}

    def update(self, schema):
        self.state.put(self.key_for(schema), FormPayload(schema),
                       lifecycle=Lifecycle.NEW if not schema.get('id') else None,
                       provenance=('local_application',))
        self.state.activate(self.key_for(schema))
        self.reserved.setdefault(schema.get("id"), set()).update(schema_identities(schema))

    async def switch(self, form, service, current, related):
        database_id = self.state.active_database_id
        if not database_id and '/' not in form:
            raise FormSelectionError('An active database or a database-qualified form name is required.')
        probe = {**(current or {}), 'databaseId': database_id, 'elements': [{'type': 'reference', 'typeParameters': {
            'range': [{'formId': form}]}}]}
        try:
            resolved = await resolve_form_names(service, probe, related, load_related=False)
        except ValueError as exc:
            raise FormSelectionError(str(exc)) from exc
        identity = resolved['elements'][0]['typeParameters']['range'][0]['formId']
        if current is not None and identity == current.get('id'):
            return deepcopy(current)
        schema = self.latest.get(identity)
        if schema is None:
            schema = await service.get_form_schema(identity)
            if (schema.get('id') != identity or not isinstance(schema.get('elements'), list)
                    or not schema.get('databaseId')):
                raise ValueError('The selected form has an invalid schema or no database ID.')
            self.state.load_form(schema, affected=True)
        self.state.activate(self.key_for(schema))
        return deepcopy(schema)

    async def select(self, identity, service):
        """Select a frozen scope member by identity, loading it once."""
        schema = self.latest.get(identity)
        if schema is None:
            schema = await service.get_form_schema(identity)
            if schema.get('id') != identity or not isinstance(schema.get('elements'), list) or not schema.get('databaseId'):
                raise ValueError('The selected form has an invalid schema or no database ID.')
            self.state.load_form(schema, affected=True)
        self.state.activate(self.key_for(schema))
        return deepcopy(schema)
