"""Source-span deltas, event instances and sparse occurrence indexes for scheme 4.

The original adaptive scheme is kept intact. Its sourced state assembler is
reused through a projection; raw local entities and identity decisions remain
revisable. Caption assertions are never relabelled as visual verification.
"""
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import re

from . import adaptive_entity as base
from .accounting import metered_cpu
from .config import digest
from .schemas import array, enum, record, short_text, validate_output
from .optimized_sampling import terms

VERSION = 'query-event-scheme4-v5'
IDENTITY_VERSION = 'sourced-pair-v1'
ROLES = ('actor', 'giver', 'receiver', 'item', 'tool', 'location')


def tokens(text):
    return list(re.finditer(r'\w+|[^\w\s]', text))


def source_spans(captions):
    result = {}
    for c in captions:
        ts = tokens(c['text'])
        for a, token in enumerate(ts):
            if not re.search(r'\w', token.group()):
                continue
            for b in range(a+1, min(a+4, len(ts))+1):
                if not re.search(r'\w', ts[b-1].group()):
                    continue
                text = c['text'][token.start():ts[b-1].end()]
                if len(text) <= 80:
                    result[f'{c["id"]}@{a}:{b}'] = dict(caption_id=c['id'], start_token=a, end_token=b, text=text)
    result.update(event_spans(captions))
    return result


def event_spans(captions):
    """Complete source assertions, keeping event action anchors out of ID/tag tokens."""
    result = {}
    for c in captions:
        ts = tokens(c['text'])
        prefix = re.match(r'^\s*#[CO]\s+', c['text'])
        a = next((i for i, t in enumerate(ts) if not prefix or t.start() >= prefix.end()), len(ts))
        b = len(ts)
        while b > a and not re.search(r'\w', ts[b-1].group()):
            b -= 1
        if sum(bool(re.search(r'\w', t.group())) for t in ts[a:b]) < 2:
            continue
        text = c['text'][ts[a].start():ts[b-1].end()]
        result[f'{c["id"]}@{a}:{b}'] = dict(caption_id=c['id'], start_token=a, end_token=b, text=text)
    return result


def source_view(captions):
    spans = source_spans(captions)
    return [dict(c, spans=[dict(ref=ref, text=s['text']) for ref, s in spans.items() if s['caption_id'] == c['id']])
            for c in captions]


def span_schema(captions):
    return record(dict(span_id=enum(source_spans(captions))))


def event_role_schema(ids, kind):
    if not ids:
        return record({})
    if kind == 'transfer':
        return record({role: enum(ids) for role in ROLES}, optional=ROLES)
    # An action has no automatic giver/receiver. Its actor cannot also become
    # its item/tool/location just because only one entity ID is available.
    objects = ('item', 'tool', 'location')
    variants = [record({role: enum(ids) for role in objects}, optional=objects)]
    for actor in ids:
        other = [eid for eid in ids if eid != actor]
        fields = dict(actor=enum([actor]))
        if other:
            fields.update({role: enum(other) for role in objects})
        variants.append(record(fields, optional=objects))
    return {'anyOf': variants}


