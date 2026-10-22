"""Question-independent caption assertions, separate from pixel-verified Memory."""
from collections import Counter
from copy import deepcopy
import math
import re

from .config import digest
from .accounting import metered_cpu


def source_records(spans, cutoff):
    """Never relabel a description crossing a cutoff as an in-scope statement."""
    result = []
    for span in spans:
        start, end = span['start'], span['end']
        if not (type(start) in (int, float) and type(end) in (int, float)
                and math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= cutoff):
            continue
        result.append({'id': f'TS{len(result)+1}', 'interval': [start, end], 'text': span['text']})
    return result


def extraction_inputs(captions):
    # Deliberately no question, options, labels, historical predictions or images.
    return {'captions': deepcopy(captions), 'max_events': 10}


def words(text):
    return re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', text.casefold())


def grounded_label(label, text):
    tokens = words(label)
    return bool(tokens) and set(tokens) <= set(words(text))


@metered_cpu('caption_graph_assembly')
def assemble_graph(video_id, caption_hash, captions, reports, failures=()):
    """Assign IDs/times in code; reject ungrounded roles and distant event merges.

    Entity IDs denote shared *text labels*, never confirmed physical identity.
    All event descriptions are copied source text, not generated paraphrases.
    """
    sources = {c['id']: c for c in captions}
    entities, events, rejected, dropped_roles, seen = {}, [], [], [], set()
    for report in reports:
        for row in report.get('events', []):
            ids = list(dict.fromkeys(row.get('caption_ids', [])))
            if not ids or not set(ids) <= sources.keys():
                rejected.append('unknown_or_empty_caption_reference')
                continue
            selected = sorted((sources[c] for c in ids), key=lambda c: c['interval'])
            interval = [selected[0]['interval'][0], max(c['interval'][1] for c in selected)]
            if interval[1] - interval[0] > 8:
                rejected.append('nonlocal_event_merge')
                continue
            text = '\n'.join(c['text'] for c in selected)
            roles = {}
            for role in ('actor', 'object', 'instrument', 'location', 'hand'):
                label = row.get(role, '').strip()
                if not label:
                    continue
                if not grounded_label(label, text):
                    dropped_roles.append({'role': role, 'label': label, 'caption_ids': ids})
                    continue
                kind = 'actor' if role == 'actor' else 'hand' if role == 'hand' else 'object'
                key = (kind, ' '.join(words(label)))
                if key not in entities:
                    entities[key] = {'id': video_id+':text_entity:'+digest(key)[:12],
                        'label': label, 'kind': kind, 'identity_status': 'text_label_only',
                        'status': 'caption_asserted', 'caption_ids': []}
                entity = entities[key]
                entity['caption_ids'] = sorted(set(entity['caption_ids']+ids), key=lambda x: sources[x]['interval'])
                roles[role] = entity['id']
            action = row.get('action', '').strip()
            # An ungrounded action name cannot introduce a new event assertion.
            if not grounded_label(action, text):
                action = 'source_description'
            key = digest([sorted(ids), action, roles])
            if key in seen:
                continue
            seen.add(key)
            events.append({'id': video_id+':text_event:'+key[:16], 'action': action,
                'roles': roles, 'interval': interval, 'caption_ids': [c['id'] for c in selected],
                'description': text, 'status': 'caption_asserted', 'source_type': 'caption'})
    events.sort(key=lambda e: (e['interval'], e['id']))
    edges = []
    for event in events:
        following = next((e for e in events if e['interval'][0] >= event['interval'][1]), None)
        if following:
            edges.append({'before': event['id'], 'after': following['id'],
                          'basis': 'source_timestamps_only', 'causal': False})
    used = {c for e in events for c in e['caption_ids']}
    return {'schema_version': 1, 'video_id': video_id, 'caption_hash': caption_hash,
        'source_records_hash': digest(captions), 'status': 'caption_asserted',
        'entities': list(entities.values()), 'events': events, 'temporal_edges': edges,
        'captions': deepcopy(captions), 'build_diagnostics': {'failures': list(failures),
            'rejected_events': dict(Counter(rejected)), 'dropped_roles': dropped_roles,
            'source_spans': len(captions), 'represented_source_spans': len(used)}}


