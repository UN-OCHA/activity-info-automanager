"""Context-aware, non-mutating names for schema references and formulas."""

from copy import deepcopy
import json
import re


FORMULA_KEYS = {'formula', 'prefixFormula', 'defaultValueFormula',
                'relevanceCondition', 'requiredCondition', 'validationCondition'}
# Strings are consumed before identifiers so their contents never get rewritten.
TOKENS = re.compile(r'''"(?:\\.|""|[^"\\])*"|'(?:\\.|''|[^'\\])*'|\[[^\]]+\]|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[A-Za-z_][A-Za-z0-9_]*|\s+|.''', re.DOTALL)


class SubstitutedText(str):
    """JSON-compatible string with display-only (start, end, original ID) spans."""

    def __new__(cls, value, spans=()):
        instance = super().__new__(cls, value)
        instance.spans = tuple(spans)
        return instance


def substitution(value, identity):
    return SubstitutedText(value, [(0, len(value), identity)]) if value != identity else value


def join_text(parts):
    spans = []
    offset = 0
    for part in parts:
        spans.extend((start + offset, end + offset, identity)
                     for start, end, identity in getattr(part, 'spans', ()))
        offset += len(part)
    return SubstitutedText(''.join(parts), spans)


async def substitute_record_id(service, form_id, record_id, related_schemas, cache=None):
    """Resolve a record label, retaining the record ID on absent/unavailable data."""
    form = related_schemas.get(form_id) or {}
    label_field = form.get('recordLabelFieldId')
    if not label_field:
        return record_id
    key = (form_id, record_id, label_field)
    if cache is not None and key in cache:
        return cache[key]
    label = record_id
    try:
        rows = await service.query_rows({
            'rowSources': [{'rootFormId': form_id}],
            'columns': [{'id': label_field, 'formula': label_field}],
            'filter': f'_id == {json.dumps(record_id, ensure_ascii=False)}',
        })
        value = rows[0].get(label_field) if len(rows) == 1 else None
        if isinstance(value, (str, int, float, bool)) and value is not None and value != '':
            label = str(value)
    except (ValueError, TypeError, AttributeError):
        pass
    if cache is not None:
        cache[key] = label
    return label


async def load_default_record_labels(service, schema, related_schemas, cache):
    forms = dict(related_schemas)
    forms[schema.get('id')] = schema
    labels = {}
    for field in schema.get('elements', []):
        if field.get('type') not in ('reference', 'multiselectreference'):
            continue
        defaults = field.get('defaultValue', [])
        for value in defaults if isinstance(defaults, list) else [defaults]:
            if isinstance(value, str) and ':' in value:
                form_id, record_id = value.split(':', 1)
                labels[(form_id, record_id)] = await substitute_record_id(
                    service, form_id, record_id, forms, cache)
    return labels


def schemas_from_tree(tree):
    schemas = {}
    for key, value in tree.get('forms', {}).items():
        if not isinstance(value, dict):
            continue
        schema = value.get('schema', value)
        if isinstance(schema, dict) and isinstance(schema.get('elements'), list):
            schemas[key] = schema
    return schemas


