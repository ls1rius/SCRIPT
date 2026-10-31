"""Question-directed partial entity memory with original-caption source indexes.

Uses scheme 4's state assembler and caption identity validation on query-selected evidence.
This experimental path does not change either original control or its scene extractor.
"""
from copy import deepcopy
from pathlib import Path
import re

from .config import digest
from .accounting import metered_cpu
from .schemas import array, enum, record, short_text
from .state import write_json
from .summary_graph import grounded_label

VERSION = 'query-frame-scheme4-v4'
FIELDS = ['structure', 'components', 'shape', 'texture', 'appearance', 'color',
          'material', 'size', 'location', 'pose', 'other']
TARGETS = FIELDS + ['state', 'action', 'relation', 'time', 'identity', 'cause']


def output_schema(operation, inputs):
    ids = [e['id'] for e in inputs.get('entities', [])]
    captions = [c['id'] for c in inputs.get('captions', [])]
    if operation == 'adaptive_next':
        return record(dict(sufficient={'type': 'boolean'}, reason=short_text(100),
            gap=short_text(120), action=enum(['extract', 'read_scene', 'expand_frames', 'link_entities', 'stop']),
            scene_id=enum(inputs['allowed_scene_ids'] + inputs.get('opened_scenes', []) + ['stop']),
            caption_ids=array(enum(captions), 8), entity_ids=array(enum(ids), 4),
            focus=array(short_text(80), 3), fields=array(enum(TARGETS), 4)))
    if operation != 'adaptive_extract':
        raise ValueError('Unknown adaptive operation: ' + operation)
    page_size = inputs.get('page_size', 3)
    refs = ids + [f'B{i}' for i in range(page_size)]
    source_refs = array(enum(captions), 3)
    source_refs['minItems'] = 1
    requested = [f for f in inputs.get('fields', FIELDS) if f in FIELDS]
    attribute = record(dict(field=enum(requested), value=short_text(80),
                            source_caption_ids=source_refs))
    cause = record(dict(action={'type': ['string', 'null']}, actor={'type': ['string', 'null']},
        tool={'type': ['string', 'null']}, basis=enum(['inferred_action', 'unknown']),
        source_caption_ids=array(enum(captions), 3)))
    observation = record(dict(caption_id=enum(captions), kind=enum(['state', 'action', 'uncertain']),
        continuity=enum(['initial', 'same', 'changed', 'uncertain']), cause=cause), optional=('continuity', 'cause'))
    entity = record(dict(entity_ref=enum(['new'] + ids), label=short_text(64),
        source_caption_ids=source_refs, attributes=array(attribute, 3 if requested else 0),
        observations=array(observation, 3)))
    labels = set()
    for caption in inputs.get('captions', []):
        words = list(re.finditer(r'\w+', caption['text']))
        for i in range(len(words)):
            for j in range(i, min(i+4, len(words))):
                phrase = caption['text'][words[i].start():words[j].end()]
                if len(phrase) <= 64:
                    labels.add(phrase)
    variants = []
    for ref, label in [('new', None)] + [(e['id'], e['label']) for e in inputs.get('entities', [])]:
        variant = deepcopy(entity)
        variant['properties']['entity_ref'] = enum([ref])
        variant['properties']['label'] = enum(sorted(labels) if label is None else [label])
        variants.append(variant)
    relation = record(dict(subject=enum(refs), object=enum(refs),
        predicate=short_text(60), source_caption_ids=source_refs,
        update=enum(['present', 'end']), caption_id=enum(captions)), optional=('update', 'caption_id'))
    return record(dict(entities=array({'anyOf': variants}, page_size), relations=array(relation, 4),
                        unresolved=short_text(120)))


def new_memory(video_id, captions, extraction_hash):
    return dict(version=VERSION, video_id=video_id, captions_hash=digest(captions),
        extraction_hash=extraction_hash, source_captions={c['id']: deepcopy(c) for c in captions},
        entities={}, observations={}, relations=[], extraction_requests=[], cross_matches=[], alignment_requests=[],
        next_entity=0, next_observation=0, indexes={}, partial=True, rejected_fields=[])