def output_schema(operation, inputs):
    caps = inputs.get('captions', [])
    ids = [e['id'] for e in inputs.get('entities', [])]
    unresolved = enum(['']) if inputs.get('compact_diagnostics') else short_text(120)
    if operation == 'optimized_plan':
        return record(dict(scope=enum(['local', 'global', 'multi_scene', 'exhaustive']),
            depth=enum(['direct', 'entity', 'event', 'causal']), sufficient={'type': 'boolean'},
            reason=short_text(100), gap=short_text(120),
            support_caption_ids=array(enum([c['id'] for c in caps]), 12),
            scene_ids=array(enum(inputs['allowed_scene_ids']), 2),
            caption_ids=array(enum([c['id'] for c in caps]), 12),
            entity_ids=array(enum(ids), 4), focus=array(short_text(64), 3),
            fields=array(enum(base.TARGETS), 4), need_identity={'type': 'boolean'}))
    if operation == 'optimized_events':
        # Event extraction runs AFTER entity commit. Only real, committed IDs
        # are offered; output-page aliases can never refer to nonexistent rows.
        variants = []
        for ref, selected in event_spans(caps).items():
            cid = selected['caption_id']
            own = array(enum([cid]), 1)
            own['minItems'] = 1
            local = [e['id'] for e in inputs.get('entities', []) if e['label'] in selected['text']]
            for kind in ('action', 'transfer') if len(local) > 1 else ('action',):
                variants.append(record(dict(event_ref=enum(['new']),
                    kind=enum([kind]), action=record(dict(span_id=enum([ref]))),
                    roles=event_role_schema(local, kind), source_caption_ids=own, basis=enum(['caption_explicit']))))
        events = array({'anyOf': variants}, 4) if variants else array(record({}), 0)
        return record(dict(entities=array(record({}), 0), relations=array(record({}), 0),
                           events=events, unresolved=unresolved))
    if operation != 'optimized_extract':
        raise ValueError('Unknown optimized operation: ' + operation)
    span = span_schema(caps)
    refs = ids + [f'B{i}' for i in range(inputs.get('page_size', 3))]
    cause = record({k: span for k in ('action', 'actor', 'tool')}, optional=('action', 'actor', 'tool'))
    attribute = record(dict(field=enum([f for f in inputs.get('fields', []) if f in base.FIELDS]), value=span))
    observation = record(dict(caption_id=enum([c['id'] for c in caps]),
        kind=enum(['state', 'action', 'uncertain']), continuity=enum(['initial', 'same', 'changed', 'uncertain']),
        cause=cause), optional=('cause',))
    # Complete event assertions may be long; entity names remain short spans.
    mentions = array(record(dict(span_id=enum([ref for ref, s in source_spans(caps).items()
        if len(s['text']) <= 64 and s['end_token']-s['start_token'] <= 4]))), 3)
    mentions['minItems'] = 1
    entity = record(dict(entity_ref=enum(['new'] + ids), mentions=mentions,
        attributes=array(attribute, 3 if attribute['properties']['field'].get('enum') != ['__no_allowed_reference__'] else 0),
        observations=array(observation, 3)))
    variants = [dict(deepcopy(entity), properties=dict(deepcopy(entity['properties']), entity_ref=enum(['new'])))]
    choices = source_spans(caps)
    for e in inputs.get('entities', []):
        allowed = [ref for ref, s in choices.items() if s['text'] == e['label']]
        if allowed:
            variant = deepcopy(entity)
            variant['properties']['entity_ref'] = enum([e['id']])
            variant['properties']['mentions']['items'] = record(dict(span_id=enum(allowed)))
            variants.append(variant)
    relation = record(dict(subject=enum(refs), object=enum(refs), predicate=span,
                           update=enum(['present', 'end'])))
    roles = record({role: enum(refs) for role in ROLES}, optional=ROLES)
    event = record(dict(event_ref=enum(['new'] + [e['id'] for e in inputs.get('events', [])]),
        kind=enum(['action', 'transfer']), action=span, roles=roles,
        source_caption_ids=array(enum([c['id'] for c in caps]), 8),
        basis=enum(['caption_explicit', 'inferred_from_states'])))
    return record(dict(entities=array({'anyOf': variants}, inputs.get('page_size', 3)),
        relations=array(relation, 4), events=array(event, 4 if inputs.get('depth') in ('event', 'causal')
                                                  and not inputs.get('entity_stage') else 0),
        unresolved=unresolved))


def new_memory(video_id, captions, extraction_hash, directory):
    m = base.new_memory(video_id, captions, extraction_hash)
    m.update(version=VERSION, events=[], relation_records=[], rejected_fields=[],
             ownership={c: s['id'] for s in directory['scenes'] for c in s['caption_ids']},
             directory_hash=digest(directory['scenes']), identity_revision=0)
    rebuild(m)
    return m


def load_memory(path, video_id, captions, extraction_hash, directory):
    if path and Path(path).exists():
        m = json.loads(Path(path).read_text())
        if (m['version'] != VERSION or m['video_id'] != video_id or
                m['extraction_hash'] != extraction_hash or m['captions_hash'] != digest(captions) or
                m['source_captions'] != {c['id']: c for c in captions} or
                m['directory_hash'] != digest(directory['scenes'])):
            raise ValueError('Optimized memory source, configuration or version changed')
        indexes = deepcopy(m['indexes'])
        rebuild(m)
        if indexes != m['indexes']:
            raise ValueError('Optimized memory indexes are inconsistent')
        return m
    return new_memory(video_id, captions, extraction_hash, directory)


