"""G: E's entity memory with open state descriptions and optional unknown actors.

Property categories, event roles and relation/link categories remain those of E.
State content is source-bound text, not a fixed dimension/value vocabulary.
"""
from collections import defaultdict
from copy import deepcopy
import re

from . import joint_entity_memory as base
from .schemas import record, enum, validate_output

VERSION = 'generalized-entity-memory-G-v1'
OPERATION = 'g_joint_extract'
COLLECTIONS = base.COLLECTIONS
ROLES = base.ROLES
PROPERTIES = base.PROPERTIES
LINKS = base.LINKS
ENTITY_ROLES = base.ENTITY_ROLES
NEGATION = base.NEGATION
UNCERTAINTY = base.UNCERTAINTY
clean_unit = base.clean_unit

PROMPT = base.PROMPT.replace(
    'Actions use actor, verb, roles=[{role,value},...]. Review every role, but emit only explicitly supported roles, once each.',
    'Actions use actor, verb, roles=[{role,value},...]. Use actor=null when the executing actor is not stated; never invent an actor. '
    'Review every role, but emit only explicitly supported roles, once each.').replace(
    'All strings must copy literal spans from unit.text.',
    'All non-null strings must copy literal spans from unit.text.').replace(
    'States are explicit temporary cleanliness/wetness/integrity/open_closed/power/posture/motion/other.',
    'States use {entity, description}. Copy the explicit condition or complete condition clause as description, including its qualifiers. '
    'State descriptions are OPEN: never classify a state into a dimension or emit dimension/value. '
    'Conditions such as unreachable, offline, awaiting review, viscous or half open are valid when explicitly stated.').replace(
    'Wet/dry/damp/clean/dirty/open/closed are temporary states, never size/color attributes.',
    'Temporary conditions belong in open state descriptions, not stable size/color attributes.').replace(
    '"dimension":"wetness","value":"wet"', '"description":"wet"')
PROMPT += '''
The server is unreachable. -> {"states":[{"entity":"server","description":"unreachable"}]}
The box was moved with a crane. -> {"actions":[{"actor":null,"verb":"moved","roles":[{"role":"patient","value":"box"},{"role":"instrument","value":"crane"}]}]}
These are examples, not a vocabulary of allowed states. Keep all other seven-category and source-only requirements.
Check each category independently: an explicit entity-to-entity relation such as book on table MUST also appear in relations,
even if you also describe its condition in states. A state description never replaces an explicit action or relation.
An explicit subject of an active action must not be omitted: C draws -> actor=C; machine produces -> actor=machine.
Use actor=null only when no executing actor is stated, such as an agentless passive or an imperative.
'''


def output_schema(inputs):
    schema = base.output_schema(inputs)
    text = enum(base.literal_spans(inputs['unit']['text']))
    schema['properties']['states']['items'] = record(dict(entity=deepcopy(text), description=deepcopy(text)))
    # LMFE does not support mixed string/null enums. Keep each branch typed.
    schema['properties']['actions']['items']['properties']['actor'] = {
        'anyOf': [deepcopy(text), {'type': 'null'}]}
    return schema


def same_clause(text, values):
    # This is a limited source-binding check, not independent semantic proof.
    # Compound/unsupported clauses retain their full original at presentation.
    clauses = re.split(r'[.;!?。；！？]|\b(?:and|but|then|while)\b', text, flags=re.I)
    return any(all(base.span(value, clause) for value in values) for clause in clauses)


def state_supported(fact, text):
    if NEGATION.search(text) or UNCERTAINTY.search(text):
        return False, 'negative_or_uncertain_clause_requires_original'
    if not same_clause(text, [fact['entity'], fact['description']]):
        return False, 'state_entity_and_description_not_in_same_source_clause'
    return True, None