@metered_cpu('caption_graph_validation')
def validate_graph(graph, manifest):
    if (graph.get('schema_version') != 1 or graph.get('status') != 'caption_asserted'
            or graph.get('video_id') != manifest['video_id']
            or graph.get('caption_hash') != manifest.get('caption_hash')):
        raise ValueError('Caption graph source/video mismatch')
    captions = graph['captions']
    if manifest.get('caption_uri'):
        from .video import load_captions
        expected_captions = source_records(load_captions(manifest)['data'], manifest['cutoff'])
        if captions != expected_captions:
            raise ValueError('Caption graph differs from the original provided descriptions')
    if digest(captions) != graph['source_records_hash']:
        raise ValueError('Caption graph source records changed')
    sources = {c['id']: c for c in captions}
    entities = {e['id']: e for e in graph['entities']}
    events = {e['id']: e for e in graph['events']}
    if len(events) != len(graph['events']) or len(sources) != len(captions) or len(entities) != len(graph['entities']):
        raise ValueError('Duplicate caption/event IDs')
    for event in events.values():
        cited = [sources[c] for c in event['caption_ids']]
        if not cited or event['status'] != 'caption_asserted' or event['source_type'] != 'caption':
            raise ValueError('Invalid caption event status')
        if not set(event['roles'].values()) <= entities.keys():
            raise ValueError('Unknown text entity')
        expected = [min(c['interval'][0] for c in cited), max(c['interval'][1] for c in cited)]
        if event['interval'] != expected or expected[1]-expected[0] > 8:
            raise ValueError('Event time is not supported by original descriptions')
        if event['description'] != '\n'.join(c['text'] for c in sorted(cited, key=lambda c: c['interval'])):
            raise ValueError('Caption event text changed')
        for role, entity_id in event['roles'].items():
            entity = entities[entity_id]
            if entity['identity_status'] != 'text_label_only' or not grounded_label(entity['label'], event['description']):
                raise ValueError('Ungrounded/confirmed text entity')
    for edge in graph['temporal_edges']:
        if edge.get('causal') is not False or events[edge['before']]['interval'][1] > events[edge['after']]['interval'][0]:
            raise ValueError('Invalid temporal ordering')
    return graph


STOP = set('a an the is are was were be to of in on at by for and or then with using it its this that as from into c he she they what how which throughout video entire main primary overall'.split())


@metered_cpu('caption_graph_retrieval')
def retrieve_graph(graph, query, maximum=12):
    """Return scope-safe matching events with exact quotes and temporal neighbors."""
    if not graph or not graph.get('events'):
        return None
    from .query_policy import option_distinctions
    term = lambda text: set(words(text))-STOP
    question_terms = term(query['question'])
    distinctions = option_distinctions(query.get('options', []))
    option_terms = Counter(w for row in distinctions for w in term(row['difference_text']))
    scope = query['scope']
    events = [e for e in graph['events'] if scope[0] <= e['interval'][0] and e['interval'][1] <= scope[1]]
    frequencies = Counter(w for e in events for w in term(e['description']+' '+e['action']))
    def relevance(event):
        return sum((int(w in question_terms)+2*int(w in option_terms)/max(1, option_terms[w]))
                   * math.log(1+len(events)/(1+frequencies[w]))
                   for w in term(event['description']+' '+event['action']))
    ranked = [(relevance(e), e) for e in events]
    ranked.sort(key=lambda x: (-x[0], x[1]['interval'], x[1]['id']))
    selected = {e['id']: e for score, e in ranked[:max(1, maximum-4)] if score > 0}
    if not selected:
        return None
    # Neighbors retain the phase around a matching event without merging repeats.
    for _, event in ranked:
        if event['id'] not in selected:
            continue
        i = events.index(event)
        for neighbor in events[max(0, i-1):i+2]:
            if len(selected) < maximum:
                selected.setdefault(neighbor['id'], neighbor)
        if len(selected) >= maximum:
            break
    ids = set(selected)
    entity_ids = {r for e in selected.values() for r in e['roles'].values()}
    caption_ids = {c for e in selected.values() for c in e['caption_ids']}
    entity_rows = [deepcopy(e) for e in graph['entities'] if e['id'] in entity_ids]
    for entity in entity_rows:
        entity['caption_ids'] = [c for c in entity['caption_ids'] if c in caption_ids]
    event_rows = sorted(deepcopy(list(selected.values())), key=lambda e: (e['interval'], e['id']))
    for event in event_rows:
        event['retrieval_score'] = relevance(event)
    return {'status': 'unverified_caption_assertions', 'entities': entity_rows,
        'events': event_rows,
        'temporal_edges': [deepcopy(e) for e in graph['temporal_edges'] if e['before'] in ids and e['after'] in ids],
        'total_in_scope_events': len(events), 'selected_events': len(selected)}


def promote_windows(ranked, context, memory):
    """Prefer existing windows containing distinguishing event midpoints.

    The mandatory coverage checkpoints remain unchanged. Only the subsequent
    ranked follow-ups use this order; captions never satisfy a visual proof.
    """
    if not context:
        return ranked
    events = context['events']
    def score(row):
        interval = memory.get(row['id'])['interval']
        return sum(e.get('retrieval_score', 0) for e in events
                   if interval[0] <= sum(e['interval'])/2 < interval[1])
    positions = {r['id']: i for i, r in enumerate(ranked)}
    return sorted(ranked, key=lambda r: (-score(r), positions[r['id']]))
