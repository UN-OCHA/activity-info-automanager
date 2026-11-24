"""Resolve selectors against the active form's fields."""


def find_field_index(selector, elements, *, label_filter=None, exact_code=False):
    """Find a unique match by code, label, or ID in that order."""
    if not isinstance(selector, str):
        return None
    properties = ('code',) if exact_code else ('code', 'label', 'id')
    folded = selector.casefold()
    for property_name in properties:
        matches = []
        for index, field in enumerate(elements):
            value = field.get(property_name) if isinstance(field, dict) else None
            if not isinstance(value, str):
                continue
            matches_value = value == selector if exact_code or property_name == 'id' else value.casefold() == folded
            if matches_value and (property_name != 'label' or label_filter is None or label_filter(field)):
                matches.append(index)
        if len(matches) > 1:
            raise ValueError(f'Ambiguous field {property_name} {selector!r}.')
        if matches:
            return matches[0]
    return None
