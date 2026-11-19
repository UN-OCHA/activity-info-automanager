"""Detached, recursively immutable values for state and presentation contracts."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
import math


@dataclass(frozen=True)
class AnnotatedText:
    value: str
    spans: tuple

    def __post_init__(self):
        spans = tuple(tuple(span) for span in self.spans)
        if any(len(span) != 3 or type(span[0]) is not int or type(span[1]) is not int
               or not isinstance(span[2], str) or not 0 <= span[0] <= span[1] <= len(self.value)
               for span in spans):
            raise ValueError('Invalid substitution annotation.')
        object.__setattr__(self, 'spans', spans)


@dataclass(frozen=True, init=False, eq=False)
class FrozenMap(Mapping):
    """A tuple-backed mapping; no mutable dictionary is exposed or retained."""

    _items: tuple

    def __init__(self, value=()):
        items = value.items() if isinstance(value, Mapping) else value
        frozen = tuple((key, freeze(item)) for key, item in items)
        if len({key for key, _ in frozen}) != len(frozen):
            raise ValueError('Duplicate immutable mapping keys.')
        if any(key is not None and not isinstance(key, (str, int, tuple)) for key, _ in frozen):
            raise TypeError('Unsupported immutable mapping key.')
        for key, _ in frozen:
            if isinstance(key, tuple):
                if any(type(part) not in (str, int) for part in key):
                    raise TypeError('Mapping key parts must be strings or integers.')
        object.__setattr__(self, '_items', frozen)

    def __getitem__(self, key):
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (key for key, _ in self._items)

    def __len__(self):
        return len(self._items)

    def __deepcopy__(self, memo):
        return self

    def __eq__(self, other):
        if not isinstance(other, FrozenMap):
            return NotImplemented
        return len(self) == len(other) and all(
            key in other and _same_value(value, other[key]) for key, value in self.items())


def _same_value(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, tuple):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


def freeze(value):
    if isinstance(value, (FrozenMap, AnnotatedText)):
        return value
    if isinstance(value, str):
        if hasattr(value, 'spans'):
            return AnnotatedText(str(value), tuple(tuple(span) for span in value.spans))
        return str(value)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    raise TypeError(f'Unsupported snapshot value: {type(value).__name__}')


def thaw(value):
    """Return fresh JSON-compatible values, retaining substitution annotations."""
    if isinstance(value, FrozenMap):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, AnnotatedText):
        from .schema_names import SubstitutedText
        return SubstitutedText(value.value, value.spans)
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def command_parameters_snapshot(command):
    return freeze(asdict(command)) if is_dataclass(command) else FrozenMap()
