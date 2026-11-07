"""L: K's source-audited evidence planning on reversible I/H memory."""
from collections import Counter
from copy import deepcopy
import json
import math

try:
    from . import evidence_planner as base, merged_lazy_memory as merged, qa_evidence_view as view
except ImportError:  # Frozen control directory.
    import evidence_planner as base
    import merged_lazy_memory as merged
    import qa_evidence_view as view

VERSION = 'I-evidence-planner-L-v1-open-state-adjacent'
POLICY = base.POLICY + (' Compact ranges combine exactly adjacent samples of the same statement; '
    'all original sample intervals remain archived and do not prove continuous activity.')
digest = base.digest
record_times = base.record_times
plan_schema = base.plan_schema
decode_plan = base.decode_plan
audit_plan = base.audit_plan
keep_all = base.keep_all
final_input = base.final_input


def fact_sentence(category, fact):
    """Keep H open-state descriptions and never invent a missing action actor."""
    if category == 'states':
        if set(fact) != {'entity', 'description'}:
            raise ValueError('L requires H open state entity/description')
        return 'A state observation for ' + fact['entity'] + ' says: ' + json.dumps(fact['description'], ensure_ascii=False) + '.'
    if category == 'actions' and 'actor' not in fact:
        templates = dict(patient='The affected object is {}', instrument='The instrument used is {}',
            material='The material used is {}', body_part='The body part involved is {}',
            surface='The working surface is {}', source_location='The starting location is {}',
            destination='The destination is {}', location='The location is {}',
            manner='The recorded manner is {}', recipient='The recipient is {}')
        if 'verb' not in fact or fact.keys() - {'verb'} - templates.keys():
            raise ValueError('Unsupported H actorless action fields')
        return ('An action with an unspecified actor is recorded: ' + json.dumps(fact['verb'], ensure_ascii=False) + '. ' +
            ' '.join(base.sentence(template.format(fact[role])) for role, template in templates.items() if role in fact)).strip()
    return view._fact_sentence(category, fact)


def build_pool(memory, original_input, parts, *, temporal_attribute_sentence=None):
    merged.validate(memory)
    description, audit = merged.render(memory)
    if description != parts(original_input)[1]:
        raise ValueError('L I archive differs from the actual I final input')
    payload = merged.restore(memory)
    pool = base.build_pool(payload, original_input, parts, fact_sentence,
                           temporal_attribute_sentence=temporal_attribute_sentence)
    for record in pool['records']:
        record['kinds'] = [kind.replace('F ', 'H ', 1) if kind.startswith('F ') else kind for kind in record['kinds']]
        for source in record['sources']:
            if source['region'] == 'F fact':
                source['region'] = 'H fact'
    pool.update(version=VERSION, parent_memory_variant='I', underlying_memory_variant='H',
        merged_archive=deepcopy(memory), merged_source_audit=audit, policy=POLICY,
        source_counts=dict(Counter(s['region'] for r in pool['records'] for s in r['sources'])))
    pool['pool_sha256'] = digest(pool['records'])
    return pool


def planner_input(question, pool, query_id):
    inputs = base.planner_input(question, pool, query_id)
    inputs['prompts'][0] = inputs['prompts'][0].replace(base.POLICY, POLICY, 1)
    return inputs


def render(pool, plan):
    _, audit = base.render(pool, plan)
    records = {r['id']: r for r in pool['records']}
    lines = [POLICY]
    def order(rid):
        return min([t[0] for t in record_times(records[rid])] or [math.inf]), int(rid[1:])
    for group in audit['groups']:
        statements, displays = [], []
        for rid in sorted(group['record_ids'], key=order):
            record = records[rid]
            original = [list(t) for t in sorted({tuple(t) for t in record_times(record)})]
            intervals = merged.adjacent_intervals(original)
            times = ', '.join('['+','.join(str(t) for t in pair)+')' for pair in intervals) or 'time unspecified'
            statements.append('At sampled intervals '+times+' ('+record['qualification']+'), '+record['text'])
            displays.append(dict(record_id=rid, original_sample_intervals=original, display_intervals=intervals))
        lines.append(' '.join(statements))
        group['interval_display'] = displays
    audit.update(version=VERSION, parent_memory_variant='I', underlying_memory_variant='H',
        interval_presentation='coalesce_exactly_adjacent_intervals_within_identical_statement')
    return '\n'.join(lines), audit


def functional_probe(parts):
    observations = [dict(caption_id=f'C{i}', frame_index=i, sample_interval=[i,i+1], evidence=e)
        for i,e in enumerate([
            dict(actions=[dict(verb='moved',patient='box',instrument='crane')]),
            dict(actions=[dict(verb='moved',patient='box',instrument='crane')]),
            dict(states=[dict(entity='server',description='unreachable')]),
            dict(original_text='The unused chair is blue.')])]
    memory = merged.materialize(dict(observations=observations))
    original = dict(head='Synthetic I source', prompts=['  Description: '+merged.render(memory)[0]+
        '\n\n###\n\n Questions: Which tool moved the box, and what state of the server is recorded?'])
    pool = build_pool(memory, original, parts)
    return dict(pool=pool, question=dict(question='Which tool moved the box, and what state of the server is recorded?',
        options=['crane and unreachable','brush and working','knife and blue','spoon and reachable','paper and warm']),
        required_ids=[r['id'] for r in pool['records'] if 'crane' in r['text'] or 'unreachable' in r['text']],
        skipped_ids=[r['id'] for r in pool['records'] if 'chair' in r['text']])
