"""Immutable adapter evidence. HTTP success is a receipt, never verification."""

from dataclasses import dataclass
from enum import StrEnum

from .immutable import freeze
from .resource_state import Existence, ResourceState


class FailureKind(StrEnum):
    MISSING = 'missing'
    FORBIDDEN = 'forbidden'
    UNAVAILABLE = 'unavailable'
    INVALID = 'invalid'
    REJECTED = 'rejected'


@dataclass(frozen=True)
class EndpointReceipt:
    status: int
    headers: object
    content: str
    body: object = None
    version: object = None

    def __post_init__(self):
        for name in ('headers', 'body', 'version'):
            object.__setattr__(self, name, freeze(getattr(self, name)))


@dataclass(frozen=True)
class EndpointFailure:
    kind: FailureKind
    message: str
    receipt: EndpointReceipt | None = None


@dataclass(frozen=True)
class WriteEvidence:
    receipt: EndpointReceipt | None = None
    failure: EndpointFailure | None = None


@dataclass(frozen=True)
class Observation:
    key: object
    existence: Existence
    state: ResourceState | None = None
    receipt: EndpointReceipt | None = None
    failure: EndpointFailure | None = None
    related: tuple['Observation', ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'existence', Existence(self.existence))
        object.__setattr__(self, 'related', tuple(self.related))
        if self.existence == Existence.PRESENT:
            if self.state is None or self.state.key != self.key or self.failure is not None:
                raise ValueError('Presence requires valid state for the observed resource.')
        elif self.state is not None:
            raise ValueError('Only present observations contain state.')
        if self.existence == Existence.ABSENT and (
                self.failure is None or self.failure.kind != FailureKind.MISSING):
            raise ValueError('Absence requires explicit not-found evidence.')

    @property
    def complete(self):
        return ((self.existence == Existence.PRESENT and self.failure is None
                 or self.existence == Existence.ABSENT and self.failure is not None and self.failure.kind == FailureKind.MISSING)
                and all(item.complete for item in self.related))
