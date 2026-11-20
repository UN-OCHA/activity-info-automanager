"""Human-readable execution summaries shared by both execution modes."""



from .planning import ActionKind

from .resource_state import FormPayload





def recovery_console_message(record):

    if record.status == 'reconciled':

        result = {

            'intended': 'The requested changes were applied and verified.',

            'unchanged': 'No changes were made.',

            'divergent': 'The current state differs from the requested changes.',

            'unknown': 'The result could not be verified.',

        }.get(record.message, record.message)

        return f'Rollback: current state checked. {result}'

    status = {

        'attempted': 'Request sent; awaiting verification.',

        'verified_restored': 'Restored and verified.',

        'blocked': 'Cannot safely restore the previous state.',

        'unknown': 'Restoration could not be verified.',

        'failed': 'The previous state was not restored.',

    }.get(record.status, record.status)

    return f'Rollback: {status}' + (f' {record.message}' if record.message else '')





def action_summaries(plan, database_names=None):
    """Describe reviewed API writes using names, without command positions or IDs."""
    database_names = database_names or {}
    forms = dict(plan.related_schemas)
    for reviewed in plan.actions:
        action = reviewed.action
        for state in (action.before, action.after, *action.deleted_forms):
            if isinstance(state.payload, FormPayload):
                forms[state.key.identity] = state.payload.schema
    labels = {}
    for identity, schema in forms.items():
        labels.setdefault(schema.get('label'), set()).add(identity)

    def form_name(state):
        schema = state.payload.schema
        label = schema.get('label') or 'Unnamed form'
        if len(labels.get(label, ())) > 1:
            database = database_names.get(schema.get('databaseId'))
            if database:
                return f'{database} / {label}'
        return label

    lines = []
    for reviewed in plan.actions:
        action = reviewed.action
        if action.kind == ActionKind.FORM_CREATE:
            operation, scope = 'Create form', form_name(action.after)
        elif action.kind == ActionKind.FORM_SCHEMA_UPDATE:
            operation, scope = 'Update form structure', form_name(action.before)
            if action.before.payload.schema.get('label') != action.after.payload.schema.get('label'):
                scope += ' → ' + form_name(action.after)
        elif action.kind == ActionKind.PERSISTED_FIELD_DELETE:
            field = next((field for field in action.before.payload.schema['elements']
                          if field['id'] == action.deleted_member_id), {})
            operation = 'Delete field'
            scope = form_name(action.before) + ' → ' + (field.get('label') or field.get('code') or 'Unnamed field')
        elif action.kind == ActionKind.FORM_DELETE:
            operation = 'Delete forms'
            scope = ', '.join(form_name(state) for state in action.deleted_forms)
        else:
            operation = 'Move forms'
            resources = action.before.payload.entries
            def resource_name(identity):
                if identity == action.key.identity:
                    return database_names.get(identity) or 'Database root'
                parts, visited = [], set()
                while identity in resources and identity not in visited:
                    visited.add(identity)
                    resource = resources[identity]
                    parts.append(resource.get('label') or 'Unnamed resource')
                    identity = resource.get('parentId')
                return ' / '.join(reversed(parts)) or 'Unnamed resource'
            scope = '; '.join(f'{resource_name(identity)} → {resource_name(parent)}'
                              for identity, parent in reviewed.destinations)
        lines.append(f'{operation} — {scope}')
    return tuple(lines)


def confirmation_summary(plan, database_names=None):
    lines = ['Execute these updates?', '',
             'These API actions will run in order. Each action may combine several commands.', '']
    summaries = action_summaries(plan, database_names)
    lines.extend(f'{index}. {summary}' for index, summary in enumerate(summaries, 1))
    if not summaries:
        lines.append('No updates required.')
    return '\n'.join(lines)

