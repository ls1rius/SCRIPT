"""Question-directed partial extraction from whole-scene text, with audited deltas.

Observations are independently sourced mentions, never inferred continuous states.
This experimental path does not change scheme 3 or its exhaustive scene extractor.
"""
from copy import deepcopy
from pathlib import Path
import re

from .config import digest
from .accounting import metered_cpu
from .schemas import array, enum, record, short_text
from .state import write_json
from .summary_graph import grounded_label

VERSION = 'query-partial-v2'
FIELDS = ['structure', 'components', 'shape', 'texture', 'appearance', 'color',
          'material', 'size', 'location', 'pose', 'other']
TARGETS = FIELDS + ['state', 'action', 'relation', 'time']


def source_phrases(captions, maximum=64):
    phrases = set()
    for caption in captions:
        words = list(re.finditer(r'\w+', caption['text']))
        for i in range(len(words)):
            for j in range(i, min(i+4, len(words))):
                phrase = caption['text'][words[i].start():words[j].end()]
                if len(phrase) <= maximum:
                    phrases.add(phrase)
    return sorted(phrases)


def output_schema(operation, inputs):
    ids = [e['id'] for e in inputs.get('entities', [])]
    captions = [c['id'] for c in inputs.get('captions', [])]
    if operation == 'transition_next':
        return record(dict(sufficient={'type': 'boolean'}, reason=short_text(160),
            gap=short_text(160), action=enum(['extract', 'read_scene', 'stop']),
            scene_id=enum(inputs['allowed_scene_ids'] + ['stop']),
            caption_ids=array(enum(captions), 8), entity_ids=array(enum(ids), 4),
            focus=array(short_text(80), 3), fields=array(enum(TARGETS), 4)))
    if operation != 'transition_extract':
        raise ValueError('Unknown adaptive operation: ' + operation)
    page_size = inputs.get('page_size', 3)
    refs = ids + [f'B{i}' for i in range(page_size)]
    source_refs = array(enum(captions), 3)
    source_refs['minItems'] = 1
    requested = [f for f in inputs.get('fields', FIELDS) if f in FIELDS]
    attribute = record(dict(field=enum(requested), value=short_text(80),
                            source_caption_ids=source_refs))
    observation = record(dict(caption_id=enum(captions),
                              kind=enum(['state', 'action', 'uncertain'])))
    entity = record(dict(entity_ref=enum(['new'] + ids), label=short_text(64),
        source_caption_ids=source_refs, attributes=array(attribute, 3 if requested else 0),
        observations=array(observation, 3)))
    labels = source_phrases(inputs.get('captions', []))
    variants = []
    for ref, label in [('new', None)] + [(e['id'], e['label']) for e in inputs.get('entities', [])]:
        variant = deepcopy(entity)
        variant['properties']['entity_ref'] = enum([ref])
        variant['properties']['label'] = enum(sorted(labels) if label is None else [label])
        variants.append(variant)
    # Choose the subject first, then exclude that reference from its object set.
    # Page aliases may still resolve to the same existing entity; commit checks that too.
    relations = {'anyOf': [record(dict(subject=enum([ref]),
        object=enum([other for other in refs if other != ref]),
        predicate=enum(source_phrases(inputs.get('captions', []), 60)),
        source_caption_ids=source_refs)) for ref in refs]}
    relation_limit = 4 if 'relation' in inputs.get('fields', TARGETS) and len(refs) > 1 else 0
    return record(dict(entities=array({'anyOf': variants}, page_size), relations=array(relations, relation_limit),
                        unresolved=short_text(120)))


def new_memory(video_id, captions, extraction_hash):
    return dict(version=VERSION, video_id=video_id, captions_hash=digest(captions),
        extraction_hash=extraction_hash, source_captions={c['id']: deepcopy(c) for c in captions},
        entities={}, observations={}, relations=[], extraction_requests=[],
        next_entity=0, next_observation=0, indexes={}, partial=True, rejected_fields=[])