def actorless_supported(fact, text):
    if NEGATION.search(text) or UNCERTAINTY.search(text):
        return False, 'negative_or_uncertain_clause_requires_original'
    values = [value for key, value in fact.items() if key not in ('kind', 'source_id')]
    if not same_clause(text, values):
        return False, 'actorless_event_not_in_one_source_clause'
    if fact['verb'].lower() in ('is', 'are', 'was', 'were', 'be', 'has', 'have'):
        return False, 'copula_is_not_action'
    verb_position = base.span(fact['verb'], text)
    prefix = re.split(r'[.;!?。；！？]|\b(?:and|but|then|while)\b', text[:verb_position[0]], flags=re.I)[-1].strip()
    # An agentless passive has a patient before its auxiliary, not a stated
    # executing actor. Active clauses with a stated subject require review.
    passive = bool(re.search(r'\b(?:is|are|was|were|be|been|being|get|gets|got)\s*$', prefix, re.I)
        and not fact['verb'].lower().endswith('ing'))
    active_prefix = re.sub(r'\b(?:is|are|was|were|has|have|had)\s*$', '', prefix, flags=re.I).strip()
    active_prefix = re.sub(r'\b\w+ly\s*$', '', active_prefix, flags=re.I).strip()
    if active_prefix and not passive and re.fullmatch(r"[\w' -]+", active_prefix):
        return False, 'explicit_subject_of_active_action_must_not_be_omitted'
    for role, connectors in base.PREPOSITIONS.items():
        if role not in fact:
            continue
        position = base.span(fact[role], text)
        prefix = text[:position[0]]
        if not re.search(r'\b(?:' + '|'.join(re.escape(v) for v in connectors)
                + r')\s+(?:(?:a|an|the|his|her|their|its|my|your)\s+)?$', prefix, re.I):
            return False, 'unsupported_' + role + '_binding'
    if re.search(r'\b(hands?|fingers?|arms?|feet|foot|legs?)\b', fact.get('instrument', ''), re.I):
        return False, 'body_part_is_not_instrument'
    return True, None


def mentions(facts):
    result = {}
    for fact in facts:
        for role, label in fact.items():
            if role not in ENTITY_ROLES:
                continue
            words = list(re.finditer(r"\b[\w'-]+\b", label))
            heads = [w for w in words if w.group().casefold() not in {'a', 'an', 'the', 'his', 'her', 'their', 'its'}]
            for index, word in enumerate(heads):
                if word.group().casefold() in {'of', 'in', 'on', 'with', 'at', 'from', 'to'}:
                    heads = heads[:index]
                    break
            head = heads[-1].group() if heads else label
            kind = ('person' if label == 'C' or head.casefold() in {'man', 'woman', 'person', 'boy', 'girl', 'child'}
                else 'body_part' if role == 'body_part' else 'object')
            result[label] = dict(label=label, head=head, entity_type=kind)
    return list(result.values())


def accept(unit, report):
    text = clean_unit(unit)['text']
    validate_output(report, output_schema(dict(unit=dict(text=text))))
    inherited = deepcopy(report)
    inherited['states'] = []
    inherited['actions'] = [a for a in inherited['actions'] if a['actor'] is not None]
    accepted = base.accept(unit, inherited)
    for candidate in report['states']:
        fact = dict(kind='state', source_id=unit['id'], **candidate)
        valid, reason = state_supported(fact, text)
        if valid:
            accepted['facts'].append(fact)
        else:
            accepted['rejected'].append(dict(record=fact, reason=reason))
    for candidate in report['actions']:
        if candidate['actor'] is not None:
            continue
        roles = {r['role']: r['value'] for r in candidate['roles']}
        fact = dict(kind='action', source_id=unit['id'], verb=candidate['verb'], **roles)
        valid, reason = actorless_supported(fact, text)
        if len(roles) != len(candidate['roles']):
            valid, reason = False, 'duplicate_action_roles'
        if valid:
            accepted['facts'].append(fact)
        else:
            accepted['rejected'].append(dict(record=fact, reason=reason))
    accepted['mentions'] = mentions(accepted['facts'])
    return accepted


def complete_simple_source(unit, accepted):
    if accepted['uncertain'] or accepted['rejected'] or not accepted['facts']:
        return False
    facts = accepted['facts']
    states = [f for f in facts if f['kind'] == 'state']
    if states:
        if len(facts) != 1:
            return False
        state = states[0]
        text = clean_unit(unit)['text'].strip().rstrip('.!?')
        description = state['description'].strip().rstrip('.!?')
        if description == text:
            return True
        return re.fullmatch(r'(?:(?:The|A|An)\s+)?' + re.escape(state['entity'])
            + r'\s+(?:is|are|was|were)\s+' + re.escape(description), text, re.I) is not None
    if any(f['kind'] == 'action' and 'actor' not in f for f in facts):
        return False
    return base.complete_simple_source(unit, accepted)


