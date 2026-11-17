from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
from typing import Literal, Protocol

from activity_info_api_client.models.form_schema import FormSchema

from .schema_identity import prepare_schema_for_write


RecoveryChoice = Literal['rollback', 'leave']
SchemaChangeStatus = Literal[
    'read_error',
    'stale',
    'unchanged',
    'success',
    'partial_left',
    'rolled_back',
    'rollback_failed',
]


class FormSchemaService(Protocol):
    async def get_form_schema(self, form_id: str) -> dict: ...

    async def put_form_schema(self, form_id: str, schema: dict) -> None: ...


@dataclass(frozen=True)
class SchemaChangeOutcome:
    status: SchemaChangeStatus
    actual_schema: dict | None
    messages: tuple[str, ...]


@dataclass(frozen=True)
class SchemaComparison:
    matches: bool
    summary: str


def compare_schemas(expected: FormSchema, actual: FormSchema) -> SchemaComparison:
    """Describe differences by JSON path, ignoring the root schemaVersion.

    Arrays are compared by position because field and option order is significant.
    Missing properties match null/false, including nested object defaults.
    An empty summary means the schemas match.
    """
    expected_data = expected.to_dict()
    actual_data = actual.to_dict()
    expected_data.pop('schemaVersion', None)
    actual_data.pop('schemaVersion', None)
    missing = object()
    differences: list[str] = []

    def display(value):
        return '<missing>' if value is missing else json.dumps(value, ensure_ascii=False)

    def visit(wanted, found, path):
        if (wanted is None or wanted is False) and (found is None or found is False):
            return
        if wanted is missing or found is missing:
            present = found if wanted is missing else wanted
            if present is None or present is False:
                return
            if isinstance(present, dict):
                visit({} if wanted is missing else wanted,
                      {} if found is missing else found, path)
                return
        if isinstance(wanted, dict) and isinstance(found, dict):
            for key in sorted(wanted.keys() | found.keys()):
                visit(wanted.get(key, missing), found.get(key, missing),
                      f'{path}[{json.dumps(key, ensure_ascii=False)}]')
        elif isinstance(wanted, list) and isinstance(found, list):
            for index in range(max(len(wanted), len(found))):
                visit(wanted[index] if index < len(wanted) else missing,
                      found[index] if index < len(found) else missing,
                      f'{path}[{index}]')
        elif wanted != found or (isinstance(wanted, bool) != isinstance(found, bool)):
            differences.append(f'{path}: expected {display(wanted)}; actual {display(found)}')

    visit(expected_data, actual_data, '$')
    return SchemaComparison(not differences, '\n'.join(differences))


def schemas_match(left: FormSchema, right: FormSchema) -> bool:
    """Boolean convenience wrapper for schema comparison."""
    return compare_schemas(left, right).matches


def _as_form_schema(schema: dict, description: str) -> FormSchema:
    try:
        return FormSchema.from_dict(schema)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'The {description} form schema is invalid.') from exc


async def apply_form_schema_change(
    service: FormSchemaService,
    form_id: str,
    loaded_schema: dict,
    target_schema: dict,
    choose_recovery: Callable[[str], Awaitable[RecoveryChoice]],
) -> SchemaChangeOutcome:
    """Safely write, verify, and when requested roll back a form schema."""
    try:
        loaded_model = _as_form_schema(loaded_schema, 'loaded')
        target_model = _as_form_schema(target_schema, 'target')
        latest_schema = await service.get_form_schema(form_id)
        latest_model = _as_form_schema(latest_schema, 'latest')
    except ValueError as exc:
        return SchemaChangeOutcome('read_error', None, (str(exc),))

    if not schemas_match(latest_model, loaded_model):
        return SchemaChangeOutcome(
            'stale',
            latest_schema,
            (
                'Execution stopped: the form schema changed after it was loaded. '
                'Review the reloaded schema and try again.',
            ),
        )

    rollback_schema = latest_schema
    rollback_model = latest_model
    if schemas_match(target_model, rollback_model):
        return SchemaChangeOutcome(
            'unchanged',
            rollback_schema,
            ('no schema changes required',),
        )

    try:
        target_schema = await prepare_schema_for_write(service, target_schema)
        target_model = _as_form_schema(target_schema, 'target')
    except (ValueError, TypeError, KeyError) as exc:
        return SchemaChangeOutcome('read_error', latest_schema, (f'Could not prepare target schema: {exc}',))

    messages: list[str] = []
    try:
        await service.put_form_schema(form_id, target_schema)
    except ValueError as exc:
        messages.append(f'Form schema API error: {exc}')

    try:
        actual_schema = await service.get_form_schema(form_id)
        actual_model = _as_form_schema(actual_schema, 'actual')
    except ValueError as exc:
        messages.append(f'Could not verify the form schema: {exc}')
        return SchemaChangeOutcome('read_error', None, tuple(messages))

    if schemas_match(actual_model, rollback_model):
        messages.append('Schema not changed: the actual schema matches the rollback schema.')
        return SchemaChangeOutcome('unchanged', actual_schema, tuple(messages))

    verification = compare_schemas(target_model, actual_model)
    if verification.matches:
        messages.append('Form schema successfully changed and verified.')
        return SchemaChangeOutcome('success', actual_schema, tuple(messages))

    messages.append('Form schema application did not fully succeed.')
    messages.append(f'Differences from the expected schema:\n{verification.summary}')
    choice = await choose_recovery(verification.summary)
    if choice == 'leave':
        messages.append('User chose Leave; the partially changed schema was not rolled back.')
        return SchemaChangeOutcome('partial_left', actual_schema, tuple(messages))

    rollback_error: str | None = None
    try:
        await service.put_form_schema(form_id, rollback_schema)
    except ValueError as exc:
        rollback_error = f'Rollback API error: {exc}'
        messages.append(rollback_error)

    try:
        after_rollback = await service.get_form_schema(form_id)
        after_rollback_model = _as_form_schema(after_rollback, 'post-rollback')
    except ValueError as exc:
        messages.append(f'Could not verify the rollback: {exc}')
        return SchemaChangeOutcome('rollback_failed', actual_schema, tuple(messages))

    rollback_comparison = compare_schemas(rollback_model, after_rollback_model)
    if rollback_error is None and rollback_comparison.matches:
        messages.append('Rollback successfully applied and verified.')
        return SchemaChangeOutcome('rolled_back', after_rollback, tuple(messages))

    if not rollback_comparison.matches:
        messages.append('Rollback failed verification: the actual schema does not match the rollback schema.\n'
                        + rollback_comparison.summary)
    else:
        messages.append('Rollback schema was restored, but the rollback request reported an error.')
    return SchemaChangeOutcome('rollback_failed', after_rollback, tuple(messages))