def rebuild_indexes(memory):
    forward = {eid: set() for eid in memory['entities']}
    reverse = {cid: dict(entity_ids=[], observation_ids=[], relation_ids=[])
               for cid in memory['source_captions']}
    def link(eid, refs, kind=None, ref=None):
        forward[eid].update(refs)
        for cid in refs:
            row = reverse[cid]
            if eid not in row['entity_ids']:
                row['entity_ids'].append(eid)
            if kind and ref not in row[kind]:
                row[kind].append(ref)
    for eid, entity in memory['entities'].items():
        link(eid, entity['mention_caption_ids'])
        for attr in entity['attributes']:
            link(eid, attr['source_caption_ids'])
    for oid, observation in memory['observations'].items():
        link(observation['entity_id'], [observation['caption_id']], 'observation_ids', oid)
    for relation in memory['relations']:
        for eid in (relation['subject'], relation['object']):
            link(eid, relation['source_caption_ids'], 'relation_ids', relation['id'])
    order = {cid: i for i, cid in enumerate(memory['source_captions'])}
    memory['indexes'] = dict(entity_to_captions={eid: sorted(refs, key=order.__getitem__)
        for eid, refs in forward.items()}, caption_to_records=reverse)


def validate_memory(memory, video_id, captions, extraction_hash):
    if (memory['version'] != VERSION or memory['video_id'] != video_id
            or memory['captions_hash'] != digest(captions)
            or memory['extraction_hash'] != extraction_hash
            or memory['source_captions'] != {c['id']: c for c in captions}):
        raise ValueError('Partial memory source, video or extraction version changed')
    old = deepcopy(memory['indexes'])
    rebuild_indexes(memory)
    if old != memory['indexes']:
        raise ValueError('Partial memory source indexes are inconsistent')


def load_memory(path, video_id, captions, extraction_hash):
    import json
    if path and Path(path).exists():
        value = json.loads(Path(path).read_text())
        validate_memory(value, video_id, captions, extraction_hash)
        return value
    value = new_memory(video_id, captions, extraction_hash)
    rebuild_indexes(value)
    return value


def entity_profiles(memory, scene_id=None):
    return [dict(id=eid, label=e['label'], scene_id=e['scene_id'],
        source_caption_ids=memory['indexes']['entity_to_captions'][eid],
        attributes=deepcopy(e['attributes'])) for eid, e in memory['entities'].items()
        if scene_id is None or e['scene_id'] == scene_id]


