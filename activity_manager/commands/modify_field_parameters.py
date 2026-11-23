"""ModifyField uses shared controls without AddField creation defaults."""

from dataclasses import replace
from functools import partial

from ..command_creator import ParameterDefinition as P
from ..command_parser import parse_function_input
from ..field_parameters import (
    FIELD_ARGUMENTS, FIELD_PARAMETERS, applies, for_types, parameter_applies, variant_required,
)


def optional_edit(parameter):
    return replace(parameter, blank_action='unset', fixed_when=None,
                   children=tuple(optional_edit(child) for child in parameter.children))


TARGET_PARAMETERS = {'field_code', 'field_type', 'field_variant', 'referenced_form'}
_EXCLUDED = {'field_code', 'barcode', 'metadata',
             'reverse_reference_field', 'attachment_cardinality'}


def configure(parameter):
    parameter = optional_edit(parameter)
    key = parameter.key
    if key == 'field_type':
        return replace(parameter, required=True)
    if key == 'field_variant':
        return replace(parameter, required_when=variant_required)
    if key == 'referenced_form':
        return replace(parameter, label='Match referenced form',
                       visible_when=for_types('reference', 'multiselectreference', 'subform', 'reversereference'),
                       help='Optional match by form ID, local name, or Database/Form.')
    if key in ('key', 'unique'):
        return replace(parameter, visible_when=lambda values: parameter_applies(key, values)
                       and not for_types('serial')(values))
    if key == 'required_condition':
        return replace(parameter, visible_when=applies('required'))
    if key in ('translation_field', 'translation_language'):
        return replace(parameter, visible_when=applies('translation'))
    if key == 'option_list':
        return replace(parameter, children=tuple(
            replace(child, children=tuple(replace(item, required=True) for item in child.children))
            if child.control == 'repeat' else child
            for child in parameter.children if child.key != 'cardinality'
        ))
    if key == 'lookup_fields':
        return replace(parameter, children=tuple(replace(child, required=True) for child in parameter.children))
    return parameter


MODIFY_FIELD_PARAMETERS = (
    P('field_code', 'Existing field code or label', 'combobox', required=True, option_source='field_codes'),
    *(configure(parameter) for parameter in FIELD_PARAMETERS[:2]),
    P('new_code', 'New field code', 'text', blank_action='unset',
      visible_when=applies('field_code')),
    *(configure(parameter) for parameter in FIELD_PARAMETERS[3:] if parameter.key not in _EXCLUDED),
)

# Only top-level editable text inputs interpret helper calls. Target selectors,
# nested children, and the shared AddField definitions keep their literal behavior.
MODIFY_FIELD_PARAMETERS = tuple(
    replace(parameter, parse_text=partial(parse_function_input, command_name='ModifyField',
        parameter='newCode' if parameter.key == 'new_code' else FIELD_ARGUMENTS[parameter.key]))
    if parameter.key not in TARGET_PARAMETERS
    and parameter.control in ('text', 'combobox') and parameter.value_type == 'string'
    else parameter
    for parameter in MODIFY_FIELD_PARAMETERS
)