def recover_span(span, sources):
    if 'span_id' in span:
        selected = source_spans(list(sources.values())).get(span['span_id'])
        if selected is None:
            raise ValueError('Invalid or foreign source span ID')
        return selected['text']
    if span.get('caption_id') not in sources:
        raise ValueError('Foreign source caption in span')
    c = sources[span['caption_id']]
    ts = tokens(c['text'])
    a, b = span.get('start_token'), span.get('end_token')
    if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(ts):
        raise ValueError('Invalid source span bounds')
    return c['text'][ts[a].start():ts[b-1].end()]


def span_caption(span):
    return span['span_id'].rsplit('@', 1)[0] if 'span_id' in span else span['caption_id']


def _check_foreign(value, allowed):
    # Unknown source IDs are a hard failure even in an optional field.
    if isinstance(value, dict):
        for key, v in value.items():
            if key == 'span_id' and v.rsplit('@', 1)[0] not in allowed:
                raise ValueError('Foreign source span')
            if key == 'caption_id' and v not in allowed:
                raise ValueError('Foreign source caption')
            if key == 'source_caption_ids' and not set(v) <= allowed:
                raise ValueError('Foreign source captions')
            _check_foreign(v, allowed)
    elif isinstance(value, list):
        for v in value:
            _check_foreign(v, allowed)


