"""ModifyField uses shared controls without AddField creation defaults."""

from dataclasses import replace

from ..command_creator import ParameterDefinition as P
from ..field_parameters import (
    FIELD_PARAMETERS, applies, for_types, parameter_applies, variant_required,
)


def optional_edit(parameter):
    return replace(parameter, blank_action='unset', fixed_when=None,
                   children=tuple(optional_edit(child) for child in parameter.children))


_EXCLUDED = {'field_code', 'barcode', 'metadata', 'referenced_form',
             'reverse_reference_field', 'attachment_cardinality'}


def configure(parameter):
    parameter = optional_edit(parameter)
    key = parameter.key
    if key == 'field_type':
        return replace(parameter, required=True)
    if key == 'field_variant':
        return replace(parameter, required_when=variant_required)
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
      visible_when=applies('field_code'), validate=next(parameter.validate for parameter in FIELD_PARAMETERS if parameter.key == 'field_code')),
    *(configure(parameter) for parameter in FIELD_PARAMETERS[3:] if parameter.key not in _EXCLUDED),
)
