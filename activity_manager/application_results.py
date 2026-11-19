"""Immutable application results and the temporary legacy UI boundary."""

from dataclasses import dataclass, field, replace
from uuid import uuid4

from .immutable import FrozenMap, freeze, thaw
from .resource_state import ResourceKey, ResourceKind, ResourceState
from .schema_names import readable_schema, unresolved_formula_references


def display_payload(state):
    if state.key.kind == ResourceKind.FORM_SCHEMA:
        return state.payload.to_dict()
    return {'id': 'database:' + state.key.identity, 'label': 'Database resources (' + state.key.identity + ')',
            'databaseId': state.key.identity, 'resources': state.payload.to_list()}


@dataclass(frozen=True)
class AuthoredCommand:
    authored_id: str
    order: int
    definition: str
    syntax: str
    parameters: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self):
        object.__setattr__(self, 'parameters', freeze(self.parameters))


@dataclass(frozen=True)
class ActualCommand:
    application_id: str
    authored_id: str
    authored_index: int
    position: int
    parameters: FrozenMap | None
    syntax: str | None
    targets: tuple[ResourceKey, ...] = ()
    context_path: tuple[str, ...] = ()
    member_ids: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'parameters', freeze(self.parameters))
        object.__setattr__(self, 'targets', tuple(self.targets))
        object.__setattr__(self, 'context_path', tuple(self.context_path))
        object.__setattr__(self, 'member_ids', tuple(self.member_ids))


@dataclass(frozen=True)
class DiagnosticAttachment:
    candidate: FrozenMap
    diagnostics: tuple = ()
    names_schema: FrozenMap | None = None

    def __post_init__(self):
        object.__setattr__(self, 'candidate', freeze(self.candidate))
        object.__setattr__(self, 'names_schema', freeze(self.names_schema))
        object.__setattr__(self, 'diagnostics', tuple(self.diagnostics))


@dataclass(frozen=True)
class DisplayResource:
    state: ResourceState
    names: FrozenMap | None = None
    annotations: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, 'names', freeze(self.names))
        object.__setattr__(self, 'annotations', tuple(self.annotations))

    @property
    def key(self):
        return self.state.key


@dataclass(frozen=True)
class DisplayState:
    resources: tuple[DisplayResource, ...] = ()
    related_schemas: FrozenMap = field(default_factory=FrozenMap)
    database_names: FrozenMap = field(default_factory=FrozenMap)
    record_labels: FrozenMap = field(default_factory=FrozenMap)
    preferred_key: ResourceKey | None = None
    generation: int = 0
    revision: int = 0
    snapshot_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        object.__setattr__(self, 'resources', tuple(self.resources))
        for name in ('related_schemas', 'database_names', 'record_labels'):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        keys = self.affected_keys
        if len(keys) != len(set(keys)) or (self.preferred_key is not None and self.preferred_key not in keys):
            raise ValueError('Display resources must be unique and contain the selected key.')

    @property
    def affected_keys(self):
        return tuple(resource.key for resource in self.resources)

    def legacy_forms(self):
        """Fresh dictionaries for NiceGUI; edits cannot feed back into the snapshot."""
        return {item.key.identity: item.state.payload.to_dict() for item in self.resources
                if item.key.kind == ResourceKind.FORM_SCHEMA and item.state.payload is not None}

    def legacy_tabs(self):
        return {(('database:' if item.key.kind == ResourceKind.DATABASE_RESOURCES else '') + item.key.identity):
                display_payload(item.state) for item in self.resources
                if item.state.payload is not None}

    def legacy_metadata(self):
        return {(('database:' if item.key.kind == ResourceKind.DATABASE_RESOURCES else '') + item.key.identity):
                (thaw(item.names), item.annotations) for item in self.resources
                if item.state.payload is not None}

    def enrich(self, labels):
        # Existing labels are pinned. Only the owner may publish this replacement.
        merged = {**thaw(freeze(labels)), **thaw(self.record_labels)}
        related, databases = thaw(self.related_schemas), thaw(self.database_names)
        resources = tuple(replace(item, names=freeze(readable_schema(
            item.state.payload.to_dict(), related, databases, record_labels=merged)))
            if item.key.kind == ResourceKind.FORM_SCHEMA and item.state.payload is not None
            else item for item in self.resources)
        return replace(self, resources=resources, record_labels=freeze(merged),
                       revision=self.revision + 1, snapshot_id=uuid4().hex)


