"""Authoritative function catalog. Add explicit definition imports here."""

from ..catalogs import index_definitions
from .models import FunctionDefinition
from .replace import REPLACE
from .regex_replace import REGEX_REPLACE


FUNCTIONS: tuple[FunctionDefinition, ...] = (REPLACE, REGEX_REPLACE)
FUNCTIONS_BY_NAME = index_definitions(FUNCTIONS)
