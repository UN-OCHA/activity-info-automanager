"""Deferred identities for code-first schema commands."""

from copy import deepcopy
import re
import secrets


PENDING_FIELD_PREFIX = 'pending-field:'


def field_code_error(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', value) is None:
        return 'Field code must start with a letter and contain only letters, numbers, and underscores.'
    return None


def pending_field_id(code: str) -> str:
    return f'{PENDING_FIELD_PREFIX}{code}'


def prepare_schema_for_put(schema: dict, referenced_schemas: dict | None = None) -> dict:
    """Materialize pending field IDs on a copy, once per actual write attempt.

    Commands and previews never allocate CUIDs. Existing IDs are preserved.
    The caller retains this exact target for post-write verification.
    """
    result = deepcopy(schema)
    elements = result.get('elements')
    if not isinstance(elements, list):
        raise ValueError('The target schema must contain an elements list.')
    used = set()
    for element in elements:
        if isinstance(element, dict):
            used.add(element.get('id'))
            parameters = element.get('typeParameters', {})
            for collection in ('values', 'lookupConfigs'):
                for item in parameters.get(collection, []):
                    used.add(item.get('id'))
    replacements = {}

    def allocate():
        while True:
            identity = 'c' + secrets.token_hex(12)
            if identity not in used:
                used.add(identity)
                return identity

    for element in elements:
        if not isinstance(element, dict):
            continue
        identity = element.get('id')
        code = element.get('code')
        if isinstance(identity, str) and identity.startswith(PENDING_FIELD_PREFIX):
            if field_code_error(code) or identity != pending_field_id(code):
                raise ValueError('A pending field identity must match its field code.')
            metadata = element.get('metadata')
            if metadata:
                reserved = {'ADDED_BY': '_addedBy', 'LAST_EDITED_BY': '_lastEditedBy'}.get(metadata)
                if not reserved or reserved in used:
                    raise ValueError('Metadata field ID is invalid or already present.')
                if element.get('type') != 'reference' or element.get('readOnly') is not True:
                    raise ValueError('Metadata requires a read-only user reference.')
                used.add(reserved)
                element['id'] = reserved
            else:
                element['id'] = allocate()
            replacements[identity] = element['id']
        parameters = element.get('typeParameters', {})
        for collection, prefix in (('values', 'pending-option:'), ('lookupConfigs', 'pending-lookup:')):
            for item in parameters.get(collection, []):
                token = item.get('id')
                if isinstance(token, str) and token.startswith(prefix):
                    if not token.startswith(f'{prefix}{code}:') or token in replacements:
                        raise ValueError('Invalid or duplicate pending option/lookup identity.')
                    item['id'] = allocate()
                    replacements[token] = item['id']

    by_code = {}
    for element in elements:
        if isinstance(element, dict) and element.get('code'):
            by_code.setdefault(element['code'], []).append(element)
    for element in elements:
        if not isinstance(element, dict):
            continue
        parameters = element.get('typeParameters', {})
        reverse_field = parameters.get('fieldId')
        if element.get('type') == 'reversereference' and isinstance(reverse_field, str) and reverse_field.startswith('field-code:'):
            code = reverse_field.removeprefix('field-code:')
            form_id = parameters.get('formId')
            external = result if form_id == result.get('id') else (referenced_schemas or {}).get(form_id)
            candidates = [field for field in (external or {}).get('elements', [])
                          if isinstance(field, dict) and field.get('code') == code]
            if len(candidates) != 1:
                raise ValueError(f'Reverse-reference field code {code!r} must resolve to exactly one field.')
            candidate = candidates[0]
            ranges = candidate.get('typeParameters', {}).get('range', [])
            if candidate.get('type') not in ('reference', 'multiselectreference') or not any(
                entry.get('formId') == result.get('id') for entry in ranges
            ):
                raise ValueError('The reverse-reference field must point back to this form.')
            parameters['fieldId'] = candidate['id']
        config = element.get('typeParameters', {}).get('translationConfig')
        if config and isinstance(config.get('fieldId'), str) and config['fieldId'].startswith('field-code:'):
            code = config['fieldId'].removeprefix('field-code:')
            matches = by_code.get(code, [])
            if len(matches) != 1:
                raise ValueError(f'Translation field code {code!r} must resolve to exactly one field.')
            other = matches[0]
            if other is element or other.get('type') != element.get('type'):
                raise ValueError('Translation fields must be distinct fields of the same type.')
            config['fieldId'] = other['id']
        if element.get('type') == 'enumerated' and 'defaultValue' in element:
            value = element['defaultValue']
            if isinstance(value, list):
                element['defaultValue'] = [replacements.get(item, item) for item in value]
            elif isinstance(value, str):
                element['defaultValue'] = replacements.get(value, value)
    return result


async def prepare_schema_for_write(service, schema):
    """Load only schemas needed to resolve code-first reverse references."""
    referenced_schemas = {}
    for element in schema.get('elements', []):
        parameters = element.get('typeParameters', {})
        field_id = parameters.get('fieldId')
        form_id = parameters.get('formId')
        if (element.get('type') == 'reversereference' and isinstance(field_id, str)
                and field_id.startswith('field-code:') and form_id != schema.get('id')
                and form_id not in referenced_schemas):
            referenced_schemas[form_id] = await service.get_form_schema(form_id)
    return prepare_schema_for_put(schema, referenced_schemas)