@dataclass(frozen=True)
class ApplicationResult:
    command: ActualCommand
    status: str
    message: str
    display_state: DisplayState | None
    diagnostics: tuple = ()
    effect_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    rejected_candidate: DiagnosticAttachment | None = None

    def __post_init__(self):
        for name in ('diagnostics', 'effect_ids', 'action_ids'):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.status == 'not_executed' and self.display_state is not None:
            raise ValueError('An unexecuted application has no post-state.')


@dataclass(frozen=True)
class AuthoredCommandResult:
    command: AuthoredCommand
    applications: tuple[ApplicationResult, ...]
    status: str
    message: str
    display_state: DisplayState | None
    diagnostics: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, 'applications', tuple(self.applications))
        object.__setattr__(self, 'diagnostics', tuple(self.diagnostics))


class DisplaySnapshotBuilder:
    """Capture all tab metadata now, instead of scanning command history later."""

    def __init__(self, generation):
        self.generation = generation
        self._labels = {}

    def capture(self, state, related, databases, issues, *, active=None, labels=None):
        from .command_execution import Diagnostic
        if labels:
            self._labels.update({key: value for key, value in thaw(freeze(labels)).items()
                                 if value != key[1]})
        context = {**related, **state.forms}
        resources = []
        for key in state.affected_keys:
            resource = state.resources[key]
            annotations = tuple(issues.get(key.identity, ()))
            rendered = None
            if key.kind == ResourceKind.FORM_SCHEMA and resource.payload is not None:
                schema = resource.payload.to_dict()
                paths = {issue.path for issue in annotations}
                annotations += tuple(Diagnostic('warning', f'Unresolved formula reference {token!r}.',
                    path, 'unresolved_formula_reference')
                    for path, token, _ in unresolved_formula_references(schema, context) if path not in paths)
                rendered = readable_schema(schema, context, databases, record_labels=self._labels)
            resources.append(DisplayResource(resource, rendered, annotations))
        preferred = active if active in state.affected_keys else next(iter(state.affected_keys), None)
        return DisplayState(tuple(resources), context, databases, self._labels, preferred, self.generation)


def adapt_authored_result(authored, execution, generation, *, plan=None):
    """Keep legacy cards while publishing separate authored and actual results."""
    if execution.iterations:
        leaves = execution.iterations
    elif execution.field_iterations or execution.form_iterations or execution.schema is None:
        leaves = ()
    else:
        leaves = (execution,)
    applications = []
    for position, leaf in enumerate(leaves):
        schema = leaf.schema
        target = (ResourceKey.form(schema['id']) if schema and schema.get('id') else
                  leaf.display_state.preferred_key if leaf.display_state else None)
        targets = leaf.target_keys or ((target,) if target else ())
        context = leaf.application_context or (
            ((schema.get('label') or schema.get('id') or 'Initial form'),) if schema else ())
        if leaf.field_code and (not context or context[-1] != leaf.field_code):
            context += (leaf.field_code,)
        if schema and schema.get('databaseId'):
            context = (schema['databaseId'],) + context
        members = ((leaf.target.field_id,) if leaf.target and leaf.target.field_id else leaf.target_member_ids)
        command = ActualCommand(f'{generation}:{authored.authored_id}:{position}', authored.authored_id,
            authored.order, position, leaf.prepared_parameters, leaf.prepared_syntax, targets, context, members)
        applications.append(ApplicationResult(command, leaf.status, leaf.message,
            leaf.display_state if leaf.status != 'not_executed' else None, leaf.diagnostics,
            effect_ids=leaf.effect_ids, rejected_candidate=leaf.rejected_candidate,
            action_ids=plan.action_ids_for_effects(leaf.effect_ids) if plan is not None else ()))
    return AuthoredCommandResult(authored, tuple(applications), execution.status,
                                 execution.message, execution.display_state, execution.diagnostics)