def readable_schema(schema, related_schemas=None, database_names=None, *, formulas_only=False,
                    record_labels=None, _to_ids=False, _unresolved=None):
    """Return a readable copy. Unknown/ambiguous references retain their spelling.

    Related schemas are keyed by form ID; the supplied local schema overrides its
    tree entry. formulas_only supports comparison without equating form names.
    """
    forms = dict(related_schemas or {})
    forms[schema.get('id')] = schema
    result = deepcopy(schema)

    def field(form, token):
        elements = (form or {}).get('elements', [])
        matches = [f for f in elements if isinstance(f, dict) and f.get('id') == token]
        if not matches:
            matches = [f for f in elements if isinstance(f, dict) and f.get('code') == token]
        return matches[0] if len(matches) == 1 else None

    def target(element):
        params = (element or {}).get('typeParameters') or {}
        ranges = params.get('range') or []
        return forms.get(ranges[0].get('formId')) if ranges else forms.get(params.get('formId'))

    def formula(text, context, path):
        tokens = TOKENS.findall(text)
        current = context
        previous = None
        dotted = False
        for i, token in enumerate(tokens):
            if token.isspace():
                continue
            if token == '.':
                current = target(previous)
                dotted = True
                continue
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*|\[[^\]]+\]', token):
                following = next((t for t in tokens[i + 1:] if not t.isspace()), None)
                keyword = not token.startswith('[') and token.upper() in {'TRUE', 'FALSE', 'AND', 'OR', 'NOT'}
                function = not token.startswith('[') and following == '('
                found = None if function or keyword else field(current if dotted else context, token.strip('[]'))
                if not found and not function and not keyword and token.lower() not in {'_id', '_label', '_parent', '_formid'}:
                    if _unresolved is not None:
                        scope = current if dotted else context
                        failure = (path, token.strip('[]'), (scope or {}).get('id'))
                        if failure not in _unresolved:
                            _unresolved.append(failure)
                if found and (_to_ids or found.get('code')):
                    code = found.get('id') if _to_ids else found['code']
                    if not isinstance(code, str):
                        previous = found
                        dotted = False
                        continue
                    bare = re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', code) and code.upper() not in {
                        'TRUE', 'FALSE', 'AND', 'OR', 'NOT'}
                    replacement = code if bare else f'[{code}]'
                    tokens[i] = (substitution(replacement, found['id'])
                                 if not _to_ids and token.strip('[]') == found.get('id') else replacement)
                previous = found
            else:
                previous = None
            dotted = False
        return ''.join(tokens) if _to_ids else join_text(tokens)

    def code(form, identity):
        found = field(form, identity)
        return substitution((found or {}).get('code') or identity, identity)

    def form_name(identity):
        other = forms.get(identity)
        if not other or not other.get('label'):
            return identity
        name = other['label']
        db = other.get('databaseId')
        if db and db != schema.get('databaseId'):
            db_name = (database_names or {}).get(db)
            return substitution(f'{db_name}/{name}', identity) if db_name else identity
        return substitution(name, identity)

    def visit(node, context, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in FORMULA_KEYS and isinstance(value, str):
                    node[key] = formula(value, context, path + (key,))
                else:
                    visit(value, context, path + (key,))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, context, path + (index,))

    for index, element in enumerate(result.get('elements', [])):
        params = element.get('typeParameters') or {}
        lookups = params.pop('lookupConfigs', None)
        visit(element, schema, ('elements', index))
        if lookups is not None:
            visit(lookups, target(element), ('elements', index, 'typeParameters', 'lookupConfigs'))
            params['lookupConfigs'] = lookups
        if formulas_only:
            continue
        def default_name(value):
            if not isinstance(value, str):
                return value
            if element.get('type') == 'enumerated':
                if _to_ids:
                    options = params.get('values', [])
                    if any(option.get('id') == value for option in options):
                        return value
                    matches = [option for option in options if option.get('label') == value]
                    return matches[0]['id'] if len(matches) == 1 else value
                matches = [option for option in params.get('values', []) if option.get('id') == value]
                if len(matches) == 1 and matches[0].get('label'):
                    return substitution(matches[0]['label'], value)
            if not _to_ids and element.get('type') in ('reference', 'multiselectreference') and ':' in value:
                form_id, record_id = value.split(':', 1)
                label = (record_labels or {}).get((form_id, record_id), record_id)
                return join_text([form_name(form_id), ':', substitution(label, record_id)])
            return value
        if 'defaultValue' in element:
            value = element['defaultValue']
            element['defaultValue'] = [default_name(item) for item in value] if isinstance(value, list) else default_name(value)
        if _to_ids:
            continue
        translation = params.get('translationConfig') or {}
        if 'fieldId' in translation:
            translation['fieldId'] = code(schema, translation['fieldId'])
        if element.get('type') == 'reversereference' and 'fieldId' in params:
            params['fieldId'] = code(forms.get(params.get('formId')), params['fieldId'])
        for entry in params.get('range') or []:
            if 'formId' in entry:
                entry['formId'] = form_name(entry['formId'])
    return result


def unresolved_formula_references(schema, related_schemas=None):
    """Return (JSON path, reference token, form context) for unresolved tokens."""
    failures = []
    readable_schema(schema, related_schemas, formulas_only=True, _unresolved=failures)
    return failures


def schema_with_ids(schema, related_schemas=None, reference_defaults=None):
    """Resolve formula codes and enumerated labels on a copy, preserving unknowns.

    Form IDs remain canonical. Pending field/option IDs are supported for
    previews and remapped during write preparation. Functions and literals survive.
    """
    result = readable_schema(schema, related_schemas, _to_ids=True)
    for field in result.get('elements', []):
        if field.get('type') not in ('reference', 'multiselectreference') or 'defaultValue' not in field:
            continue
        ranges = (field.get('typeParameters') or {}).get('range') or []
        if not ranges:
            continue
        form_id = ranges[0].get('formId')

        def canonical(value):
            if type(value) not in (str, int, float) or (
                isinstance(value, str) and value.startswith(f'{form_id}:')
            ):
                return value
            return (reference_defaults or {}).get((form_id, type(value), value), value)

        value = field['defaultValue']
        field['defaultValue'] = [canonical(item) for item in value] if isinstance(value, list) else canonical(value)
    return result