@metered_cpu('optimized_delta_commit')
def commit_delta(memory, report, captions, scene_id, request, call_id, fields, depth):
    """Commit core facts atomically; quarantine optional semantic failures.

    The model emits token positions, never labels, state descriptions or causes
    copied from some unrelated caption. Invalid sources remain hard errors.
    """
    sources = {c['id']: c for c in captions}
    if any(memory['source_captions'].get(c['id']) != c or memory['ownership'][c['id']] != scene_id for c in captions):
        raise ValueError('Extraction sources changed or crossed the requested scene')
    _check_foreign(report, set(sources))
    profiles = base.entity_profiles(memory, scene_id)
    inputs = dict(captions=captions, entities=profiles, fields=fields, depth=depth, events=memory['events'], page_size=3)
    validate_output(report, output_schema('optimized_extract', inputs))
    staged = deepcopy(memory)
    rejected = []
    canonical = []
    mapping = {e['id']: e['id'] for e in profiles}
    next_id = staged['next_entity']
    for i, row in enumerate(report['entities']):
        mentions = row['mentions']
        labels = [recover_span(s, sources) for s in mentions]
        # The first mention anchors a new local entity. Additional mismatching
        # mentions are unsupported optional links, not permission to merge names.
        ref = row['entity_ref']
        label = labels[0] if ref == 'new' else memory['entities'][ref]['label']
        valid_mentions = [s for s, text in zip(mentions, labels) if text == label]
        if not valid_mentions:
            raise ValueError('Existing local entity lacks its own exact source mention')
        for s, text in zip(mentions, labels):
            if text != label:
                rejected.append(dict(kind='mention', value=deepcopy(s), reason='Additional mention has a different label'))
        if len(label) > 64:
            raise ValueError('Entity mention is too long')
        if ref == 'new':
            eid = f'E{next_id}'
            next_id += 1
        else:
            eid = ref
            if label != memory['entities'][eid]['label']:
                raise ValueError('Existing local entity label changed')
        mapping[f'B{i}'] = eid
        own = list(dict.fromkeys(span_caption(s) for s in valid_mentions))
        attrs = []
        for attr in row['attributes']:
            try:
                span = attr['value']
                value = recover_span(span, sources)
                cid = span_caption(span)
                if len(value) > 80 or label not in sources[cid]['text']:
                    raise ValueError('Attribute source does not explicitly mention its entity')
                attrs.append(dict(field=attr['field'], value=value, source_caption_ids=[cid]))
            except ValueError as exc:
                rejected.append(dict(kind='attribute', value=deepcopy(attr), reason=str(exc)))
        observations = []
        for ob in row['observations']:
            if ob['caption_id'] not in own:
                rejected.append(dict(kind='observation', value=deepcopy(ob), reason='Observation lacks a matching entity mention'))
                continue
            converted = dict(caption_id=ob['caption_id'], kind=ob['kind'], continuity=ob['continuity'])
            cause = ob.get('cause', {})
            if cause:
                try:
                    # Causes are requested only for a sourced change. Initial
                    # actions are events, not automatic physical state changes.
                    if depth != 'causal' or ob['continuity'] != 'changed':
                        raise ValueError('Cause was not requested for a physical change')
                    values = {k: recover_span(s, sources) for k, s in cause.items()}
                    refs = list(dict.fromkeys(span_caption(s) for s in cause.values()))
                    if not values.get('action') or any(c not in own for c in refs):
                        raise ValueError('Cause must cite its entity own mention sources')
                    converted['cause'] = dict(action=values.get('action'), actor=values.get('actor'),
                        tool=values.get('tool'), basis='inferred_action', source_caption_ids=refs)
                except ValueError as exc:
                    rejected.append(dict(kind='cause', value=deepcopy(cause), reason=str(exc)))
            observations.append(converted)
        canonical.append(dict(entity_ref=ref, label=label, source_caption_ids=own,
                              attributes=attrs, observations=observations))
    # Reuse the original scheme-4 entity/state validation. Relationship and event
    # instances are stored separately, so the old static-key merge cannot erase time.
    # Each output row can supplement the SAME existing entity from a different
    # source. Commit rows individually inside our transaction: this retains all
    # mentions without weakening base's per-row identity/source validation.
    # The outer mapping preserves original B indices, including duplicate refs.
    refs = [r['entity_ref'] for r in canonical if r['entity_ref'] != 'new']
    pages = [[row] for row in canonical] if len(refs) != len(set(refs)) else [canonical]
    request_start = len(staged['extraction_requests'])
    for page in pages:
        base.commit_delta(staged, dict(entities=page, relations=[], unresolved=report['unresolved']),
                          captions, scene_id, request, call_id)
    recorded = staged['extraction_requests'][request_start:]
    if len(recorded) > 1:
        combined = dict(recorded[-1], rejected_cause_count=sum(r['rejected_cause_count'] for r in recorded),
                        committed_pages=len(recorded))
        staged['extraction_requests'][request_start:] = [combined]
    for rel in report['relations']:
        try:
            a, b = mapping[rel['subject']], mapping[rel['object']]
            span = rel['predicate']
            predicate = recover_span(span, sources)
            cid = span_caption(span)
            if a == b or any(staged['entities'][e]['label'] not in sources[cid]['text'] for e in (a, b)):
                raise ValueError('Relation source must mention distinct endpoints')
            record_key = digest([a, b, span, rel['update']])
            if not any(r['record_key'] == record_key for r in staged['relation_records']):
                staged['relation_records'].append(dict(id=f'R{len(staged["relation_records"])}',
                    subject=a, object=b, predicate=predicate, update=rel['update'], caption_id=cid,
                    scene_id=scene_id, source_caption_ids=[cid], span=deepcopy(span),
                    record_key=record_key, status='caption_asserted'))
        except (ValueError, KeyError) as exc:
            rejected.append(dict(kind='relation', value=deepcopy(rel), reason=str(exc)))
    for event in report['events']:
        try:
            action = recover_span(event['action'], sources)
            refs = list(dict.fromkeys(event['source_caption_ids']))
            if not refs or span_caption(event['action']) not in refs:
                raise ValueError('Event must cite its own action')
            interval = [min(sources[c]['interval'][0] for c in refs), max(sources[c]['interval'][1] for c in refs)]
            if interval[1] - interval[0] > 8:
                raise ValueError('Event evidence must be a local window of at most 8 seconds')
            roles = {k: mapping[v] for k, v in event['roles'].items()}
            text = '\n'.join(sources[c]['text'] for c in refs)
            if any(staged['entities'][e]['label'] not in text for e in roles.values()):
                raise ValueError('Event role lacks its own source mention')
            conflicting = set()
            for left, right in (('actor', 'item'), ('actor', 'tool'), ('giver', 'receiver'),
                                ('giver', 'item'), ('receiver', 'item'), ('item', 'tool')):
                if left in roles and roles[left] == roles.get(right):
                    conflicting.update((left, right))
            if event['kind'] != 'transfer':
                conflicting.update(set(roles) & {'giver', 'receiver'})
            for role in sorted(conflicting):
                rejected.append(dict(kind='event_role', value=dict(role=role, entity=roles.pop(role)),
                    source_caption_ids=refs, reason='Conflicting or unrequested event-role assignment'))
            key = digest([event['kind'], event['action'], roles, event['basis']])
            previous = next((e for e in staged['events'] if e['record_key'] == key), None)
            if event['event_ref'] != 'new':
                previous = next((e for e in staged['events'] if e['id'] == event['event_ref']), None)
                if previous is None or previous['record_key'] != key:
                    raise ValueError('An event reference cannot merge different occurrences or roles')
            if previous is None:
                staged['events'].append(dict(id=f'EV{len(staged["events"])}', kind=event['kind'],
                    action=action, action_span=deepcopy(event['action']), roles=roles,
                    source_caption_ids=refs, source_support_interval=interval,
                    action_interval=interval if event['basis'] == 'caption_explicit' else None,
                    time_bounds=interval, evidence_basis=event['basis'],
                    status='caption_asserted' if event['basis'] == 'caption_explicit' else 'unresolved_hypothesis',
                    unresolved_roles=sorted(conflicting),
                    physical_identity_confirmed=False, record_key=key))
            else:
                all_refs = list(dict.fromkeys(previous['source_caption_ids'] + refs))
                span_end = max(staged['source_captions'][c]['interval'][1] for c in all_refs)
                span_start = min(staged['source_captions'][c]['interval'][0] for c in all_refs)
                if span_end-span_start > 8:
                    raise ValueError('Refill cannot join distant event evidence')
                previous.update(source_caption_ids=all_refs, source_support_interval=[span_start, span_end])
        except (ValueError, KeyError) as exc:
            rejected.append(dict(kind='event', value=deepcopy(event), reason=str(exc)))
    staged['rejected_fields'].extend(dict(r, call_id=call_id) for r in rejected)
    rebuild(staged)
    memory.clear()
    memory.update(staged)
    return dict(accepted_entities=len(canonical), rejected_fields=len(rejected))


