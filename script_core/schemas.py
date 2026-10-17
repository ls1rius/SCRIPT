"""Model output JSON schemas, including call-local reference allowlists."""
from copy import deepcopy

from .state import DYNAMIC, PREDICATE_ROLES

S = {'type': 'string'}
N = {'type': 'number'}
B = {'type': 'boolean'}
NULL_S = {'type': ['string', 'null']}
NULL_N = {'type': ['number', 'null']}


def short_text(maximum):
    return {'type': 'string', 'maxLength': maximum}


def enum(values):
    return {'type': 'string', 'enum': list(values)} if values else {'type': 'string', 'enum': ['__no_allowed_reference__']}


def array(item=S, maximum=32):
    # An empty reference allowlist permits [] only. The sentinel is not evidence.
    if item.get('enum') == ['__no_allowed_reference__']:
        maximum = 0
    return {'type': 'array', 'items': deepcopy(item), 'maxItems': maximum}


def record(properties, optional=()):
    return {'type': 'object', 'properties': properties, 'required': [k for k in properties if k not in optional],
            'additionalProperties': False}


def decoder_schema(schema):
    """Omit forced-empty fields: LMFE permits a first item even at maxItems=0.

    The canonical schema still validates the complete object. Omitted fields are
    restored to [] only when their allowlist makes any other value impossible.
    """
    if isinstance(schema, list):
        return [decoder_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    result = {k: decoder_schema(v) for k, v in schema.items()}
    if schema.get('type') == 'object':
        empty = {k for k, v in schema['properties'].items()
                 if v.get('type') == 'array' and v.get('maxItems') == 0}
        result['properties'] = {k: v for k, v in result['properties'].items() if k not in empty}
        result['required'] = [k for k in result['required'] if k not in empty]
    return result


def restore_empty_arrays(value, schema):
    if isinstance(value, dict) and schema.get('type') == 'object':
        for key, field in schema['properties'].items():
            if field.get('type') == 'array' and field.get('maxItems') == 0:
                value.setdefault(key, [])
            if key in value:
                restore_empty_arrays(value[key], field)
    elif isinstance(value, list) and schema.get('type') == 'array':
        for item in value:
            restore_empty_arrays(item, schema['items'])
    return value


INTERVAL = {'type': 'array', 'items': N, 'minItems': 2, 'maxItems': 2}
VARIABLES = ['actor', 'recipient', 'object', 'target_object', 'target_fabric', 'target_bag',
             'target_person', 'giver', 'receiver', 'location', 'destination', 'speaker', 'listener']


def output_schema(operation, inputs):
    if operation == 'repair':
        return output_schema(inputs['original_operation'], inputs.get('original_inputs', {}))
    frame_ids = [f['frame_id'] for f in inputs.get('frames', [])]
    occurrences = [o['id'] for o in inputs.get('occurrences', [])]
    claims = [c['id'] for c in inputs.get('claims', [])]
    requirements = [r['id'] for r in inputs.get('requirements', [])]
    bindings = [b['id'] for b in inputs.get('bindings', [])]
    entities = [e['id'] for e in inputs.get('entities', [])]
    refs = set(frame_ids+occurrences+claims+requirements+bindings+entities+inputs.get('allowed_scene_ids', []))
    variable_names = [v['name'] for v in inputs.get('variables', [])]
    roles = record({role: enum(occurrences) for role in ('actor', 'recipient', 'object', 'location', 'destination', 'speaker', 'listener')},
                   optional=('actor', 'recipient', 'object', 'location', 'destination', 'speaker', 'listener'))
    roles['properties']['content'] = S
    point_times = sorted({f['pts'] for f in inputs.get('frames', []) if 'pts' in f}
                         | {span['start'] for span in inputs.get('subtitles', []) if 'start' in span})
    claim = record({'predicate': enum(inputs.get('extraction_scope', {}).get('predicates', ['other'])),
                    'roles': roles, 'interval': INTERVAL,
                    'observed_at': {'anyOf': [{'type': 'null'}]+([{'type': 'number', 'enum': point_times}] if point_times else [])},
                    'source_type': enum(['visual', 'subtitle']), 'source_frame_ids': array(enum(frame_ids), 4),
                    'fact_type': enum(['state', 'action', 'inference']), 'description': short_text(160)})
    def typed_claims():
        # Select fact kind before sources so the decoder cannot complete an action
        # with one frame and only discover its process requirement afterwards.
        variants = []
        for kind in ('state', 'action', 'inference'):
            if kind == 'action' and len(set(frame_ids)) < 3:
                continue
            fields = {'fact_type': enum([kind]), **{k: deepcopy(v) for k, v in claim['properties'].items() if k != 'fact_type'}}
            if kind == 'state':
                predicates = [p for p in fields['predicate']['enum'] if p not in DYNAMIC]
                if not predicates:
                    continue
                fields['predicate'] = enum(predicates)
            if kind == 'action':
                fields['source_frame_ids']['minItems'] = 3
            variants.append(record(fields))
        return {'anyOf': variants}
    if operation == 'caption':
        return record({'caption': S, 'mentions': array(record({'label': S, 'description': S}), 16),
                       'event_keywords': array(), 'source_frame_ids': array(enum(frame_ids), 4), 'uncertainties': array()})
    if operation == 'summarize':
        return record({'text': short_text(inputs.get('max_chars', 160)),
                       'place': {'anyOf': [short_text(48), {'type': 'null'}]},
                       'event_keywords': array(short_text(32), 4), 'uncertainties': array(short_text(80), 2)})
    if operation == 'read_plan':
        window_ids = [w['id'] for w in inputs.get('coarse_context', {}).get('windows', [])]
        return record({'reason': short_text(96), 'checks': array(record({
            'window_id': enum(window_ids), 'goal': short_text(96), 'reason': short_text(80)
        }), inputs.get('max_reads', 3) if window_ids else 0)})
    if operation == 'supplement':
        return record({'terms': array(S, inputs.get('max_terms', 12)), 'source_frame_ids': array(enum(frame_ids), 4)})
    if operation == 'parse':
        parse_roles = record({role: enum(VARIABLES) for role in ('actor', 'recipient', 'object', 'location', 'destination', 'speaker', 'listener', 'content')},
                             optional=('actor', 'recipient', 'object', 'location', 'destination', 'speaker', 'listener', 'content'))
        requirement = record({'key': short_text(32), 'predicate': enum(PREDICATE_ROLES), 'roles': parse_roles, 'scope': INTERVAL,
                              'quantifier': enum(['exists', 'last', 'all', 'count', 'negative', 'global']), 'prerequisites': array(short_text(32), 3),
                              'acceptable_modalities': array(enum(['visual', 'subtitle', 'audio']), 3),
                              'answer_slot': {'anyOf': [short_text(40), {'type': 'null'}]}})
        variants = []
        for predicate, required_roles in PREDICATE_ROLES.items():
            variant = deepcopy(requirement)
            variant['properties']['predicate'] = enum([predicate])
            if predicate != 'other':
                variant['properties']['roles'] = record({role: enum(VARIABLES) for role in required_roles})
            variants.append(variant)
        constraints = inputs.get('repair_constraints', [])
        if constraints:
            constrained = []
            for constraint in constraints:
                variant = deepcopy(requirement)
                variant['properties']['key'] = enum([constraint['key']])
                variant['properties']['predicate'] = enum([constraint['predicate']])
                variant['properties']['roles'] = record({k: enum([v]) for k, v in constraint['roles'].items()})
                constrained.append(variant)
            # Only constrain the whole set when it is a single unambiguous attribute requirement.
            if len(constraints) == 1:
                variants = constrained
        ambiguity = record({'kind': enum(['reference', 'scope']), 'question_span': short_text(64),
                            'alternatives': dict(array(short_text(64), 2), minItems=2), 'description': short_text(80)})
        result = record({'task_type': enum(['overview', 'temporal_overview', 'local', 'count', 'exhaustive', 'last', 'absence', 'speech']),
                        'requirements': array({'anyOf': variants}, 4), 'variables': array(record({'name': enum(VARIABLES),
                       'description': short_text(48), 'constraints': array(short_text(32), 2)}), 8),
                       'ambiguities': array(ambiguity, 2), 'evidence_gaps': array(short_text(80), 2)},
                        optional=('task_type', 'evidence_gaps'))
        result['properties']['requirements']['minItems'] = 1
        if len(constraints) == 1:
            needed = list(constraints[0]['roles'].values())
            result['properties']['requirements']['maxItems'] = 1
            result['properties']['variables']['items']['properties']['name'] = enum(needed)
            result['properties']['variables']['minItems'] = len(needed)
            result['properties']['variables']['maxItems'] = len(needed)
        return result
    if operation == 'goal':
        return record({'goal': short_text(160), 'acceptance_checks': array(short_text(80), 3)})
    if operation == 'flat_plan':
        return record({'candidate_id': enum([c['id'] for c in inputs['candidates']]), 'goal': S, 'acceptance_checks': array()})
    if operation == 'observe':
        claim['properties']['roles'] = record({})
        return record({'claims': array(typed_claims(), inputs.get('max_claims', 3)), 'uncertainties': array(short_text(80), 2)})
    if operation == 'extract':
        return record({'descriptions': array(record({'occurrence_id': enum(occurrences), 'description': short_text(96),
                                                    'source_frame_ids': array(enum(frame_ids), 4)}), len(occurrences)),
                       'claims': array(typed_claims(), inputs.get('max_claims', 3)), 'missing_entity': array(record({'category': short_text(40), 'frame_id': enum(frame_ids),
                            'region': {'type': 'array', 'items': N, 'minItems': 4, 'maxItems': 4}, 'reason': short_text(64)}), 2),
                       'uncertainties': array(short_text(80), 2)})
    if operation == 'verify_claims':
        fields = {'result': enum(['supported', 'refuted', 'unresolved']),
                        'distinguishing_facts': array(short_text(96), 2),
                        'unresolved_conflicts': array(short_text(96), 2),
                        'source_frame_ids': array(enum(frame_ids), 4), 'process_visible': B}
        if inputs.get('require_complete_claim'):
            fields = {k: fields[k] for k in ('distinguishing_facts', 'unresolved_conflicts',
                                             'source_frame_ids', 'process_visible')} | {
                'whole_claim_supported': B, 'result': fields['result']}
        check = record(fields)
        return record({'checks': record({key: deepcopy(check) for key in inputs['candidates']})})
    if operation in ('verify_claim', 'verify_identity'):
        fields = {'result': enum(['same', 'different', 'unknown'] if operation == 'verify_identity' else ['supported', 'refuted', 'unresolved']),
                  'distinguishing_facts': array(), 'unresolved_conflicts': array(), 'source_frame_ids': array(enum(frame_ids), 4)}
        if operation == 'verify_identity':
            fields['basis_types'] = array(enum(['continuous_motion', 'unique_marker', 'multiple_details_with_exclusion']), 3)
        else:
            fields['process_visible'] = B
            if inputs.get('require_complete_claim'):
                fields['distinguishing_facts'] = array(short_text(96), 3)
                fields['unresolved_conflicts'] = array(short_text(96), 2)
                fields = {k: fields[k] for k in ('distinguishing_facts', 'unresolved_conflicts',
                                                 'source_frame_ids', 'process_visible')} | {
                    'whole_claim_supported': B, 'result': fields['result']}
        return record(fields)
    if operation == 'assess':
        return record({'bindings': array(record({'variable': enum(variable_names), 'occurrence_id': enum(occurrences),
                                                 'claim_ids': array(enum(claims), 4)}), min(4, len(variable_names), len(occurrences))),
                       'requirement_updates': array(record({'requirement_id': enum(requirements),
                            'result': enum(['supported', 'refuted', 'unresolved']), 'claim_ids': array(enum(claims), 4),
                            'binding_variables': array(enum(variable_names), 4)}), len(requirements)),
                       'candidate_discriminations': array(record({'hypothesis_id': enum(bindings),
                            'result': enum(['same', 'different', 'unknown']), 'claim_ids': array(enum(claims), 4)}), min(2, len(bindings))),
                       'next_leads': array(record({'scene_id': enum(inputs.get('allowed_scene_ids', [])),
                            'requirement_id': enum(requirements), 'route': enum(['summary', 'entity', 'time', 'event', 'recovery']),
                            'basis_refs': array(enum(sorted(refs)), 4)}), 2),
                       'identity_corrections': array(record({'occurrence_id': enum(occurrences), 'entity_id': enum(entities),
                            'claim_ids': array(enum(claims), 4)}), min(2, len(occurrences), len(entities))),
                       'identity_operations': array(record({'operation': enum(['merge', 'split']),
                            'source_entity_id': enum(entities),
                            'target_entity_id': {'anyOf': [enum(entities), {'type': 'null'}]},
                            'occurrence_ids': array(enum(occurrences), 4), 'claim_ids': array(enum(claims), 4)}), min(2, len(entities))),
                       'unresolved': array(short_text(80), 2)})
    if operation == 'coverage':
        return record({'visibility': enum(['clear', 'partial', 'unknown']), 'result': enum(['change_seen', 'no_change_seen', 'unknown']),
                       'min_target_short_side': NULL_N, 'task_verified': B, 'distinguishing_facts': array(),
                       'source_frame_ids': array(enum(frame_ids)), 'unresolved_conflicts': array()})
    if operation == 'gate':
        fields = {}
        if inputs.get('ambiguities'):
            fields['ambiguity_checks'] = array(record({'ambiguity_index': enum([str(i) for i in range(len(inputs['ambiguities']))]),
                'resolved': B, 'claim_ids': array(enum(claims), 4), 'reason': short_text(64)}), len(inputs['ambiguities']))
        if inputs.get('options'):
            fields.update(option_checks=record({option_id: record({
                    'reason': short_text(64), 'claim_ids': array(enum(claims), 4),
                    'basis': enum(['direct_support', 'direct_contradiction', 'not_observed', 'uncertain']),
                    'result': enum(['supported', 'refuted', 'unresolved'])}) for option_id in inputs['option_ids']}),
                selected_option={'anyOf': [enum(inputs['option_ids']), {'type': 'null'}]})
        fields.update(passed=B, unresolved=array(short_text(80), 2))
        return record(fields)
    if operation == 'answer':
        if inputs.get('answer_strategy') == 'direct':
            return record({'text': short_text(240),
                           'selected_option': enum(inputs['option_ids']) if inputs.get('answer_mode') == 'choice' else {'type': 'null'},
                           'claim_ids': array(enum(claims), 4),
                           'evidence_ids': array(enum([e['id'] for e in inputs.get('evidence', [])]), 4)})
        fields = {}
        if inputs.get('answer_mode') == 'choice' and inputs.get('options'):
            # Generate the comparison before committing to an option ID. This
            # is a decision aid, not another sufficient-evidence certificate.
            fields = {'evidence_summary': short_text(160),
                      'option_alignment': record({key: record({
                          'reason': short_text(96),
                          'claim_ids': array(enum(claims), 2),
                          'match': enum(['matches', 'shared_only', 'contradicted', 'unknown'])
                      }) for key in inputs['option_ids']})}
        return record({**fields, 'selected_option': enum(inputs.get('option_ids', [])) if inputs.get('answer_mode') == 'choice' else {'type': 'null'},
                       'text': short_text(160), 'claim_ids': array(enum(claims), 4),
                       'evidence_ids': array(enum([e['id'] for e in inputs.get('evidence', [])]), 4)})
    raise ValueError(f'No JSON schema for operation: {operation}')


def validate_output(value, schema, path='$'):
    """Validate the explicit JSON-Schema subset emitted above, including reference enums."""
    import math
    if 'anyOf' in schema:
        errors = []
        for alternative in schema['anyOf']:
            try:
                validate_output(value, alternative, path)
                return
            except ValueError as exc:
                errors.append(str(exc))
        raise ValueError(f'{path}: no allowed schema variant: {errors[0]}')
    kinds = schema.get('type', [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    checks = {'object': isinstance(value, dict), 'array': isinstance(value, list),
              'string': isinstance(value, str), 'null': value is None, 'boolean': type(value) is bool,
              'number': type(value) in (int, float) and math.isfinite(value), 'integer': type(value) is int}
    if kinds and not any(checks[k] for k in kinds):
        raise ValueError(f'{path}: expected {kinds}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path}: reference/value is outside the allowlist')
    if isinstance(value, str) and len(value) > schema.get('maxLength', float('inf')):
        raise ValueError(f'{path}: string length exceeds the schema')
    if isinstance(value, dict):
        missing = set(schema.get('required', []))-set(value)
        if missing:
            raise ValueError(f'{path}: missing fields {sorted(missing)}')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value)-set(properties):
            raise ValueError(f'{path}: unauthorized fields {sorted(set(value)-set(properties))}')
        for key, item in value.items():
            if key in properties:
                validate_output(item, properties[key], f'{path}.{key}')
    if isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', float('inf')):
            raise ValueError(f'{path}: array length is outside the schema')
        for index, item in enumerate(value):
            validate_output(item, schema.get('items', {}), f'{path}[{index}]')
