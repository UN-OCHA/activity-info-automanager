"""Command registration metadata, independent of command implementations."""

import ast
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..command_creator import CommandCreatorDefinition


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    parser: Callable[[str, ast.Call], Any]
    creator: CommandCreatorDefinition