def _intervals(cids, sources):
    merged = []
    for lo, hi in sorted({tuple(sources[c]['interval']) for c in cids}):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(hi, merged[-1][1])
        else:
            merged.append([lo, hi])
    return merged


@metered_cpu('optimized_state_projection')
def rebuild(memory):
    from .temporal_entity import canonical
    staged = deepcopy(memory)
    staged['relations'] = []
    staged['cross_matches'] = [m for m in memory['cross_matches'] if m.get('active', True)]
    # A model's repeated `same` does not fill unobserved gaps in a state episode.
    for eid in staged['entities']:
        obs = sorted((o for o in staged['observations'].values() if o['entity_id'] == eid),
                     key=lambda o: staged['source_captions'][o['caption_id']]['interval'])
        previous_end = None
        for ob in obs:
            lo, hi = staged['source_captions'][ob['caption_id']]['interval']
            if previous_end is not None and lo > previous_end and ob.get('continuity') == 'same':
                ob['continuity'] = 'uncertain'
            previous_end = max(previous_end or hi, hi)
    base.rebuild_temporal_network(staged)
    net = staged['temporal_network']
    sources = memory['source_captions']
    for s in net['states'].values():
        s['supported_intervals'] = _intervals(s['source_caption_ids'], sources)
        s['relation_ids'] = []
    # Keep raw present/end assertions independent. Compress only adjacent,
    # supported occurrences of a currently open relation episode.
    episodes = []
    for row in sorted(memory['relation_records'], key=lambda r: (sources[r['caption_id']]['interval'], r['id'])):
        a, b = canonical(net, row['subject']), canonical(net, row['object'])
        if a == b:
            continue
        lo, hi = sources[row['caption_id']]['interval']
        candidates = [r for r in episodes if (r['subject'], r['object'], r['predicate']) == (a, b, row['predicate'])
                      and r['valid_to_bounds'] is None]
        previous = candidates[-1] if candidates else None
        if row['update'] == 'end':
            if previous:
                previous['valid_to_bounds'] = [previous['supported_intervals'][-1][1], hi]
                previous['end_source_caption_ids'] = row['source_caption_ids']
            continue
        if previous and lo <= previous['supported_intervals'][-1][1]:
            previous['source_caption_ids'] = list(dict.fromkeys(previous['source_caption_ids'] + row['source_caption_ids']))
            previous['supported_intervals'] = _intervals(previous['source_caption_ids'], sources)
            previous['last_observed_at'] = hi
        else:
            episodes.append(dict(id=f'RP{len(episodes)}', subject=a, object=b, predicate=row['predicate'],
                source_caption_ids=list(row['source_caption_ids']), supported_intervals=[[lo, hi]],
                first_observed_at=lo, last_observed_at=hi, valid_from=None, valid_to=None,
                valid_from_bounds=[None, hi], valid_to_bounds=None, status='caption_asserted'))
    net['relations'] = episodes
    for s in net['states'].values():
        s['relation_ids'] = [r['id'] for r in episodes if s['entity_id'] in (r['subject'], r['object']) and
            any(a < d and c < b for a, b in s['supported_intervals'] for c, d in r['supported_intervals'])]
    memory['temporal_network'] = net
    all_matches = memory['cross_matches']
    memory['cross_matches'] = [m for m in all_matches if m.get('active', True)]
    base.rebuild_indexes(memory)
    memory['cross_matches'] = all_matches
    postings = defaultdict(list)
    for c in sources.values():
        for word in sorted(terms(c['text'])):
            postings[word].append(c['id'])
    occurrences = defaultdict(list)
    scene_entities = defaultdict(list)
    for eid, e in memory['entities'].items():
        root = canonical(net, eid)
        occurrences[root].append(dict(local_entity_id=eid, scene_id=e['scene_id'],
            caption_ids=list(e['mention_caption_ids']), identity_status='text_inferred' if root != eid else 'scene_local'))
        if root not in scene_entities[e['scene_id']]:
            scene_entities[e['scene_id']].append(root)
    memory['indexes'].update(candidate_postings=dict(postings), entity_occurrences=dict(occurrences),
                             scene_to_entities=dict(scene_entities))
    for cid, record_ in memory['indexes']['caption_to_records'].items():
        record_['event_ids'] = [e['id'] for e in memory['events'] if cid in e['source_caption_ids']]
        record_['relation_ids'] = [r['id'] for r in memory['relation_records'] if cid in r['source_caption_ids']]
    for r in memory['relation_records']:
        for eid in (r['subject'], r['object']):
            refs = memory['indexes']['entity_to_captions'][eid]
            refs[:] = sorted(set(refs + r['source_caption_ids']), key=lambda c: sources[c]['interval'])
    for e in memory['events']:
        for eid in e['roles'].values():
            refs = memory['indexes']['entity_to_captions'][eid]
            refs[:] = sorted(set(refs + e['source_caption_ids']), key=lambda c: sources[c]['interval'])


