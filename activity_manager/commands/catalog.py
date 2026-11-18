"""Authoritative command catalog; tuple order also controls the creator menu."""

from ..catalogs import index_definitions
from .add_field import COMMAND as ADD_FIELD
from .delete_field import COMMAND as DELETE_FIELD
from .modify_field import COMMAND as MODIFY_FIELD
from .move_field import COMMAND as MOVE_FIELD
from .set_variable import COMMAND as SET_VARIABLE


COMMANDS = (MOVE_FIELD, DELETE_FIELD, ADD_FIELD, MODIFY_FIELD, SET_VARIABLE)
COMMANDS_BY_NAME = index_definitions(COMMANDS)
COMMAND_CREATORS = tuple(definition.creator for definition in COMMANDS)
