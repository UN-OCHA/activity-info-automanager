"""Resolve names consistently across database and form selectors."""


def find_named_id(selector, names, *, kind):
    """Find a unique case-insensitive name, then try the exact ID."""
    folded = selector.casefold()
    matches = [identity for identity, name in names.items()
               if isinstance(name, str) and name.casefold() == folded]
    if len(matches) > 1:
        raise ValueError(f'{kind} name {selector!r} matches multiple {kind.lower()}s; use an ID instead.')
    if matches:
        return matches[0]
    return selector if selector in names else None


def require_named_id(selector, names, *, kind):
    identity = find_named_id(selector, names, kind=kind)
    if identity is None:
        raise ValueError(f'{kind} name {selector!r} must resolve to exactly one {kind.lower()}; use an ID instead.')
    return identity
