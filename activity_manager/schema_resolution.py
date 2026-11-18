"""Asynchronous preview resolution, retaining all failures in a reviewable schema."""

from copy import deepcopy
import json
import asyncio

from .command_execution import Diagnostic, ExecutionResult

from .schema_names import (
    resolve_form_names, schema_with_ids, resolve_reference_defaults,
    readable_schema, load_default_record_labels,
    unresolved_formula_references, referenced_form_ids,
)


class CachedResolutionService:
    """Cache successful reads for one loaded source, never for execution."""

    def __init__(self, service, cache):
        self.service = service
        self.cache = cache

    async def _read(self, method, *args):
        key = (method, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key not in self.cache:
            self.cache[key] = await getattr(self.service, method)(*args)
        return deepcopy(self.cache[key])

    async def get_form_names(self, database_id):
        return await self._read('get_form_names', database_id)

    async def get_database_names(self):
        return await self._read('get_database_names')

    async def get_form_tree(self, form_id):
        return await self._read('get_form_tree', form_id)

    async def get_form_schema(self, form_id):
        return await self._read('get_form_schema', form_id)

    async def query_rows(self, query):
        return await self._read('query_rows', query)


async def resolve_command_schema(service, schema, related, database_names, reference_defaults, cache, *, source_schema=None):
    diagnostics = []
    result = deepcopy(schema)
    try:
        return await _resolve_command_schema(service, result, related, database_names, reference_defaults,
                                             cache, diagnostics, referenced_form_ids(source_schema))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        diagnostics.append(Diagnostic('error', f'{type(exc).__name__}: {exc}', code='unexpected_resolution_error'))
        return ExecutionResult(result, tuple(diagnostics))


async def _resolve_command_schema(service, result, related, database_names, reference_defaults, cache, issues, known_form_ids):
    """Resolve without generating identities and accumulate ordered diagnostics."""
    service = CachedResolutionService(service, cache)

    def issue(path, message):
        issues.append(Diagnostic('error', message, tuple(path)))

    # Resolve each form location separately so one failure does not hide others.
    for index, field in enumerate(result.get('elements', [])):
        params = field.get('typeParameters') or {}
        locations = [(entry, 'formId', ('elements', index, 'typeParameters', 'range', n, 'formId'))
                     for n, entry in enumerate(params.get('range') or []) if 'formId' in entry]
        if field.get('type') in ('subform', 'reversereference') and 'formId' in params:
            locations.append((params, 'formId', ('elements', index, 'typeParameters', 'formId')))
        for container, key, path in locations:
            raw = container[key]
            one = {**result, 'elements': [{'type': 'reference', 'typeParameters': {'range': [{'formId': raw}]}}]}
            try:
                resolved = await resolve_form_names(service, one, related,
                    known_form_ids=known_form_ids, load_related=False)
                identity = resolved['elements'][0]['typeParameters']['range'][0]['formId']
                container[key] = identity
                if identity not in related and identity != result.get('id'):
                    try:
                        tree = await service.get_form_tree(identity)
                        for form_id, schema in tree.items():
                            related.setdefault(form_id, schema)
                    except ValueError as exc:
                        issues.append(Diagnostic('warning',
                            f'Form ID {identity!r}: related schema/name unavailable; keeping the ID. {exc}',
                            tuple(path), code='unavailable_form_context'))
            except ValueError as exc:
                issue(path, f'Form {raw!r}: {exc}')

    canonical = schema_with_ids(result, related)
    result.clear()
    result.update(canonical)
    for path, token, form_id in unresolved_formula_references(result, related):
        issues.append(Diagnostic('warning', f'Unresolved formula reference {token!r}'
            + (f' in form {form_id!r}.' if form_id else ' (form context unavailable).'), path))
    for index, field in enumerate(result.get('elements', [])):
        params = field.get('typeParameters') or {}
        label = field.get('code') or field.get('id')
        prefix = ('elements', index)
        if field.get('type') == 'enumerated' and field.get('defaultValue') is not None:
            identities = {option.get('id') for option in params.get('values') or []}
            values = field['defaultValue']
            for n, value in enumerate(values if isinstance(values, list) else [values]):
                if not isinstance(value, str) or value not in identities:
                    path = prefix + ('defaultValue',) + ((n,) if isinstance(values, list) else ())
                    issue(path, f'Field {label!r}: option default {value!r} could not resolve to exactly one option.')
        if field.get('type') in ('reference', 'multiselectreference') and 'defaultValue' in field:
            values = field['defaultValue']
            converted = []
            for n, value in enumerate(values if isinstance(values, list) else [values]):
                one_field = {**field, 'defaultValue': value}
                try:
                    one = await resolve_reference_defaults(service, {**result, 'elements': [one_field]},
                                                           related, reference_defaults, strict=True)
                    converted.append(one['elements'][0]['defaultValue'])
                except ValueError as exc:
                    converted.append(value)
                    path = prefix + ('defaultValue',) + ((n,) if isinstance(values, list) else ())
                    issue(path, str(exc))
            field['defaultValue'] = converted if isinstance(values, list) else converted[0]

        translation = params.get('translationConfig') or {}
        reverse = field.get('type') == 'reversereference'
        config = params if reverse else translation
        token = config.get('fieldId')
        if isinstance(token, str) and token.startswith('field-code:'):
            code = token.removeprefix('field-code:')
            context = related.get(params.get('formId')) if reverse and params.get('formId') != result.get('id') else result
            matches = [f for f in (context or {}).get('elements', []) if f.get('code') == code]
            valid = len(matches) == 1
            if valid and reverse:
                valid = matches[0].get('type') in ('reference', 'multiselectreference') and any(
                    entry.get('formId') == result.get('id') for entry in (matches[0].get('typeParameters') or {}).get('range') or [])
            elif valid:
                valid = matches[0].get('id') != field.get('id') and matches[0].get('type') == field.get('type')
            if valid:
                config['fieldId'] = matches[0]['id']
            else:
                path = prefix + ('typeParameters',) + (() if reverse else ('translationConfig',)) + ('fieldId',)
                config['fieldId'] = code
                issue(path, f'Field {label!r}: field code {code!r} could not resolve to a valid reference.')

    label_cache = {}
    forms = {**related, result.get('id'): result}
    # Reuse successful input labels without querying the same record again.
    for (form_id, _, label), identity in reference_defaults.items():
        if isinstance(identity, str) and identity.startswith(f'{form_id}:'):
            label_field = (forms.get(form_id) or {}).get('recordLabelFieldId')
            if label_field:
                label_cache[(form_id, identity.removeprefix(f'{form_id}:'), label_field)] = str(label)
    labels = await load_default_record_labels(service, result, related, label_cache)
    names = readable_schema(result, related, database_names, record_labels=labels)
    return ExecutionResult(result, tuple(issues), names)