@metered_cpu('adaptive_delta_commit')
def commit_delta(memory, report, captions, scene_id, request, call_id):
    """Keep independently grounded facts; quarantine semantic errors with provenance.

    Foreign sources, changed source text and malformed records remain hard failures.
    A rejected entity never shifts the output-page aliases of subsequent entities.
    """
    from .schemas import validate_output
    profiles = entity_profiles(memory, scene_id)
    fields = request.get('fields', TARGETS)
    inputs = dict(captions=captions, entities=profiles, page_size=request.get('page_size', 3), fields=fields)
    schema = output_schema('transition_extract', inputs)
    refs = [e['id'] for e in profiles] + [f'B{i}' for i in range(inputs['page_size'])]
    # Decoder constraints prevent common semantic errors. At the commit boundary
    # validate their shape first so each rejected optional fact can be audited.
    source_refs = array(enum([c['id'] for c in captions]), 3)
    source_refs['minItems'] = 1
    schema['properties']['relations'] = array(record(dict(subject=enum(refs), object=enum(refs),
        predicate=short_text(60), source_caption_ids=source_refs)), 4)
    validate_output(report, schema)
    allowed = {c['id']: c for c in captions}
    if any(memory['source_captions'].get(cid) != c for cid, c in allowed.items()):
        raise ValueError('Extraction source text or timestamp changed')
    staged = deepcopy(memory)
    known = {e['id']: e for e in profiles}
    rejected = []
    def reject(kind, value, reason):
        rejected.append(dict(kind=kind, value=deepcopy(value), reason=reason,
                             call_id=call_id, request_key=request.get('request_key')))
    def support(refs):
        if not refs or not set(refs) <= allowed.keys():
            raise ValueError('Foreign or empty caption support')
        return '\n'.join(allowed[c]['text'] for c in refs)
    mapping = dict((eid, eid) for eid in known)
    used = set()
    for i, row in enumerate(report['entities']):
        # A label must occur in every cited mention, not just somewhere in the batch.
        mentions = [cid for cid in row['source_caption_ids']
                    if grounded_label(row['label'], allowed[cid]['text'])]
        for cid in row['source_caption_ids']:
            if cid not in mentions:
                reject('mention', dict(label=row['label'], caption_id=cid),
                       'Entity label not stated in its own source')
        if not mentions:
            reject('entity', row, 'No independently grounded entity mention')
            continue
        ref = row['entity_ref']
        if ref != 'new' and (ref in used or ref not in known or row['label'] != known[ref]['label']):
            reject('entity', row, 'Duplicate or changed existing entity reference')
            continue
        if ref == 'new':
            eid = f'E{staged["next_entity"]}'
            staged['next_entity'] += 1
            staged['entities'][eid] = dict(id=eid, label=row['label'], scene_id=scene_id,
                mention_caption_ids=[], attributes=[])
        else:
            eid = ref
            used.add(ref)
        mapping[f'B{i}'] = eid
        entity = staged['entities'][eid]
        entity['mention_caption_ids'] = list(dict.fromkeys(entity['mention_caption_ids'] + mentions))
        for attr in row['attributes']:
            if not grounded_label(attr['value'], support(attr['source_caption_ids'])) or not all(
                    grounded_label(row['label'], allowed[c]['text']) for c in attr['source_caption_ids']):
                reject('attribute', attr, 'Attribute sources must mention the entity and value')
                continue
            match = next((a for a in entity['attributes'] if
                (a['field'], a['value']) == (attr['field'], attr['value'])), None)
            if match is None:
                entity['attributes'].append(deepcopy(attr))
            else:
                match['source_caption_ids'] = list(dict.fromkeys(match['source_caption_ids'] + attr['source_caption_ids']))
        for observation in row['observations']:
            cid = observation['caption_id']
            if not grounded_label(row['label'], allowed[cid]['text']):
                reject('observation', observation, 'Observation source does not mention its entity')
                continue
            # Independently verify a missing mention source instead of inventing support.
            if cid not in entity['mention_caption_ids']:
                entity['mention_caption_ids'].append(cid)
            key = (eid, cid, observation['kind'])
            if any((o['entity_id'], o['caption_id'], o['kind']) == key
                   for o in staged['observations'].values()):
                continue
            oid = f'O{staged["next_observation"]}'
            staged['next_observation'] += 1
            staged['observations'][oid] = dict(id=oid, entity_id=eid,
                scene_id=scene_id, **observation, status='caption_asserted')
    for relation in report['relations']:
        a, b = mapping.get(relation['subject']), mapping.get(relation['object'])
        if 'relation' not in fields:
            reject('relation', relation, 'Relation was outside the requested extraction fields')
            continue
        if a is None or b is None or a == b:
            reject('relation', relation, 'Foreign or collapsed relation endpoints')
            continue
        text = support(relation['source_caption_ids'])
        if not grounded_label(relation['predicate'], text):
            reject('relation', relation, 'Relation predicate not stated in its sources')
            continue
        if not all(grounded_label(staged['entities'][eid]['label'], text) for eid in (a, b)):
            reject('relation', relation, 'Relation sources must mention both endpoints')
            continue
        match = next((r for r in staged['relations'] if
            (r['subject'], r['object'], r['predicate']) == (a, b, relation['predicate'])), None)
        if match is None:
            staged['relations'].append(dict(relation, id=f'R{len(staged["relations"])}',
                subject=a, object=b, scene_id=scene_id, status='caption_asserted'))
        else:
            match['source_caption_ids'] = list(dict.fromkeys(match['source_caption_ids'] + relation['source_caption_ids']))
    staged['rejected_fields'].extend(rejected)
    staged['extraction_requests'].append(dict(request, call_id=call_id,
        unresolved=report['unresolved'], rejected_field_count=len(rejected),
        status='partially_checked' if rejected else 'checked_requested_scope_only'))
    rebuild_indexes(staged)
    memory.clear()
    memory.update(staged)
    return dict(rejected_fields=len(rejected), accepted_entities=len(report['entities'])-
                sum(r['kind'] == 'entity' for r in rejected))


