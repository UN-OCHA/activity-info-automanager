"""Authoritative command catalog; tuple order also controls the creator menu."""

from ..catalogs import index_definitions
from .add_field import COMMAND as ADD_FIELD
from .add_modify_field import COMMAND as ADD_MODIFY_FIELD
from .delete_field import COMMAND as DELETE_FIELD
from .modify_field import COMMAND as MODIFY_FIELD
from .move_field import COMMAND as MOVE_FIELD
from .set_variable import COMMAND as SET_VARIABLE
from .with_form import COMMAND as WITH_FORM


from .modify_form import COMMAND as MODIFY_FORM
from .move_form import COMMAND as MOVE_FORM

from .add_form import COMMAND as ADD_FORM
from .add_modify_form import COMMAND as ADD_MODIFY_FORM

from .delete_form import COMMAND as DELETE_FORM

COMMANDS = (MOVE_FIELD, DELETE_FIELD, ADD_FIELD, MODIFY_FIELD, ADD_MODIFY_FIELD, SET_VARIABLE,
            WITH_FORM, MODIFY_FORM, MOVE_FORM, ADD_FORM, ADD_MODIFY_FORM, DELETE_FORM)
COMMANDS_BY_NAME = index_definitions(COMMANDS)
COMMAND_CREATORS = tuple(definition.creator for definition in COMMANDS)