def revoke_identity(memory, match_id, reason):
    staged = deepcopy(memory)
    match = next(m for m in staged['cross_matches'] if m['id'] == match_id)
    match.update(active=False, revoked_reason=reason)
    staged['identity_revision'] += 1
    rebuild(staged)
    memory.clear()
    memory.update(staged)


def align_needed(memory, scene_id, query, gap, meter, entity_ids=()):
    from .scene_memory import validate_matches
    from .temporal_entity import canonical
    words = terms(query['question'] + ' ' + gap)
    def rank(ids):
        return sorted(ids, key=lambda e: (-(2*(e in entity_ids) + len(words & terms(memory['entities'][e]['label']))), e))[:3]
    left = rank([eid for eid, e in memory['entities'].items() if e['scene_id'] == scene_id])
    right = rank([eid for eid, e in memory['entities'].items() if e['scene_id'] != scene_id])
    changed = False
    pairs = sorted(((a, b) for a in left for b in right), key=lambda p: (
        -len(terms(memory['entities'][p[0]]['label']) & terms(memory['entities'][p[1]]['label'])), p))[:3]
    for a, b in pairs:
        net = memory['temporal_network']
        if canonical(net, a) == canonical(net, b):
            continue
        def graph(eid):
            e = memory['entities'][eid]
            refs = e['mention_caption_ids']
            return dict(entities=[dict(id=eid, label=e['label'], caption_ids=refs)],
                captions=[memory['source_captions'][c] for c in refs])
        l, r = graph(a), graph(b)
        key = digest(dict(pair=sorted((a, b)), sources=sorted((digest(l), digest(r))),
                          scope='identity_and_material_lineage', version=IDENTITY_VERSION,
                          revision=memory['identity_revision']))
        if key in memory['alignment_requests']:
            continue
        # Question/gap select the pair; the cached verification task itself has
        # a stable scope, independent of question wording.
        report, _ = meter.json('scene_correspondence_lineage', dict(left=l, right=r,
            gap='Determine same/different physical instance or material lineage for this selected pair; unknown is allowed.'), ('matches',))
        matches = validate_matches(report, l, r)
        # Do not let pairwise calls silently create competing global matches
        # for two distinct local instances from the same scene.
        for match in matches:
            if match['decision'] == 'same':
                ca, cb = canonical(net, a), canonical(net, b)
                if set(net['entities'][ca]['scene_ids']) & set(net['entities'][cb]['scene_ids']):
                    match.update(decision='unknown', relation='unknown', status='unresolved', reason='competing local instances')
            match.update(id=f'I{len(memory["cross_matches"])}', active=True, request_key=key)
            memory['cross_matches'].append(match)
            changed = True
        memory['alignment_requests'].append(key)
        rebuild(memory)
    return changed