def rebuild_indexes(memory):
    forward = {eid: set() for eid in memory['entities']}
    reverse = {cid: dict(entity_ids=[], observation_ids=[], relation_ids=[], correspondence_ids=[])
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
    for i, match in enumerate(memory.get('cross_matches', [])):
        for eid in (match['left_entity'], match['right_entity']):
            link(eid, match['caption_ids'], 'correspondence_ids', f'I{i}')
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
    """Validate in a copy; keep separate instances and independently dated mentions."""
    from .schemas import validate_output
    profiles = entity_profiles(memory, scene_id)
    inputs = dict(captions=captions, entities=profiles, page_size=3)
    validate_output(report, output_schema('adaptive_extract', inputs))
    # Match the original caption pipeline: unsupported optional explanations
    # stay unknown instead of discarding valid observations and the final answer.
    # Validate references before this projection; foreign citations still fail.
    from .caption_temporal import grounded_causes
    report, rejected_causes = grounded_causes(report, captions)
    staged = deepcopy(memory)
    staged.setdefault('rejected_fields', []).extend(dict(item, kind='cause', call_id=call_id,
        request_key=request.get('request_key')) for item in rejected_causes)
    allowed = {c['id']: c for c in captions}
    known = {e['id']: e for e in profiles}
    def support(refs):
        if not refs or not set(refs) <= allowed.keys():
            raise ValueError('Foreign or empty caption support')
        return '\n'.join(allowed[c]['text'] for c in refs)
    mapping = dict((eid, eid) for eid in known)
    used = set()
    for i, row in enumerate(report['entities']):
        text = support(row['source_caption_ids'])
        if not grounded_label(row['label'], text):
            raise ValueError('Entity label not stated in its own sources')
        ref = row['entity_ref']
        if ref == 'new':
            eid = f'E{staged["next_entity"]}'
            staged['next_entity'] += 1
            staged['entities'][eid] = dict(id=eid, label=row['label'], scene_id=scene_id,
                mention_caption_ids=[], attributes=[])
        else:
            if ref in used or ref not in known or row['label'] != known[ref]['label']:
                raise ValueError('Duplicate or changed existing entity reference')
            eid = ref
            used.add(ref)
        mapping[f'B{i}'] = eid
        entity = staged['entities'][eid]
        entity['mention_caption_ids'] = list(dict.fromkeys(
            entity['mention_caption_ids'] + row['source_caption_ids']))
        for attr in row['attributes']:
            if not grounded_label(attr['value'], support(attr['source_caption_ids'])):
                raise ValueError('Attribute value not stated in its sources')
            if not all(grounded_label(row['label'], allowed[c]['text'])
                       for c in attr['source_caption_ids']):
                raise ValueError('Attribute source does not mention its entity')
            match = next((a for a in entity['attributes'] if
                (a['field'], a['value']) == (attr['field'], attr['value'])), None)
            if match is None:
                entity['attributes'].append(deepcopy(attr))
            else:
                match['source_caption_ids'] = list(dict.fromkeys(
                    match['source_caption_ids'] + attr['source_caption_ids']))
        for observation in row['observations']:
            cid = observation['caption_id']
            if cid not in row['source_caption_ids']:
                if not grounded_label(row['label'], allowed[cid]['text']):
                    staged['rejected_fields'].append(dict(kind='observation', value=deepcopy(observation),
                        reason='Observation source does not mention its entity', call_id=call_id,
                        request_key=request.get('request_key')))
                    continue
                # The model can omit one mention from its bounded source list.
                # Verify that actual caption before adding the missing posting.
                if cid not in entity['mention_caption_ids']:
                    entity['mention_caption_ids'].append(cid)
            key = (eid, cid, observation['kind'])
            cause = observation.get('cause', {})
            if cause.get('basis', 'unknown') != 'unknown':
                text = support(cause['source_caption_ids'])
                if any(cause.get(k) and not grounded_label(cause[k], text) for k in ('action', 'actor', 'tool')):
                    raise ValueError('State change cause not stated in its sources')
            previous = next((o for o in staged['observations'].values()
                if (o['entity_id'], o['caption_id'], o['kind']) == key), None)
            if previous is not None:
                # A partial follow-up's unknown fields cannot revoke an existing sourced fact.
                for k in ('continuity', 'cause'):
                    if k not in observation:
                        continue
                    candidate = observation[k]
                    unknown = (candidate == 'uncertain' or
                        (candidate == 'same' and previous.get(k) == 'changed')) if k == 'continuity' else candidate['basis'] == 'unknown'
                    if k not in previous or not unknown:
                        previous[k] = deepcopy(candidate)
                continue
            oid = f'O{staged["next_observation"]}'
            staged['next_observation'] += 1
            staged['observations'][oid] = dict(id=oid, entity_id=eid,
                scene_id=scene_id, **observation, status='caption_asserted')
    for relation in report['relations']:
        a, b = mapping.get(relation['subject']), mapping.get(relation['object'])
        if a is None or b is None or a == b:
            raise ValueError('Foreign or collapsed relation endpoints')
        text = support(relation['source_caption_ids'])
        if not grounded_label(relation['predicate'], text):
            raise ValueError('Relation predicate not stated in its sources')
        if not all(grounded_label(staged['entities'][eid]['label'], text) for eid in (a, b)):
            raise ValueError('Relation sources must mention both endpoints')
        cid = relation.get('caption_id', relation['source_caption_ids'][0])
        if cid not in relation['source_caption_ids']:
            raise ValueError('Relation time must be supported by its own sources')
        match = next((r for r in staged['relations'] if
            (r['subject'], r['object'], r['predicate'], r.get('update', 'present')) ==
            (a, b, relation['predicate'], relation.get('update', 'present'))), None)
        if match is None:
            staged['relations'].append(dict(relation, id=f'R{len(staged["relations"])}',
                subject=a, object=b, scene_id=scene_id, caption_id=cid, status='caption_asserted'))
        else:
            match['source_caption_ids'] = list(dict.fromkeys(
                match['source_caption_ids'] + relation['source_caption_ids']))
    staged['extraction_requests'].append(dict(request, call_id=call_id,
        unresolved=report['unresolved'], rejected_cause_count=len(rejected_causes),
        status='checked_with_unknown_cause' if rejected_causes else 'checked_requested_scope_only'))
    rebuild_indexes(staged)
    rebuild_temporal_network(staged)
    memory.clear()
    memory.update(staged)


@metered_cpu('adaptive_scheme4_state_assembly')
def rebuild_temporal_network(memory):
    """Replay sourced deltas by time through scheme 4's unchanged state assembler."""
    from .temporal_entity import new_network, canonical
    from .caption_temporal import commit_page, _merge_caption_identity
    net = new_network()
    net.update(source_type='caption', evidence_status='caption_asserted', physical_identity_confirmed=False)
    net['next_entity'] = memory['next_entity']
    for eid, entity in memory['entities'].items():
        net['entities'][eid] = dict(id=eid, label=entity['label'], attributes=[], scene_ids=[], state_ids=[])
    all_sources = list(memory['source_captions'].values())
    for eid, entity in memory['entities'].items():
        observations = sorted((o for o in memory['observations'].values() if o['entity_id'] == eid),
            key=lambda o: (memory['source_captions'][o['caption_id']]['interval'][0], o['id']))
        if not observations:
            observations = [dict(caption_id=entity['mention_caption_ids'][0], continuity='initial')]
        # A caption is one sampled assertion, regardless of the number of query fields it supplies.
        unique = {}
        for o in observations:
            if o['caption_id'] not in unique:
                unique[o['caption_id']] = deepcopy(o)
                continue
            merged = unique[o['caption_id']]
            for key in ('continuity', 'cause'):
                if key in o:
                    unknown = (o[key] == 'uncertain' or
                        (o[key] == 'same' and merged.get(key) == 'changed')) if key == 'continuity' else o[key]['basis'] == 'unknown'
                    if key not in merged or not unknown:
                        merged[key] = deepcopy(o[key])
        rows = []
        for i, o in enumerate(unique.values()):
            continuity = 'initial' if i == 0 else o.get('continuity', 'uncertain')
            if i and continuity == 'initial':
                continuity = 'uncertain'
            rows.append(dict(caption_id=o['caption_id'], continuity=continuity,
                state=memory['source_captions'][o['caption_id']]['text'],
                cause=deepcopy(o.get('cause', dict(action=None, actor=None, tool=None,
                    basis='unknown', source_caption_ids=[])))))
        r = dict(entities=[dict(entity_ref=eid, label=entity['label'],
            attributes=deepcopy(entity['attributes']), observations=rows)], relations=[],
            more_entities=False, unresolved='')
        commit_page(net, r, all_sources, entity['scene_id'], f'adaptive:{eid}', list(net['entities']))
    for relation in sorted(memory['relations'], key=lambda r: memory['source_captions'][
            r.get('caption_id', r['source_caption_ids'][0])]['interval'][0]):
        refs = relation['source_caption_ids']
        report = dict(entities=[], relations=[dict(subject=relation['subject'], object=relation['object'],
            predicate=relation['predicate'], caption_id=relation.get('caption_id', refs[0]),
            update=relation.get('update', 'present'), source_caption_ids=refs)],
            more_entities=False, unresolved='')
        commit_page(net, report, all_sources, relation['scene_id'], 'adaptive:relations', list(net['entities']))
    for match in memory.get('cross_matches', []):
        net['identities'].append(dict(match, source_type='caption'))
        a, b = canonical(net, match['left_entity']), canonical(net, match['right_entity'])
        if a == b:
            continue
        if match['decision'] == 'same':
            _merge_caption_identity(net, a, b)
        elif match.get('relation') in ('derived_from', 'part_of', 'assembled_into'):
            net['lineage'].append(dict(child=a, parent=b, relation=match['relation'],
                source_caption_ids=match['caption_ids'], status='text_inferred'))
    # The authoritative original-caption table is stored once at memory level.
    net.pop('source_captions', None)
    net['source_table'] = 'memory.source_captions'
    memory['temporal_network'] = net


def cross_align_partial(memory, scene_id, query, gap, meter, relevant_ids=()):
    """Question-directed identity/history links, using scheme 4's exact source validation."""
    from .scene_memory import validate_matches
    relevant = set(relevant_ids)
    words = set(re.findall(r'\w+', query['question'].lower() + ' ' + gap.lower()))
    def ranked(ids):
        return sorted(ids, key=lambda eid: (
            -(2*int(eid in relevant) + len(words & set(re.findall(r'\w+', memory['entities'][eid]['label'].lower())))), eid))[:3]
    left_ids = ranked([eid for eid, e in memory['entities'].items() if e['scene_id'] == scene_id])
    right_ids = ranked([eid for eid, e in memory['entities'].items() if e['scene_id'] != scene_id])
    if not left_ids or not right_ids:
        return False
    def graph(ids):
        rows = [dict(id=eid, label=memory['entities'][eid]['label'],
            caption_ids=memory['entities'][eid]['mention_caption_ids']) for eid in ids]
        refs = {c for e in rows for c in e['caption_ids']}
        return dict(entities=rows, captions=[deepcopy(c) for c in memory['source_captions'].values() if c['id'] in refs])
    left, right = graph(left_ids), graph(right_ids)
    key = digest(dict(left=left, right=right, gap=gap, question=query['question']))
    if key in memory['alignment_requests']:
        return False
    report, _ = meter.json('scene_correspondence_lineage', dict(left=left, right=right,
        question=query['question'], gap=gap), ('matches',))
    matches = validate_matches(report, left, right)
    same = [m for m in matches if m['decision'] == 'same']
    for match in matches:
        if match['decision'] == 'same' and (sum(m['left_entity'] == match['left_entity'] for m in same) > 1
                or sum(m['right_entity'] == match['right_entity'] for m in same) > 1):
            match.update(decision='unknown', relation='unknown', status='unresolved', reason='competing candidates')
    staged = deepcopy(memory)
    staged['cross_matches'].extend(matches)
    staged['alignment_requests'].append(key)
    rebuild_indexes(staged)
    rebuild_temporal_network(staged)
    memory.clear()
    memory.update(staged)
    return True


@metered_cpu('adaptive_memory_projection')
def memory_context(memory, query=None):
    """Retrieve scheme-4 states/history by the current question, with partial coverage explicit."""
    from .temporal_entity import entity_context
    from .caption_temporal import compact_context
    if 'temporal_network' not in memory:
        rebuild_temporal_network(memory)
    net = memory['temporal_network']
    query = query or dict(question='', options=[])
    view = compact_context(entity_context(net, query), net)
    chosen = {e['id'] for e in view['entities']}
    from .temporal_entity import canonical
    view.update(partial=True, unresolved_observations=[deepcopy(o) for o in net['observations']
        if canonical(net, o['entity_id']) in chosen])
    view['context_selection']['partial_memory'] = True
    return view


def source_priority(memory, query):
    words = set(re.findall(r'\w+', query['question'].lower()))
    ranked = sorted(memory['entities'], key=lambda eid: (
        -len(words & set(re.findall(r'\w+', memory['entities'][eid]['label'].lower()))), eid))
    matched = [eid for eid in ranked if words & set(re.findall(
        r'\w+', memory['entities'][eid]['label'].lower()))][:4]
    return list(dict.fromkeys(c for eid in matched
        for c in memory['indexes']['entity_to_captions'][eid]))


def grounded_query_delta(memory, report, sources, sid, request, call_id):
    """Quarantine unsupported optional attributes; retain strict source checks."""
    from .schemas import validate_output
    validate_output(report, output_schema('adaptive_extract', dict(captions=sources,
        entities=entity_profiles(memory, sid), page_size=3)))
    clean = deepcopy(report)
    lookup = {c['id']: c['text'] for c in sources}
    rejected = []
    for row in clean['entities']:
        kept = []
        for attr in row['attributes']:
            refs = attr['source_caption_ids']
            if not refs or not set(refs) <= lookup.keys():
                raise ValueError('Foreign or empty attribute support')
            if (grounded_label(attr['value'], '\n'.join(lookup[c] for c in refs)) and
                    all(grounded_label(row['label'], lookup[c]) for c in refs)):
                kept.append(attr)
            else:
                rejected.append(dict(kind='attribute', value=attr, call_id=call_id,
                    request_key=request['request_key'], reason='Attribute or entity not stated in its own sources'))
        row['attributes'] = kept
    labels = {e['id']: e['label'] for e in entity_profiles(memory, sid)}
    identities = {eid: eid for eid in labels}
    for i, row in enumerate(clean['entities']):
        alias = f'B{i}'
        labels[alias] = row['label']
        identities[alias] = alias if row['entity_ref'] == 'new' else row['entity_ref']
    kept = []
    for relation in clean['relations']:
        refs = relation['source_caption_ids']
        if not refs or not set(refs) <= lookup.keys():
            raise ValueError('Foreign or empty relation support')
        text = '\n'.join(lookup[c] for c in refs)
        a, b = relation['subject'], relation['object']
        supported = (a in identities and b in identities and identities[a] != identities[b]
            and grounded_label(relation['predicate'], text)
            and all(grounded_label(labels[e], text) for e in (a, b))
            and relation.get('caption_id', refs[0]) in refs)
        if supported:
            kept.append(relation)
        else:
            rejected.append(dict(kind='relation', value=relation, call_id=call_id,
                request_key=request['request_key'], reason='Unsupported or collapsed relation endpoints/predicate'))
    clean['relations'] = kept
    commit_delta(memory, clean, sources, sid, request, call_id)
    memory['rejected_fields'].extend(rejected)


def select_bounded_action(plan, selected, memory, read_streak, coverage, max_read_streak=2):
    """After bounded reading, inspect an unprocessed, literally matching focus.

    This schedules extraction, never asserts a fact or creates an entity. A new
    field can revisit an old source; a checked source/field/focus cannot loop.
    """
    if (plan['action'] not in ('read_scene', 'expand_frames') or read_streak < max_read_streak
            or not coverage['met'] or not plan['fields'] or not plan['focus']):
        return plan
    covered = set()
    for request in memory['extraction_requests']:
        if (set(plan['fields']) <= set(request.get('fields', [])) and
                set(plan['focus']) <= set(request.get('focus', []))):
            covered.update(request.get('caption_ids', []))
    candidates = [c['id'] for c in selected if c['id'] not in covered and any(
        grounded_label(f, c['text']) for f in plan['focus'])]
    if not candidates:
        return plan
    return dict(plan, action='extract', caption_ids=candidates[-8:],
                policy_override='inspect_read_evidence_before_further_expansion')


def run(packet, directory, frames, backend, cfg, target, settings, memory_path=None, features=None):
    from .budget import BudgetLedger, Meter, ResourceExhausted, ToolFailure, exploration_exhausted
    from .temporal_pipeline import fit_qa_context
    from .adaptive_sampling import Sampler
    from .prompts import CONTRACTS

    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    extraction_hash = digest(dict(version=VERSION, models=cfg['models'],
        features=packet.get('feature_hash', packet.get('frame_features')),
        contracts={k: v for k, v in CONTRACTS.items() if k.startswith('adaptive_') or k == 'scene_correspondence_lineage'}))
    query = dict(packet['question'], id=packet['question']['question_id'])
    memory = load_memory(memory_path, query['video_id'], packet['captions'], extraction_hash)
    reused = len(memory['entities'])
    ledger = BudgetLedger(settings['online_seconds'], answer_reserve=cfg['budget']['final_answer_reserve'],
                          calls_path=target/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, target/'raw_calls')
    opened, reviews, incomplete, seen = [], [], [], set()
    result = dict(arm='adaptive_scheme4', method=VERSION, question_id=query['id'],
                  answer=None, error=None, reused_entity_count=reused, base_scheme=4)
    supplemental = dict(enabled=False, triggered=False, observations=[], frame_ids=[], seconds=0.)
    scenes = {s['id']: s for s in directory['scenes']}
    captions = packet['captions']
    sampler = None

    @metered_cpu('checkpoint_write')
    def save():
        write_json(target/'network.json', memory)
        if memory_path:
            write_json(memory_path, memory)

    try:
        scope, _ = meter.json('scene_scope', dict(question=query['question'], options=query['options']), ('scope', 'reason'))
        embedded, _ = meter.call('clip_text', dict(texts=[query['question']]), build=True)
        if features is None:
            if 'frame_features' in packet:
                features = packet['frame_features']
            else:
                import torch
                features = torch.load(packet['feature_uri'], map_location='cpu', weights_only=True).tolist()
        sampler = Sampler(directory, features, embedded['vectors'][0])
        initial = sampler.rank_scenes()[0]['scene_id']
        priority = source_priority(memory, query)
        result['retrieval'] = dict(entry_scene=initial, scene_ranking=sampler.rank_scenes(),
            source_index_priority_caption_ids=priority, policy='question_and_current_gap_at_both_levels')
        opened.append(initial)
        sampler.read(initial, seen, settings.get('adaptive_initial_frames', 2), priority)
        gap_embeddings = {}
        max_scenes = settings['max_scene_visits']
        bounded_policy = settings.get('adaptive_bounded_policy', False)
        read_streak = 0

        def expand(plan, coverage_only=False):
            coverage = sampler.coverage(scope, seen)
            candidates = sampler.coverage_candidates(scope, seen, opened) if coverage_only else [
                dict(row, unread_caption_ids=[c for c in scenes[row['scene_id']]['caption_ids'] if c not in seen])
                for row in sampler.rank_scenes()]
            candidates = [r for r in candidates if r['unread_caption_ids'] and
                (r['scene_id'] in opened or len(opened) < max_scenes)]
            if plan['action'] == 'expand_frames' and not coverage_only:
                within = [r for r in candidates if r['scene_id'] in opened]
                candidates = within or candidates
            elif plan['action'] == 'read_scene' and not coverage_only:
                fresh = [r for r in candidates if r['scene_id'] not in opened]
                candidates = fresh or candidates
            if not candidates:
                return False
            top = candidates[:3]
            selected_scene = next((r for r in top if r['scene_id'] == plan['scene_id']), top[0])
            sid = selected_scene['scene_id']
            if sid not in opened:
                opened.append(sid)
            temporal = bool(set(plan['fields']) & {'action', 'state', 'time', 'cause'})
            new = sampler.read(sid, seen, settings.get('adaptive_expand_frames', 3), priority,
                temporal_context=temporal, allowed_caption_ids=selected_scene['unread_caption_ids'])
            return bool(new)

        for step in range(settings.get('adaptive_max_steps', 16)):
            selected = [c for c in captions if c['id'] in seen]
            coverage = sampler.coverage(scope, seen)
            scene_ranking = sampler.rank_scenes()
            allowed = [r['scene_id'] for r in scene_ranking if r['scene_id'] not in opened]
            context = memory_context(memory, query)
            inp = dict(question=query['question'], options=query['options'], captions=selected,
                entities=entity_profiles(memory), temporal_memory=context,
                prior_requests=memory['extraction_requests'][-8:],
                directory=[dict(id=s['id'], interval=s['interval'], description=s['description'])
                           for s in directory['scenes']], scene_ranking=scene_ranking,
                unread_frame_counts={sid: sum(c not in seen for c in scenes[sid]['caption_ids']) for sid in opened},
                allowed_scene_ids=allowed, opened_scenes=opened, coverage=coverage, source_type='caption',
                reading_policy='Only sampled-frame summaries are provided; opening a scene does not read all its text.')
            if bounded_policy:
                inp.update(remaining_plans=settings.get('adaptive_max_steps', 16)-step,
                    consecutive_read_steps=read_streak, previous_plans=reviews[-2:],
                    planning_policy='Do not repeat a gap without new evidence. Inspect already-read matching sources '
                    'before further expansion. Stop sufficient only with supported option discrimination and coverage.')
            try:
                plan, _ = meter.json('adaptive_next', inp,
                    ('sufficient', 'reason', 'gap', 'action', 'scene_id', 'caption_ids', 'entity_ids', 'focus', 'fields'))
            except (ResourceExhausted, ToolFailure) as exc:
                if not exploration_exhausted(exc):
                    raise
                incomplete.append(dict(stage='adaptive_plan', reason=str(exc)))
                result['stop_reason'] = 'exploration_budget'
                break
            if bounded_policy and not plan['sufficient']:
                plan = select_bounded_action(plan, selected, memory, read_streak, coverage,
                    settings.get('adaptive_max_read_streak', 2))
            reviews.append(dict(plan, coverage_check=coverage, read_caption_ids=[c['id'] for c in selected]))
            if plan['sufficient'] and coverage['met']:
                result['stop_reason'] = 'sufficient'
                break
            gap = plan['gap'].strip()
            if gap and gap != sampler.gap:
                if gap not in gap_embeddings:
                    try:
                        encoded, _ = meter.call('clip_text', dict(texts=[query['question'] + '\nMissing evidence: ' + gap]), build=True)
                    except (ResourceExhausted, ToolFailure) as exc:
                        if not exploration_exhausted(exc):
                            raise
                        incomplete.append(dict(stage='gap_retrieval', reason=str(exc)))
                        result['stop_reason'] = 'exploration_budget'
                        break
                    gap_embeddings[gap] = encoded['vectors'][0]
                sampler.set_gap(gap, gap_embeddings[gap])
            if not coverage['met'] and (plan['sufficient'] or plan['action'] == 'stop'):
                if expand(plan, coverage_only=True):
                    continue
                result['stop_reason'] = 'coverage_or_scene_limit'
                break
            if plan['action'] in ('read_scene', 'expand_frames'):
                if expand(plan, coverage_only=bounded_policy and not coverage['met']):
                    read_streak += 1
                    continue
                result['stop_reason'] = 'no_unread_candidates'
                break
            if plan['action'] == 'link_entities':
                sid = plan['scene_id'] if plan['scene_id'] in opened else opened[-1]
                try:
                    linked = cross_align_partial(memory, sid, query, gap, meter, plan['entity_ids'])
                except (ResourceExhausted, ToolFailure) as exc:
                    if not exploration_exhausted(exc):
                        raise
                    incomplete.append(dict(stage='identity', reason=str(exc)))
                    result['stop_reason'] = 'exploration_budget'
                    break
                if linked:
                    save()
                    continue
                if expand(dict(plan, action='expand_frames')):
                    continue
                result['stop_reason'] = 'no_new_identity_evidence'
                break
            if plan['action'] == 'stop':
                result['stop_reason'] = 'no_useful_expansion'
                break
            ids = set(plan['caption_ids'])
            read_streak = 0
            if not ids or not plan['fields'] or not plan['focus']:
                result['stop_reason'] = 'empty_extraction_scope'
                break
            progressed = False
            exhausted = False
            for sid in opened:
                sources = [c for c in selected if c['id'] in ids and c['id'] in scenes[sid]['caption_ids']]
                if not sources:
                    continue
                request = dict(scene_id=sid, caption_ids=[c['id'] for c in sources],
                    focus=plan['focus'], fields=plan['fields'], entity_ids=plan['entity_ids'])
                key = digest(request)
                if any(r['request_key'] == key for r in memory['extraction_requests']):
                    continue
                request.update(request_key=key, query_hash=digest(query), status='requested_scope_only')
                extraction = dict(question=query['question'], options=query['options'], gap=gap,
                    focus=plan['focus'], fields=plan['fields'], captions=sources,
                    entities=entity_profiles(memory, sid), temporal_memory=context, page_size=3, source_type='caption')
                try:
                    for attempt in range(2):
                        report, call = meter.json('adaptive_extract', extraction, ('entities', 'relations', 'unresolved'))
                        try:
                            grounded_query_delta(memory, report, sources, sid, request, call['call_id'])
                            break
                        except ValueError as exc:
                            if attempt:
                                raise
                            extraction['validation_feedback'] = str(exc)
                    # Preserve scheme-4 identity/lineage/history, with only the query's sampled entities.
                    cross_align_partial(memory, sid, query, gap, meter, plan['entity_ids'])
                except (ResourceExhausted, ToolFailure) as exc:
                    if not exploration_exhausted(exc):
                        raise
                    incomplete.append(dict(stage='adaptive_extract_or_identity', reason=str(exc), scene_id=sid))
                    result['stop_reason'] = 'exploration_budget'
                    exhausted = True
                    break
                progressed = True
                save()
            if exhausted:
                break
            if not progressed:
                if expand(dict(plan, action='expand_frames')):
                    continue
                result['stop_reason'] = 'scope_already_checked'
                break
        else:
            result['stop_reason'] = 'step_limit'
        selected_seconds = sorted(sampler.positions[c] for c in seen)
        result['scope_requirement'] = scope
        result['coverage_check'] = sampler.coverage(scope, seen)
        if not result['coverage_check']['met']:
            incomplete.append(dict(stage='coverage', missing=result['coverage_check']['missing']))
        if memory.get('rejected_fields'):
            incomplete.append(dict(stage='partial_extraction', rejected_field_count=len(memory['rejected_fields'])))
        if cfg['query'].get('temporal_sparse_visual', False):
            from .sparse_visual import supplement
            sampled_directory = deepcopy(directory)
            sampled_directory['captions'] = [c for c in captions if c['id'] in seen]
            for scene in sampled_directory['scenes']:
                scene['caption_ids'] = [c for c in scene['caption_ids'] if c in seen]
            supplemental = supplement(query, sampled_directory, frames, opened, reviews, scope, meter, target)
        context = memory_context(memory, query)
        inp, context, tokens = fit_qa_context(query, captions, selected_seconds, context,
            meter, final=True, visual_observations=supplemental['observations'])
        write_json(target/'qa_context.json', dict(context=context, input_tokens=tokens,
            selected_seconds=selected_seconds, read_caption_ids=sorted(seen)))
        answer, _ = meter.call('videotree_qa', inp, final=True)
        result.update(answer=answer['data'], selected_seconds=selected_seconds, qa_input_tokens=tokens)
    except Exception as exc:
        result['error'] = dict(type=type(exc).__name__, message=str(exc))
    finally:
        save()
        net = memory.get('temporal_network', {})
        result.update(opened_scenes=opened, reviews=reviews, incomplete=incomplete,
            entity_count=len(net.get('entities', memory['entities'])), observation_count=len(memory['observations']),
            state_count=len(net.get('states', {})), transition_count=len(net.get('transitions', [])),
            relation_count=len(net.get('relations', [])), identity_comparisons=len(net.get('identities', [])),
            extraction_request_count=len(memory['extraction_requests']), read_caption_ids=sorted(seen),
            read_caption_count=len(seen), total_caption_count=len(captions),
            frame_selection_trace=sampler.trace if sampler else [],
            rejected_field_count=len(memory.get('rejected_fields', [])),
            partial_memory=True, supplemental_visual=supplemental, cost_summary=ledger.summary())
        write_json(target/'result.json', result)
        meter.close()
    return result