async def substitute_record_label(service, form_id, label, related_schemas):
    """Use the first matching record; return the input unchanged if none match."""
    label_field = (related_schemas.get(form_id) or {}).get('recordLabelFieldId')
    if not label_field or type(label) not in (str, int, float):
        return label
    rows = await service.query_rows({
        'rowSources': [{'rootFormId': form_id}],
        'columns': [{'id': '_id', 'formula': '_id'}],
        'filter': f'{label_field} == {json.dumps(label, ensure_ascii=False, allow_nan=False)}',
    })
    identity = rows[0].get('_id') if rows else None
    return f'{form_id}:{identity}' if isinstance(identity, str) and identity else label


async def resolve_reference_defaults(service, schema, related_schemas, cache=None, *, strict=False):
    """Resolve scalar/multiple reference labels on a copy; strict mode rejects failures."""
    result = deepcopy(schema)
    forms = dict(related_schemas)
    forms[result.get('id')] = result
    cache = {} if cache is None else cache
    for field in result.get('elements', []):
        if field.get('type') not in ('reference', 'multiselectreference') or 'defaultValue' not in field:
            continue
        ranges = (field.get('typeParameters') or {}).get('range') or []
        if not ranges:
            if strict and field['defaultValue'] is not None:
                raise ValueError(f'Reference default for {field.get("code", field.get("id"))!r} has no referenced form.')
            continue
        form_id = ranges[0].get('formId')

        async def resolve(value):
            def fail():
                raise ValueError(f'Reference default {value!r} for field '
                                 f'{field.get("code", field.get("id"))!r} could not resolve to a record in {form_id!r}.')
            if value is None:
                return value
            if type(value) not in (str, int, float):
                if strict:
                    fail()
                return value
            if isinstance(value, str) and value.startswith(f'{form_id}:'):
                if strict and not value.removeprefix(f'{form_id}:'):
                    fail()
                return value
            key = (form_id, type(value), value)
            if key not in cache:
                cache[key] = await substitute_record_label(service, form_id, value, forms)
            if strict and not (isinstance(cache[key], str) and cache[key].startswith(f'{form_id}:')
                               and cache[key].removeprefix(f'{form_id}:')):
                fail()
            return cache[key]

        value = field['defaultValue']
        field['defaultValue'] = [await resolve(item) for item in value] if isinstance(value, list) else await resolve(value)
    return result


async def resolve_form_names(service, schema, related_schemas=None):
    """Resolve local Form and remote Database/Form names, rejecting ambiguity.

    Existing known IDs, system CUIDs and reserved user-form IDs are preserved.
    Directory reads use the service's generated-client endpoints only.
    """
    result = deepcopy(schema)
    forms = dict(related_schemas or {})
    forms[result.get('id')] = result
    directories = {}
    databases = None

    async def resolve(value):
        nonlocal databases
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'Invalid referenced form {value!r}.')
        if value in forms or value.endswith('@users'):
            return value
        # Older service implementations can still write canonical schemas.
        if not hasattr(service, 'get_form_names'):
            return value
        database_id = result.get('databaseId')
        name = value
        if '/' in value:
            database_name, name = value.split('/', 1)
            if databases is None:
                databases = await service.get_database_names()
            matches = [identity for identity, label in databases.items() if label == database_name]
            if len(matches) != 1:
                raise ValueError(f'Database name {database_name!r} must resolve to exactly one database; use an ID instead.')
            database_id = matches[0]
        if not database_id:
            raise ValueError(f'Cannot resolve referenced form {value!r} without a database ID.')
        if database_id not in directories:
            directories[database_id] = await service.get_form_names(database_id)
        directory = directories[database_id]
        if value in directory:
            return value
        matches = [identity for identity, label in directory.items() if label == name]
        if not matches and '/' not in value and re.fullmatch(r'[a-z][a-z0-9]{8,}', value):
            candidate = await service.get_form_schema(value)
            if candidate.get('id') == value:
                forms[value] = candidate
                if related_schemas is not None:
                    related_schemas[value] = candidate
                return value
        if len(matches) != 1:
            raise ValueError(f'Form name {value!r} must resolve to exactly one form; use an ID instead.')
        identity = matches[0]
        if related_schemas is not None and identity not in forms and hasattr(service, 'get_form_tree'):
            tree = await service.get_form_tree(identity)
            forms.update(tree)
            related_schemas.update(tree)
        return identity

    for field in result.get('elements', []):
        params = field.get('typeParameters') or {}
        for entry in params.get('range') or []:
            if 'formId' in entry:
                entry['formId'] = await resolve(entry['formId'])
        if field.get('type') in ('subform', 'reversereference') and 'formId' in params:
            params['formId'] = await resolve(params['formId'])
    return result