def context(memory, query, seen, coverage):
    """Project only records whose sources have actually been read this query."""
    from .temporal_entity import canonical
    seen = set(seen)
    visible = deepcopy(memory)
    visible['cross_matches'] = [m for m in memory['cross_matches'] if set(m['caption_ids']) <= seen]
    rebuild(visible)
    net = visible['temporal_network']
    # All selected sources are supplied, so an ordinal event cannot be silently
    # dropped by a generic top-N relevance truncation. QA checks the actual size.
    states = [deepcopy(s) for s in net['states'].values() if set(s['source_caption_ids']) <= seen]
    events = [deepcopy(e) for e in memory['events'] if set(e['source_caption_ids']) <= seen]
    ids = {s['entity_id'] for s in states}
    for event in events:
        event['local_roles'] = dict(event['roles'])
        event['roles'] = {k: canonical(net, v) for k, v in event['roles'].items()}
        ids.update(event['roles'].values())
        # These are temporal associations, never a new causal assertion.
        role_ids = set(event['roles'].values())
        lo, hi = event['time_bounds']
        event['before_state_refs'] = [s['id'] for s in states if s['entity_id'] in role_ids and s['last_observed_at'] <= lo]
        event['after_state_refs'] = [s['id'] for s in states if s['entity_id'] in role_ids and s['first_observed_at'] >= hi]
        event['state_link_basis'] = 'temporal_candidates_not_cause'
        event['identity_evidence_refs'] = [m['id'] for m in visible['cross_matches'] if m.get('active', True) and
            any(canonical(net, m[k]) in role_ids for k in ('left_entity', 'right_entity'))]
    entities = [dict(id=eid, label=net['entities'][eid]['label'],
        attributes=[a for a in net['entities'][eid]['attributes'] if set(a['source_caption_ids']) <= seen]) for eid in sorted(ids)]
    state_ids = {s['id'] for s in states}
    relations = [deepcopy(r) for r in net['relations'] if set(r['source_caption_ids']) <= seen and
                 r['subject'] in ids and r['object'] in ids]
    return dict(source_type='caption', entities=entities, states=states,
        transitions=[t for t in net['transitions'] if t['before'] in state_ids and t['after'] in state_ids
                     and set(t['cause']['source_caption_ids']) <= seen],
        relations=relations, events=sorted(events, key=lambda e: (e['time_bounds'], e['id'])),
        identities=[deepcopy(m) for m in visible['cross_matches'] if m.get('active', True) and
                    canonical(net, m['left_entity']) in ids and canonical(net, m['right_entity']) in ids],
        lineage=[r for r in net['lineage'] if set(r['source_caption_ids']) <= seen and r['child'] in ids and r['parent'] in ids],
        context_selection=dict(partial_memory=True, selected_entities=len(ids), selected_states=len(states),
            selected_events=len(events), ordinal_completeness=bool(coverage['scope'] == 'exhaustive' and coverage['met']),
            temporal_coverage=coverage),
        evidence_policy='Caption assertions and revisable text identity inferences. Events are source instances; repeated captions are not automatic event counts. Unsupported transfer hypotheses are unresolved. Missing retrieval is not absence. Unknown gaps do not establish persistence. Ordinal/count/last claims require coverage and unambiguous event identity/order.')
