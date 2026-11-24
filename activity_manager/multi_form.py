"""Canonical form workspace, with detached views and local-over-remote selection."""

from copy import deepcopy

from .command_execution import Diagnostic
from .resource_state import ApplicationState, FormPayload, Lifecycle, LoadStatus, ResourceKey

from .name_lookup import require_named_id
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
        self.loaded_form_trees = set()
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

    async def switch(self, form, service, related=None):
        database_id = self.state.active_database_id
        try:
            names = await self.form_directory(service, database_id)
            identity = require_named_id(form, names, kind='Form')
        except ValueError as exc:
            raise FormSelectionError(str(exc)) from exc
        if related is None:
            schema = await self.select(identity, service)
        else:
            schema, warnings = await self.select_with_tree(identity, service, related)
        if schema.get('databaseId') != database_id:
            raise FormSelectionError('The selected form is outside the active database.')
        return schema if related is None else (schema, warnings)

    async def form_directory(self, service, database_id):
        if not database_id:
            raise FormSelectionError('An active database or a database-qualified form name is required.')
        names = dict(await service.get_form_names(database_id))
        names.update({identity: schema.get('label') or identity
                      for identity, schema in self.latest.items()
                      if schema.get('databaseId') == database_id})
        return {identity: name for identity, name in names.items()
                if not ((state := self.state.resources.get(ResourceKey.form(identity)))
                        and state.lifecycle == Lifecycle.DELETED)}

    async def select(self, identity, service, *, remote_schema=None):
        """Select a frozen scope member by identity, loading it once."""
        local = self.state.resources.get(ResourceKey.form(identity))
        if local is not None and local.lifecycle == Lifecycle.DELETED:
            raise FormSelectionError('Form deleted. Command skipped.')
        schema = local.payload.to_dict() if local is not None and local.load_status == LoadStatus.LOADED else None
        if schema is None:
            schema = remote_schema if remote_schema is not None else await service.get_form_schema(identity)
            self._validate_schema(identity, schema)
            self.state.load_form(schema, affected=True)
        self.state.activate(self.key_for(schema))
        return deepcopy(schema)

    @staticmethod
    def _validate_schema(identity, schema):
        if (not isinstance(schema, dict) or schema.get('id') != identity
                or not isinstance(schema.get('elements'), list) or not schema.get('databaseId')):
            raise ValueError('The selected form has an invalid schema or no database ID.')

    async def read_form_tree(self, identity, service):
        """Read ahead without changing the active form or registering baselines."""
        state = self.state.resources.get(ResourceKey.form(identity))
        if identity in self.loaded_form_trees or (state and state.lifecycle in (Lifecycle.NEW, Lifecycle.DELETED)):
            return None, None
        try:
            tree = await service.get_form_tree(identity)
            if not isinstance(tree, dict) or identity not in tree:
                raise ValueError('The form tree did not contain the selected form schema.')
            self._validate_schema(identity, tree[identity])
            return tree, None
        except AttributeError:
            # Schema-only services do not provide related-form reads.
            return None, None
        except Exception as exc:
            return None, exc

    async def select_with_tree(self, identity, service, related, *, tree_read=None):
        """Select from the tree when available, keeping local revisions authoritative."""
        tree, error = tree_read if tree_read is not None else await self.read_form_tree(identity, service)
        schema = await self.select(identity, service, remote_schema=tree[identity] if tree is not None else None)
        related.update(self.latest)
        if error is not None:
            return schema, (Diagnostic('warning', f'Form {schema.get("label") or identity!r}: '
                                       f'related schemas could not be loaded. {error}',
                                       code='form_tree_unavailable'),)
        if tree is not None:
            for related_id, data in tree.items():
                state = self.state.resources.get(ResourceKey.form(related_id))
                if state is None or state.lifecycle != Lifecycle.DELETED:
                    related.setdefault(related_id, data)
            self.loaded_form_trees.add(identity)
        return schema, ()
