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


def _as_form_schema(schema: dict, description: str) -> FormSchema:
    try:
        return FormSchema.from_dict(schema)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'The {description} form schema is invalid.') from exc
