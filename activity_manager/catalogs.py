"""Read-only, case-insensitive indexes for explicitly declared catalogs."""

import keyword
from types import MappingProxyType


def index_definitions(definitions):
    index = {}
    for definition in definitions:
        name = definition.name
        if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
            raise ValueError(f'Invalid catalog name {name!r}.')
        key = name.casefold()
        if key in index:
            raise ValueError(f'Duplicate catalog name {name!r}.')
        index[key] = definition
    return MappingProxyType(index)
