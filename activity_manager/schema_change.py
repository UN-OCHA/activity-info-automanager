from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from activity_info_api_client.models.form_schema import FormSchema


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


def schemas_match(left: FormSchema, right: FormSchema) -> bool:
    """Compare complete schemas, ignoring only the server-managed version."""
    left_data = left.to_dict()
    right_data = right.to_dict()
    left_data.pop('schemaVersion', None)
    right_data.pop('schemaVersion', None)
    return left_data == right_data


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
    choose_recovery: Callable[[], Awaitable[RecoveryChoice]],
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

    if schemas_match(actual_model, target_model):
        messages.append('Form schema successfully changed and verified.')
        return SchemaChangeOutcome('success', actual_schema, tuple(messages))

    messages.append('Form schema application did not fully succeed.')
    choice = await choose_recovery()
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

    if rollback_error is None and schemas_match(after_rollback_model, rollback_model):
        messages.append('Rollback successfully applied and verified.')
        return SchemaChangeOutcome('rolled_back', after_rollback, tuple(messages))

    if not schemas_match(after_rollback_model, rollback_model):
        messages.append('Rollback failed verification: the actual schema does not match the rollback schema.')
    else:
        messages.append('Rollback schema was restored, but the rollback request reported an error.')
    return SchemaChangeOutcome('rollback_failed', after_rollback, tuple(messages))