def materialize(observations, source_results, max_identity_gap=8, max_state_gap=2):
    memory, _ = base.materialize(observations, source_results, max_identity_gap, max_state_gap)
    memory['schema'] = VERSION
    memory['policy'].update(state_representation='entity_description', fixed_state_dimensions=False,
        state_matching='exact_source_description; no inferred dimension or semantic synonym merging')
    audit = validate_memory(memory, observations)
    return memory, audit


def validate_memory(memory, source_observations=None):
    audit = base.validate_memory(memory, source_observations)
    states = [f for f in memory['facts'] if f['kind'] == 'state']
    for fact in states:
        if set(fact['fields']) != {'entity', 'description'} or not fact['fields']['description'].strip():
            raise ValueError('G state must contain entity/description, without dimension/value')
    audit.update(open_state_facts=len(states), actorless_action_facts=sum(
        f['kind'] == 'action' and 'actor' not in f['fields'] for f in memory['facts']))
    return audit


def render(memory):
    validate_memory(memory)
    facts = {f['id']: f for f in memory['facts']}
    entities = {e['id']: e for e in memory['entities']}
    sections = defaultdict(list)
    covered = set()
    def name(fid, role):
        fact = facts[fid]
        ref = fact['entity_refs'].get(role)
        return f'{ref}({entities[ref]["label"]})' if ref else fact['fields'].get(role, '')
    for segment in memory['state_segments']:
        fact = facts[segment['fact_id']]
        fields = fact['fields']
        entity = segment['entity_id']
        if fact['kind'] == 'relation':
            body = fields['predicate'] + ' ' + name(fact['id'], 'right')
        elif fact['kind'] == 'state':
            body = 'State: ' + fields['description']
        else:
            body = fields['property'] + '=' + fields['value']
        sections[entity].append((segment['interval'][0], base.bounds(segment['interval']) + ': ' + body))
        covered.update((i, fact['id']) for i in segment['observation_indices'])
    grouped = {}
    for event in memory['action_observations']:
        grouped.setdefault((event['anchor_entity'], event['fact_id']), []).append(event['observation_index'])
    for (anchor, fid), indices in grouped.items():
        fact = facts[fid]
        fields = fact['fields']
        times = ', '.join(base.bounds(memory['observations'][i]['sample_interval']) for i in indices)
        body = ' '.join(v for v in [name(fid, 'actor'), fields['verb']] if v)
        if 'actor' not in fields:
            body += '; actor=unknown'
        if 'patient' in fields and fact['entity_refs'].get('patient') != anchor:
            body += ' ' + name(fid, 'patient')
        roles = [role + '=' + name(fid, role) for role in ROLES if role in fields and role != 'patient']
        if roles:
            body += '; ' + '; '.join(roles)
        sections[anchor].append((memory['observations'][indices[0]]['sample_interval'][0], times + ': ' + body))
        covered.update((i, fid) for i in indices)
    for i, row in enumerate(memory['observations']):
        for fid in row['fact_ids']:
            if (i, fid) in covered:
                continue
            fact = facts[fid]
            anchor = fact['entity_refs'].get('entity', fact['entity_refs'].get('left'))
            body = fact['kind'] + ': ' + '; '.join(
                k + '=' + (name(fid, k) if k in fact['entity_refs'] else v) for k, v in fact['fields'].items())
            sections[anchor].append((row['sample_interval'][0], base.bounds(row['sample_interval']) + ': ' + body))
            covered.add((i, fid))
    lines = []
    for entity, entries in sections.items():
        if entity:
            obj = entities[entity]
            aliases = [a for a in obj['aliases'] if a != obj['label']]
            lines.append(entity + ': ' + obj['label'] + (' (also ' + ', '.join(aliases) + ')' if aliases else ''))
        for _, body in sorted(entries, key=lambda x: x[0]):
            lines.append('  ' + body if entity else body)
    if memory['residuals']:
        lines.append('Original observations:')
        for residual in memory['residuals']:
            row = memory['observations'][residual['observation_index']]
            lines.append('Frame ' + base.bounds(row['sample_interval']) + ': ' + residual['text'])
    expected = {(i, fid) for i, row in enumerate(memory['observations']) for fid in row['fact_ids']}
    if covered != expected:
        raise ValueError('G rendered view dropped a fact observation')
    return '\n'.join(lines), dict(rendered_fact_observations=len(covered), source_rows=len(memory['observations']),
        all_fact_observations_rendered=True, state_representation='entity_description')