@metered_cpu('adaptive_memory_projection')
def memory_context(memory):
    """A QA projection compatible with bounded whole-entity context selection."""
    sources = memory['source_captions']
    entities = []
    for entity in memory['entities'].values():
        entities.append(dict(id=entity['id'], label=entity['label'],
            attributes=[dict(a, observed_at=[sources[c]['interval'][0]
                for c in a['source_caption_ids']]) for a in entity['attributes']],
            source_caption_ids=memory['indexes']['entity_to_captions'][entity['id']]))
    observations = sorted(memory['observations'].values(), key=lambda o:
        (sources[o['caption_id']]['interval'][0], o['id']))
    states = [dict(id=o['id'], entity_id=o['entity_id'], scene_id=o['scene_id'],
        description=sources[o['caption_id']]['text'], kind=o['kind'],
        first_observed_at=sources[o['caption_id']]['interval'][0],
        last_observed_at=sources[o['caption_id']]['interval'][1],
        source_caption_ids=[o['caption_id']], valid_from_bounds=None, valid_to_bounds=None,
        relation_ids=[], logical_state_id=None, fragment_kind='independent_caption_observation')
        for o in observations]
    return dict(entities=entities, states=states, transitions=[], relations=deepcopy(memory['relations']),
        lineage=[], source_type='caption', partial=True,
        rejected_field_count=len(memory['rejected_fields']),
        evidence_policy='Question-selected caption assertions. Unqueried fields are unknown. '
            'Each observation is independent; no continuous state or physical identity is implied.',
        context_selection=dict(selected_entities=len(entities), selected_states=len(states),
            partial_memory=True, completed_extraction_requests=len(memory['extraction_requests'])))


def source_priority(memory, query):
    words = set(re.findall(r'\w+', query['question'].lower()))
    ranked = sorted(memory['entities'], key=lambda eid: (
        -len(words & set(re.findall(r'\w+', memory['entities'][eid]['label'].lower()))), eid))
    matched = [eid for eid in ranked if words & set(re.findall(
        r'\w+', memory['entities'][eid]['label'].lower()))][:4]
    return list(dict.fromkeys(c for eid in matched
        for c in memory['indexes']['entity_to_captions'][eid]))


