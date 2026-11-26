"""Schema equivalence shared by handlers and rendering tests; no endpoint writes."""
from dataclasses import dataclass
import json
from activity_info_api_client.models.form_schema import FormSchema
from .schema_names import readable_schema, schema_with_ids


@dataclass(frozen=True)
class SchemaComparison:
    matches: bool
    summary: str


def compare_schemas(expected: FormSchema, actual: FormSchema, related_schemas=None,
                    reference_defaults=None) -> SchemaComparison:
    """Describe differences by JSON path, ignoring the root schemaVersion.

    Arrays are compared by position because field and option order is significant.
    Missing properties match null/false, including nested object defaults.
    Formulas and defaults use canonical identities. Reference label mappings
    must be resolved by the async caller; unknown labels remain distinct.
    An empty summary means the schemas match.
    """
    def normalized(model):
        canonical = schema_with_ids(model.to_dict(), related_schemas, reference_defaults)
        return readable_schema(canonical, related_schemas, formulas_only=True)

    expected_data = normalized(expected)
    actual_data = normalized(actual)
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


def schemas_match(left: FormSchema, right: FormSchema, related_schemas=None, reference_defaults=None) -> bool:
    """Boolean convenience wrapper for schema comparison."""
    return compare_schemas(left, right, related_schemas, reference_defaults).matches


def unexpected_schema_changes(before, requested, observed):
    """Find changes a partial schema update cannot explain.

    Use exact values for recovery attribution. Nested lists are atomic, and
    an existing field must remain present in an ordinary update.
    """
    missing = object()
    paths = []

    def same(left, right):
        if isinstance(left, (dict, list)) and isinstance(right, type(left)):
            return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
        return left == right and isinstance(left, bool) == isinstance(right, bool)

    def visit(old, target, actual, path):
        if (isinstance(actual, dict) and (isinstance(old, dict) or isinstance(target, dict))
                and all(isinstance(value, dict) or value is missing for value in (old, target))):
            old_members = {} if old is missing else old
            target_members = {} if target is missing else target
            for key in sorted(old_members.keys() | target_members.keys() | actual.keys()):
                visit(old_members.get(key, missing), target_members.get(key, missing), actual.get(key, missing),
                      f'{path}[{json.dumps(key)}]')
        elif not (same(old, actual) or same(target, actual)):
            paths.append(path)

    def field_index(schema):
        return {field['id']: field for field in schema['elements']}

    try:
        old_fields, target_fields, actual_fields = map(field_index, (before, requested, observed))
    except (KeyError, TypeError):
        return ('$["elements"] (invalid field identities)',)
    if any(len(index) != len(schema['elements']) for index, schema in
           zip((old_fields, target_fields, actual_fields), (before, requested, observed))):
        return ('$["elements"] (duplicate field identities)',)

    ignored = {'schemaVersion', 'elements'}
    visit(*({k: v for k, v in schema.items() if k not in ignored}
            for schema in (before, requested, observed)), '$')
    for field_id in sorted(old_fields.keys() | target_fields.keys() | actual_fields.keys()):
        path = f'$["elements"][id={json.dumps(field_id)}]'
        old = old_fields.get(field_id, missing)
        target = target_fields.get(field_id, missing)
        actual = actual_fields.get(field_id, missing)
        if old is not missing and actual is missing:
            paths.append(path)
        elif (old is missing and target is not missing and actual is not missing
              and not same(actual.get('type', missing), target.get('type', missing))):
            paths.append(f'{path}["type"]')
        else:
            visit(old, target, actual, path)

    existing = old_fields.keys() & actual_fields.keys()
    actual_order = tuple(key for key in actual_fields if key in existing)
    allowed_orders = {tuple(key for key in index if key in existing)
                      for index in (old_fields, target_fields)}
    if actual_order not in allowed_orders:
        paths.append('$["elements"] (existing field order)')

    # Each observed addition must have the requested position relative to the
    # fields that are present. Pending additions can be skipped.
    def predecessors(index):
        seen = set()
        for key in index:
            if key in actual_fields:
                if key not in old_fields:
                    yield key, seen.copy()
                seen.add(key)

    target_positions = dict(predecessors(target_fields))
    for key, preceding in predecessors(actual_fields):
        if key in target_positions and preceding != target_positions[key]:
            paths.append(f'$["elements"][id={json.dumps(key)}] (field position)')
    return tuple(paths)


def _as_form_schema(schema: dict, description: str) -> FormSchema:
    try:
        return FormSchema.from_dict(schema)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'The {description} form schema is invalid.') from exc
