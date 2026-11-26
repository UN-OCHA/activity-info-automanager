"""AddField's requirements and defaults applied to shared field definitions."""

from dataclasses import replace

from .move_field import MOVE_FIELD
from ..command_creator import ParameterDefinition as P

from ..field_parameters import (
    configure_field_parameters,
    for_types,
    referenced_form_visible,
    translation_enabled,
    variant_required,
    metadata_enabled,
)


ADD_FIELD_PARAMETER_OVERRIDES = {
    'field_type': {'required': True, 'default': 'FREE_TEXT'},
    'field_variant': {'required_when': variant_required, 'default': 'text'},
    'field_code': {'required': True},
    'label': {'required': True},
    'required': {'required': True, 'default': False},
    'barcode': {'visible_when': lambda values: False},
    'translation_field': {'required_when': translation_enabled},
    'translation_language': {'required_when': translation_enabled},
    'metadata': {'required_when': metadata_enabled},
    'calculation_formula': {'required_when': for_types('calculated')},
    'option_list': {'required_when': for_types('enumerated')},
    'option_list.cardinality': {'visible_when': lambda values: False},
    'option_list.presentation': {'required': True, 'default': 'automatic'},
    'option_list.values': {'required': True},
    'option_list.values.label': {'required': True},
    'referenced_form': {'required_when': referenced_form_visible},
    'reverse_reference_field': {'required_when': for_types('reversereference')},
    'lookup_fields.formula': {'required': True},
    'lookup_fields.label': {'required': True},
}

_FIELD_PARAMETERS = configure_field_parameters(ADD_FIELD_PARAMETER_OVERRIDES)
ADD_FIELD_PARAMETERS = (
    replace(MOVE_FIELD.parameters[1], required=False, blank_action='unset'),
    replace(MOVE_FIELD.parameters[2], key='before_after_field'),
    *_FIELD_PARAMETERS,
    P('manual_entry_allowed', 'Allow manual coordinate entry', 'boolean',
      default=True, visible_when=for_types('geopoint')),
)