def run(packet, directory, frames, backend, cfg, target, settings, memory_path=None, evidence_only=False):
    from .budget import BudgetLedger, Meter, ResourceExhausted, ToolFailure, exploration_exhausted
    from .scene_memory import navigation_coverage
    from .temporal_pipeline import fit_qa_context
    import numpy as np

    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    # These are extraction dependencies, not a question/answer cache key.
    from .prompts import CONTRACTS
    extraction_hash = digest(dict(version=VERSION, models=cfg['models'],
        contracts={k: v for k, v in CONTRACTS.items()
                   if k.startswith('transition_')}))
    query = dict(packet['question'], id=packet['question']['question_id'])
    memory = load_memory(memory_path, query['video_id'], packet['captions'], extraction_hash)
    reused = len(memory['entities'])
    previous_rejections = len(memory['rejected_fields'])
    ledger = BudgetLedger(settings['online_seconds'],
        answer_reserve=cfg['budget']['final_answer_reserve'], calls_path=target/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, target/'raw_calls')
    opened, reviews, incomplete = [], [], []
    sufficiency_feedback = []
    local_scope_exhausted = False
    result = dict(arm='adaptive_partial', method=VERSION, question_id=query['id'],
        answer=None, error=None, reused_entity_count=reused)
    supplemental = dict(enabled=False, triggered=False, observations=[], frame_ids=[], seconds=0.)
    scenes = {s['id']: s for s in directory['scenes']}
    captions = packet['captions']
    from . import m_scene_memory as m_core
    if cfg['query'].get('temporal_qa_evidence') == 'role_bound_scene_script':
        from . import n_scene_memory as m_core
    m_enabled = cfg['query'].get('temporal_qa_evidence') == m_core.MODE
    if m_enabled:
        result['method'] = m_core.VERSION + '-source-navigation'
    scene_directory = [dict(id=s['id'], interval=s['interval'], description=s['description'])
                       for s in directory['scenes']]
    navigation_events=[]
    ranked=[]
    @metered_cpu('checkpoint_write')
    def trace(kind,**details):
        if not m_enabled:return
        navigation_events.append(dict(sequence=len(navigation_events),event=kind,**deepcopy(details)))
        write_json(target/'navigation_trace.json',dict(version=m_core.VERSION,question=query,
            scene_catalog=[dict(s,caption_ids=scenes[s['id']]['caption_ids']) for s in scene_directory],
            candidate_selection=result.get('retrieval_proposal'),retrieval=result.get('retrieval'),
            opened_scenes=list(opened),remaining_candidates=[s for s in ranked if s not in opened],
            reviews=deepcopy(reviews),events=navigation_events,stop_reason=result.get('stop_reason'),
            error=result.get('error')))
    def opened_event(sid):
        trace('scene_opened',scene_id=sid,selection_rule='next item in initial candidate ranking',
            captions=[c for c in captions if c['id'] in scenes[sid]['caption_ids']],
            caption_frame_indices=scenes[sid]['chronological_order'],model_viewed_pixels=False)
    @metered_cpu('checkpoint_write')
    def save():
        write_json(target/'network.json', memory)
        if memory_path:
            write_json(memory_path, memory)
    try:
        if m_enabled:
            scope = dict(scope='question_driven', reason='Concrete semantic gaps; no temporal coverage gate')
            trace('selection_started')
            selection_inputs=dict(question=query['question'],options=query['options'],
                                  directory=scene_directory,source_type='caption')
            selection_operation = getattr(m_core, 'SELECT_OPERATION', 'm_select_scene')
            if selection_operation in ('n_select_scene','m_candidate_rank'):
                selection_inputs = m_core.prepare_selection_inputs(selection_inputs)
                trace('selection_inputs', inputs=selection_inputs)
                entry = m_core.review_scenes(selection_inputs, meter,
                    lambda sid, request, output, call: trace('scene_candidate_review', scene_id=sid,
                        inputs=request, model_output=output, call_id=call['call_id']))
            else:
                entry, _ = meter.json(selection_operation,selection_inputs,('scene_decisions','reason'))
            result['retrieval_proposal']=deepcopy(entry)
            trace('selection_proposed',selection=entry)
            if selection_operation in ('n_select_scene','m_candidate_rank'):
                entry = m_core.normalize_selection(entry, selection_inputs)
                trace('selection_derived',selection=entry,
                      rule='Candidate priorities from archived selector decisions; descending score; directory order breaks ties')
            ranked=m_core.validate_candidates(entry,selection_inputs)
            initial=ranked[0] if ranked else None
            result['retrieval'] = dict(policy=m_core.VERSION, entry_scene=initial,
                entry_reason=entry['scene_decisions'][initial]['reason'] if initial else entry['reason'],
                directory=scene_directory,candidate_order=list(ranked),
                scene_decisions=deepcopy(entry['scene_decisions']),candidate_boundary_enforced=True,
                ranking_rule='Only relevance=candidate; descending model priority; equal scores use original directory order')
            trace('candidate_set_frozen',candidate_order=ranked,scene_decisions=entry['scene_decisions'])
            if not ranked:
                result['stop_reason']='no_relevant_candidates'
                incomplete.append(dict(stage='candidate_selection',reason='No relevant candidate scene was selected; no detailed source evidence read'))
        else:
            scope, _ = meter.json('scene_scope', dict(question=query['question'], options=query['options']),
                              ('scope', 'reason'))
            embedded, _ = meter.call('clip_text', dict(texts=[query['question']] + query['options']), build=True)
            vectors = np.asarray(embedded['vectors'])
            centers = np.asarray([s['center'] for s in directory['scenes']])
            scores = .5 * (centers @ vectors[0]) + .5 * (centers @ vectors[1:].T).mean(axis=1)
            ranked = [directory['scenes'][i]['id'] for i in sorted(range(len(scores)),
                  key=lambda i: (-float(scores[i]), i))]
            priority = set(source_priority(memory, query))
            initial = next((s for s in ranked if priority.intersection(scenes[s]['caption_ids'])), ranked[0])
            result['retrieval'] = dict(scores=scores.tolist(), entry_scene=initial,
                                      source_index_priority_caption_ids=sorted(priority))
        if initial is not None:
            opened.append(initial)
            opened_event(initial)
        for step in (range(10) if opened else []):
            selected = [c for c in captions if any(c['id'] in scenes[s]['caption_ids'] for s in opened)]
            coverage = (m_core.coverage_status() if m_enabled else
                        navigation_coverage(scope, scenes, opened, [0, len(captions)]))
            remaining = [s for s in ranked if s not in opened]
            allowed = remaining[:1] if m_enabled else remaining
            inp = dict(question=query['question'], options=query['options'],
                captions=([c for c in selected if c['id'] in scenes[opened[-1]]['caption_ids']]
                          if m_enabled else selected), entities=entity_profiles(memory),
                observations=list(memory['observations'].values()), relations=memory['relations'],
                prior_requests=memory['extraction_requests'][-8:],
                directory=([s for s in scene_directory if s['id'] in ranked] if m_enabled else scene_directory),
                allowed_scene_ids=allowed, opened_scenes=opened, coverage=coverage)
            if m_enabled:
                inp.pop('coverage')
                inp.update(current_scene=opened[-1], previous_reviews=deepcopy(reviews), source_type='caption',
                    previous_scene_captions=[c for c in selected
                        if c['id'] not in scenes[opened[-1]]['caption_ids']])
                inp.update(candidate_boundary_enforced=True,initial_candidate_order=list(ranked),
                           remaining_candidate_ids=remaining,local_scope_exhausted=local_scope_exhausted)
                if sufficiency_feedback:
                    inp.update(sufficiency_feedback=deepcopy(sufficiency_feedback),sufficiency_blocked=True)
            try:
                operation=('m_resolve_gap' if sufficiency_feedback else 'm_transition_next') if m_enabled else 'transition_next'
                plan, _ = meter.json(operation, inp,
                    ('resolution_status' if operation=='m_resolve_gap' else 'evidence_status' if m_enabled else 'sufficient',
                     'reason', 'gap', 'action', 'scene_id', 'caption_ids', 'entity_ids', 'focus', 'fields'))
                if m_enabled:
                    trace('review_proposed',operation=operation,model_decision=plan)
                    m_core.validate_step(plan, inp)
            except (ResourceExhausted, ToolFailure) as exc:
                if not exploration_exhausted(exc):
                    raise
                incomplete.append(dict(stage='adaptive_plan', reason=str(exc)))
                result['stop_reason'] = 'exploration_budget'
                break
            reviews.append(dict(plan, coverage_check=coverage, current_scene=opened[-1]))
            trace('review_decision',decision=plan,current_scene=opened[-1],
                  allowed_scene_ids=allowed,remaining_candidates=remaining)
            if plan['sufficient'] and (m_enabled or coverage['met']):
                if m_enabled:
                    checks=m_core.check_sufficiency(plan,inp,meter)
                    reviews[-1]['sufficiency_checks']=checks
                    sufficiency_feedback=[c for c in checks if not c['alternative_ruled_out']]
                    if sufficiency_feedback:
                        reviews[-1].update(sufficient=False,action='resolve_verified_gap',
                            gap='; '.join(c['missing_detail'] for c in sufficiency_feedback)[:160],
                            current_scene_assessment='needs_gap_resolution')
                        trace('sufficiency_rejected',proposal=plan,checks=checks,feedback=sufficiency_feedback)
                        continue
                result['stop_reason'] = 'sufficient'
                break
            action = plan['action']
            if not m_enabled and not coverage['met'] and (plan['sufficient'] or action == 'stop'):
                action = 'read_scene'
            if action == 'read_scene':
                visit_cap=settings.get('m_max_scene_visits',len(ranked)) if m_enabled else settings['max_scene_visits']
                if not allowed or len(opened) >= visit_cap:
                    result['stop_reason'] = 'scene_limit'
                    break
                nxt = plan['scene_id']
                if not m_enabled and not coverage['met']:
                    # Coverage takes precedence over a local-sufficiency prediction.
                    missing = coverage['missing']
                    nxt = min(allowed, key=lambda sid: (
                        -sum(isinstance(t, (int, float)) and scenes[sid]['interval'][0] <= t <
                             scenes[sid]['interval'][1] for t in missing), ranked.index(sid)))
                if nxt not in allowed:
                    if m_enabled:
                        raise ValueError('M selected an unavailable next scene')
                    nxt = allowed[0]
                opened.append(nxt)
                sufficiency_feedback=[]
                local_scope_exhausted=False
                opened_event(nxt)
                continue
            if action == 'stop':
                result['stop_reason'] = 'candidate_exhausted' if m_enabled and not remaining else 'no_useful_expansion'
                break
            ids = set(plan['caption_ids'])
            if not ids or not plan['fields'] or not plan['focus']:
                result['stop_reason'] = 'empty_extraction_scope'
                break
            selected_sources = [c for c in selected if c['id'] in ids]
            # Split scene-local deltas, rather than guessing cross-scene physical identity.
            progressed = False
            exhausted = False
            for sid in opened:
                sources = [c for c in selected_sources if c['id'] in scenes[sid]['caption_ids']]
                if not sources:
                    continue
                request = dict(scene_id=sid, caption_ids=[c['id'] for c in sources],
                    focus=plan['focus'], fields=plan['fields'], entity_ids=plan['entity_ids'])
                key = digest(request)
                if any(r['request_key'] == key for r in memory['extraction_requests']):
                    continue
                request.update(request_key=key, query_hash=digest(query),
                    status='requested_scope_only')
                extraction = dict(question=query['question'], options=query['options'],
                    gap=plan['gap'], focus=plan['focus'], fields=plan['fields'],
                    captions=sources, entities=entity_profiles(memory, sid), page_size=3,
                    source_type='caption')
                for attempt in range(1 if m_enabled else 2):
                    try:
                        report, call = meter.json('transition_extract', extraction, ('entities', 'relations', 'unresolved'))
                    except (ResourceExhausted, ToolFailure) as exc:
                        if not exploration_exhausted(exc):
                            raise
                        incomplete.append(dict(stage='transition_extract', reason=str(exc), scene_id=sid))
                        result['stop_reason'] = 'exploration_budget'
                        exhausted = True
                        break
                    try:
                        commit_delta(memory, report, sources, sid, request, call['call_id'])
                        break
                    except ValueError as exc:
                        if attempt or m_enabled:
                            raise
                        extraction['validation_feedback'] = str(exc)
                if exhausted:
                    break
                progressed = True
                save()
                trace('local_extraction_committed',scene_id=sid,request=request,call_id=call['call_id'])
            if exhausted:
                break
            if not progressed:
                if m_enabled and remaining:
                    sufficiency_feedback=[dict(missing_detail=plan['gap'],
                        reason='The requested local source/field scope was already checked without resolving the gap')]
                    local_scope_exhausted=True
                    trace('local_scope_exhausted',feedback=sufficiency_feedback)
                    continue
                result['stop_reason'] = 'candidate_exhausted' if m_enabled else 'scope_already_checked'
                break
            sufficiency_feedback=[]
            local_scope_exhausted=False
        else:
            if opened:result['stop_reason'] = 'step_limit'
        rejections = memory['rejected_fields'][previous_rejections:]
        if rejections:
            incomplete.append(dict(stage='partial_extraction', reason='Rejected unsupported extraction fields',
                                   rejected_field_count=len(rejections)))
        selected_seconds = sorted(i for sid in opened for i in scenes[sid]['chronological_order'])
        result['scope_requirement'] = scope
        result['coverage_check'] = (m_core.coverage_status() if m_enabled else
                                   navigation_coverage(scope, scenes, opened, [0, len(captions)]))
        if cfg['query'].get('temporal_sparse_visual', False) and opened:
            from .sparse_visual import supplement
            supplemental = supplement(query, directory, frames, opened, reviews, scope, meter, target)
        if evidence_only:
            result.update(evidence_only=True, selected_seconds=selected_seconds)
            return result
        context = memory_context(memory)
        inp, context, tokens = fit_qa_context(query, captions, selected_seconds, context,
            meter, final=True, visual_observations=supplemental['observations'])
        write_json(target/'qa_context.json', dict(context=context, input_tokens=tokens,
                                                selected_seconds=selected_seconds))
        answer, _ = meter.call('videotree_qa', inp, final=True)
        result.update(answer=answer['data'], selected_seconds=selected_seconds, qa_input_tokens=tokens)
    except Exception as exc:
        result['error'] = dict(type=type(exc).__name__, message=str(exc))
    finally:
        save()
        trace('stopped',reason=result.get('stop_reason'),error=result.get('error'),incomplete=incomplete)
        result.update(opened_scenes=opened, reviews=reviews, incomplete=incomplete,
            entity_count=len(memory['entities']), observation_count=len(memory['observations']),
            relation_count=len(memory['relations']), extraction_request_count=len(memory['extraction_requests']),
            rejected_field_count=len(memory['rejected_fields'])-previous_rejections,
            partial_memory=True, supplemental_visual=supplemental, cost_summary=ledger.summary())
        if evidence_only and not result['error']:
            from .temporal_pipeline import qa_context_inputs
            write_json(target/'evidence_bundle.json', dict(schema_version=1, packet_hash=digest(packet),
                memory=deepcopy(memory), opened_scenes=list(opened), selected_seconds=selected_seconds,
                supplemental_visual=deepcopy(supplemental), source_incomplete=deepcopy(incomplete),
                preparation_seconds=result['cost_summary']['budgeted_total'],
                qa_input=qa_context_inputs(query,captions,selected_seconds,memory_context(memory),
                    visual_observations=supplemental['observations'])))
        write_json(target/'result.json', result)
        meter.close()
    return result
