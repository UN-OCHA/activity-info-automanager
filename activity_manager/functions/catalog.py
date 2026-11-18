"""Authoritative function catalog. Add explicit definition imports here."""

from ..catalogs import index_definitions
from .models import FunctionDefinition
from .replace import REPLACE
from .regex_replace import REGEX_REPLACE
from .append_formula import APPEND_FORMULA
from .append import APPEND


FUNCTIONS: tuple[FunctionDefinition, ...] = (REPLACE, REGEX_REPLACE, APPEND_FORMULA, APPEND)
FUNCTIONS_BY_NAME = index_definitions(FUNCTIONS)
