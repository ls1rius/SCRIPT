"""N-only role/value decoding constraints, aligned with H's existing guards.

This does not infer missing roles or relabel rejected model outputs. It narrows
generation to pairs that can pass the inherited lexical role-binding check.
"""
from copy import deepcopy
import re

from . import generalized_lazy_memory as parent
from . import semantic_memory as binding
from .schemas import enum, record

VERSION = parent.VERSION + ':N-required-role-review-v2'
EXTRACT = 'n_extract_fields'
BATCH_OPERATION = 'n_extract_batch'
EXTRACT_PROMPT = '\n'.join(line for line in parent.EXTRACT_PROMPT.splitlines()
    if 'actions=[' not in line and 'ONE roles array' not in line) + '''
N action format: roles is an OBJECT, not an array. Every requested role has
a required key. Review each key against the entire source clause. Copy its
literal value when explicitly bound to this action; use null only if absent
or the binding is uncertain. Do not silently skip a requested role.
Put the patient and all supported requested roles of the same action in one
record. A preposition is not a new action. Null is not evidence of absence.
C wipes the red book with a cloth, requested actions.base and actions.instrument:
actions=[{"actor":"C","verb":"wipes","roles":{"patient":"red book","instrument":"cloth"}}].
Preserve explicit prepositional details when their action roles are requested.
Choose the role using the source connector, not an imagined goal: on/in/at
does not by itself establish a destination; to/into/onto can support destination.
For an explicit on phrase use the supported surface or location role as appropriate.
Hands, fingers and arms used in an action are body parts, not instruments.
Do not omit a source phrase merely because one role label is unavailable.
These syntax constraints do not prove semantic completeness; retain uncertainty
when the source does not establish the binding. Never invent missing details.
'''


def __getattr__(name):
    # Reuse H's storage, source-only requests and acceptance without changing H.
    return getattr(parent, name)


def role_value_supported(role, value, text):
    position = binding.span(value, text)
    if position is None:
        return False
    connectors = binding.PREPOSITIONS.get(role)
    if connectors and not re.search(
            r'\b(?:' + '|'.join(re.escape(c) for c in connectors) +
            r')\s+(?:(?:a|an|the|his|her|their|its|my|your|both)\s+)?$',
            text[:position[0]], re.I):
        return False
    if role == 'instrument' and re.search(r'\b(hands?|fingers?|arms?|feet|foot|legs?)\b', value, re.I):
        return False
    return True


def output_schema(operation, inputs):
    if operation == parent.PLAN:
        return parent.output_schema(operation, inputs)
    if operation != EXTRACT:
        raise ValueError('Unknown N source extraction operation')
    schema = parent.output_schema(parent.EXTRACT, inputs)
    item = schema['properties']['actions']['items']
    text = parent.joint.clean_unit(inputs['unit'])['text']
    for branch in item.get('anyOf', [item]):
        # H's explicit/unknown-actor alternatives share the nested roles schema.
        # Detach it before narrowing either branch.
        roles = deepcopy(branch['properties']['roles'])
        props = roles['items']['properties']
        slots = {}
        for role in sorted(props['role']['enum'], key=lambda r: (r != 'patient', r)):
            values = [v for v in props['value']['enum'] if role_value_supported(role, v, text)]
            slots[role] = {'anyOf': [enum(values), {'type': 'null'}]} if values else {'type': 'null'}
        branch['properties']['roles'] = record(slots)
    return schema


def accept(unit, report):
    # Request-specific required keys are checked by serial/batch schema validation.
    # Direct acceptance also checks literal values, allowed roles and action binding.
    fields = {f for f in parent.FIELDS if not f.startswith('actions.')}
    fields.add('actions.base')
    for action in report['actions']:
        if not isinstance(action['roles'], dict):
            raise ValueError('N requires explicit role review objects')
        fields.update('actions.'+r for r in action['roles'] if r != 'patient')
    parent.validate_output(report, output_schema(EXTRACT, parent.extraction_inputs(unit, fields)))
    converted = deepcopy(report)
    for action in converted['actions']:
        action['roles'] = [dict(role=k, value=v) for k, v in action['roles'].items() if v is not None]
    return parent.accept(unit, converted)


# Batch inspection otherwise uses the inherited list-shaped wire protocol.
inspect_accept = accept
