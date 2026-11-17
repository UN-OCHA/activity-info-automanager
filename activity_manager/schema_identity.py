"""Deferred identities for code-first schema commands."""

from copy import deepcopy
import re
import secrets

from .schema_names import FORMULA_KEYS, readable_schema, schema_with_ids, resolve_reference_defaults, resolve_form_names


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
    # Pending references in preview formulas must be mapped back through codes
    # before the pending field IDs themselves are materialized.
    result = readable_schema(schema, referenced_schemas, formulas_only=True)
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
        if isinstance(reverse_field, str):
            parameters['fieldId'] = replacements.get(reverse_field, reverse_field)
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
        if config and isinstance(config.get('fieldId'), str):
            config['fieldId'] = replacements.get(config['fieldId'], config['fieldId'])
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
    result = schema_with_ids(result, referenced_schemas)
    for field in result['elements']:
        if field.get('type') != 'enumerated' or field.get('defaultValue') is None:
            continue
        options = (field.get('typeParameters') or {}).get('values') or []
        identities = {option.get('id') for option in options}
        value = field['defaultValue']
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, str) or item not in identities:
                raise ValueError(f'Enumerated default {item!r} for field '
                                 f'{field.get("code", field.get("id"))!r} must resolve to exactly one option ID.')
    return result


async def prepare_schema_for_write(service, schema, related_schemas=None, reference_defaults=None):
    """Resolve names on a copy only after the user confirms execution."""
    load_root_tree = related_schemas is None
    related_schemas = dict(related_schemas or {})
    schema = await resolve_form_names(service, schema, related_schemas)
    def has_formula(node):
        if isinstance(node, dict):
            return any((key in FORMULA_KEYS and isinstance(value, str)) or has_formula(value)
                       for key, value in node.items())
        return isinstance(node, list) and any(has_formula(value) for value in node)

    needed = set()
    reverse_needed = set()
    formulas = has_formula(schema)
    for element in schema.get('elements', []):
        parameters = element.get('typeParameters', {})
        field_id = parameters.get('fieldId')
        form_id = parameters.get('formId')
        if (element.get('type') == 'reversereference' and isinstance(field_id, str)
                and field_id.startswith('field-code:') and form_id != schema.get('id')):
            needed.add(form_id)
            reverse_needed.add(form_id)
        for entry in parameters.get('range') or []:
            reference = entry.get('formId')
            defaults = element.get('defaultValue', [])
            values = defaults if isinstance(defaults, list) else [defaults]
            unresolved = any(type(value) in (str, int, float) and not (
                isinstance(value, str) and value.startswith(f'{reference}:')) for value in values)
            if reference and reference != schema.get('id') and (formulas or unresolved):
                needed.add(reference)
    referenced_schemas = dict(related_schemas or {})
    if needed and load_root_tree and hasattr(service, 'get_form_tree'):
        referenced_schemas.update(await service.get_form_tree(schema['id']))
    for form_id in needed - referenced_schemas.keys():
        # Tree entries without schemas represent inaccessible/deleted forms.
        # Leave their formulas/defaults unresolved; strict reverse references
        # still require the schema to validate that they point back correctly.
        if form_id in reverse_needed or not hasattr(service, 'get_form_tree'):
            referenced_schemas[form_id] = await service.get_form_schema(form_id)
    result = prepare_schema_for_put(schema, referenced_schemas)
    return await resolve_reference_defaults(service, result, referenced_schemas, reference_defaults, strict=True)
