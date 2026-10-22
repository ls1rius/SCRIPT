"""Sparse source-backed memory anchors and bounded hierarchical scene navigation."""
from collections import Counter, defaultdict, deque
from copy import deepcopy
import math

from .accounting import metered_cpu
from .budget import ToolFailure, ResourceExhausted
from .config import digest
from .summary_graph import grounded_label, words, STOP


KINDS = {'state_change', 'relocation_transfer', 'phase_change', 'exception',
         'identity_attribute', 'goal_decision', 'split_merge'}


def terms(text):
    return set(words(text))-STOP-{'person', 'frame', 'second', 'seconds'}


def embed_batches(meter, texts, size=16):
    result = []
    for start in range(0, len(texts), size):
        out, _ = meter.call('embed', {'texts': texts[start:start+size]}, build=True)
        result.extend(out['vectors'])
    return result


@metered_cpu('key_anchor_assembly')
def assemble_keys(directory, reports, failures=()):
    sources = {c['id']: c for c in directory['captions']}
    ownership = {c: s['id'] for s in directory['scenes'] for c in s['caption_ids']}
    cards, seen, rejected = [], set(), []
    for report in reports:
        for row in report['keys']:
            before = list(dict.fromkeys(row['before_caption_ids']))
            after = list(dict.fromkeys(row['after_caption_ids']))
            refs = set(before+after)
            if row['kind'] not in KINDS or not after or not refs <= sources.keys():
                rejected.append('invalid_kind_or_source')
                continue
            if before and (set(before) & set(after) or max(sources[c]['interval'][0] for c in before)
                           >= min(sources[c]['interval'][0] for c in after)):
                rejected.append('invalid_before_after_order')
                continue
            selected = sorted((sources[c] for c in refs), key=lambda c: c['interval'])
            raw = '\n'.join(c['text'] for c in selected)
            labels = [s for s in row['entity_terms'] if grounded_label(s, raw)]
            signature = digest([row['kind'], sorted(refs), labels])
            if signature in seen:
                continue
            seen.add(signature)
            cards.append({'id': directory['video_id']+':key:'+signature[:16], 'kind': row['kind'],
                'description': row['description'], 'entity_terms': labels,
                'before_caption_ids': before, 'after_caption_ids': after,
                'interval': [selected[0]['interval'][0], max(c['interval'][1] for c in selected)],
                'scene_ids': list(dict.fromkeys(ownership[c['id']] for c in selected)),
                'source_captions': deepcopy(selected), 'status': 'caption_hint',
                'pre_state_known': bool(before), 'last_observed_at': max(sources[c]['interval'][1] for c in after),
                'state_valid_until': None, 'physical_identity_confirmed': False})
    cards.sort(key=lambda c: (c['interval'], c['id']))
    return cards, {'failed_batches': list(failures), 'rejected_keys': dict(Counter(rejected)),
        'accepted_keys': len(cards), 'identity_or_relation_graph_prebuilt': False}


def key_text(card):
    return ' '.join([card['kind'], card['description'], *card['entity_terms'],
        *(c['text'] for c in card['source_captions'])])


@metered_cpu('key_directory_control')
def augment_directory(directory, cfg, meter):
    settings = cfg['scene_memory']
    captions = directory['captions']
    reports, failures = [], []
    size = settings['key_batch_captions']
    for start in range(0, len(captions), size):
        # Two prior captions preserve transitions at batch boundaries, without future lookahead.
        batch = captions[max(0, start-2):start+size]
        try:
            report, _ = meter.json('scene_key_information', {'captions': batch,
                'max_keys': settings['max_keys_per_batch']}, ('keys',), build=True)
            reports.append(report)
        except (ToolFailure, ResourceExhausted) as exc:
            failures.append({'batch_start': start, 'error': str(exc)})
    cards, diagnostics = assemble_keys(directory, reports, failures)
    texts = [key_text(c) for c in cards]
    vectors = embed_batches(meter, texts)
    postings = defaultdict(list)
    for card, text in zip(cards, texts):
        for term in sorted(terms(text)):
            postings[term].append(card['id'])
    directory['key_memory'] = {'schema_version': 1, 'records': cards, 'vectors': vectors,
        'positions': {c['id']: i for i,c in enumerate(cards)},
        'postings': dict(postings), 'records_hash': digest(cards), 'build_diagnostics': diagnostics}
    scene_lookup = {s['id']: s for s in directory['scenes']}
    for s in directory['scenes']:
        s['key_ids'] = []
    pairs = {(p['source'], p['target']): p for p in directory['pointers']}
    for card in cards:
        for sid in card['scene_ids']:
            scene_lookup[sid]['key_ids'].append(card['id'])
        for a, b in zip(card['scene_ids'], card['scene_ids'][1:]):
            if (a, b) not in pairs:
                left, right = scene_lookup[a], scene_lookup[b]
                p = {'id': f'{a}->{b}', 'source': a, 'target': b, 'kind': 'key_transition_candidate',
                    'description': f'Earlier phase: {left["description"]} Later phase: {right["description"]}',
                    'gap_seconds': right['interval'][0]-left['interval'][1], 'similarity': 0.,
                    'basis': 'caption_key_spanning_scene_boundary',
                    'transition_status': 'directory_hint_not_observed_transition', 'entity_correspondences': []}
                directory['pointers'].append(p)
                pairs[a, b] = p
            pairs[a, b].setdefault('key_ids', []).append(card['id'])
    return directory


def validate_keys(directory):
    memory = directory.get('key_memory')
    if not memory:
        return
    cards = memory['records']
    sources = {c['id']: c for c in directory['captions']}
    ownership = {c: s['id'] for s in directory['scenes'] for c in s['caption_ids']}
    lookup = {c['id']: c for c in cards}
    if len(lookup) != len(cards) or len(cards) != len(memory['vectors']) or digest(cards) != memory['records_hash']:
        raise ValueError('Key index integrity mismatch')
    if memory['positions'] != {c['id']: i for i,c in enumerate(cards)}:
        raise ValueError('Key record positions changed')
    expected_postings = defaultdict(list)
    expected_scene_keys = defaultdict(list)
    for card in cards:
        refs = set(card['before_caption_ids']+card['after_caption_ids'])
        expected = sorted((sources[c] for c in refs), key=lambda c: c['interval'])
        if (card['source_captions'] != expected or card['status'] != 'caption_hint'
                or card['state_valid_until'] is not None or card['physical_identity_confirmed']
                or card['pre_state_known'] != bool(card['before_caption_ids'])
                or card['last_observed_at'] != max(sources[c]['interval'][1] for c in card['after_caption_ids'])
                or card['scene_ids'] != list(dict.fromkeys(ownership[c['id']] for c in expected))):
            raise ValueError('Key anchor invented sources, identity or state persistence')
        if card['interval'] != [expected[0]['interval'][0], max(c['interval'][1] for c in expected)]:
            raise ValueError('Key anchor interval mismatch')
        for term in sorted(terms(key_text(card))):
            expected_postings[term].append(card['id'])
        for sid in card['scene_ids']:
            expected_scene_keys[sid].append(card['id'])
    if dict(expected_postings) != memory['postings']:
        raise ValueError('Key postings differ from source-backed records')
    for scene in directory['scenes']:
        if scene.get('key_ids') != expected_scene_keys[scene['id']]:
            raise ValueError('Scene key references changed')


def bounded_pointer_targets(scenes, vectors, count, limit):
    """Bound candidate comparisons using recent history, text terms and coarse buckets."""
    from .scene_memory import cosine
    recent = deque(maxlen=max(1, limit//2))
    buckets, names = defaultdict(lambda: deque(maxlen=limit)), defaultdict(lambda: deque(maxlen=8))
    comparisons = 0
    for j, scene in enumerate(scenes):
        vector = vectors[j]
        bucket = tuple(x >= 0 for x in vector[:8])
        labels = sorted(set(scene['entity_terms']))
        candidates = []
        for label in labels:
            candidates.extend(reversed(names[label]))
        candidates.extend(reversed(buckets[bucket]))
        candidates.extend(reversed(recent))
        candidates = list(dict.fromkeys(i for i in candidates if i < j-1))[:limit]
        comparisons += len(candidates)
        ranked = sorted(candidates, key=lambda i: (-cosine(vectors[i], vector), i))
        yield j, ranked[:count], comparisons
        recent.append(j)
        buckets[bucket].append(j)
        for label in labels:
            names[label].append(j)


@metered_cpu('scene_hierarchy_assembly')
def build_hierarchy(directory, cfg, meter):
    settings = cfg['scene_memory']
    fanout = settings['hierarchy_fanout']
    nodes = {s['id']: {'id': s['id'], 'interval': s['interval'], 'children': [], 'leaf': True,
        'navigation_excerpt': s['description'], 'entity_terms': s['entity_terms'], 'vector': v}
        for s, v in zip(directory['scenes'], directory['vectors'])}
    level = [s['id'] for s in directory['scenes']]
    depth = 0
    while len(level) > 1:
        depth += 1
        parents, texts = [], []
        frequency = Counter(t for sid in level for t in nodes[sid]['entity_terms'])
        for start in range(0, len(level), fanout):
            children = level[start:start+fanout]
            labels = sorted({t for c in children for t in nodes[c]['entity_terms']}, key=lambda t: (frequency[t], t))[:16]
            # This excerpt is only a navigation aid. All keys and source text stay separate and intact.
            excerpt = ' | '.join(nodes[c]['navigation_excerpt'] for c in children)[:900]
            node = {'id': f'{directory["video_id"]}:directory:{depth}:{start//fanout}',
                'interval': [nodes[children[0]]['interval'][0], nodes[children[-1]]['interval'][1]],
                'children': children, 'leaf': False, 'navigation_excerpt': excerpt, 'entity_terms': labels}
            parents.append(node)
            texts.append(excerpt+' '+' '.join(labels))
        vectors = embed_batches(meter, texts)
        for p, vector in zip(parents, vectors):
            p['vector'] = vector
            nodes[p['id']] = p
        level = [p['id'] for p in parents]
    directory['hierarchy'] = {'root': level[0], 'nodes': nodes, 'depth': depth, 'fanout': fanout,
        'key_records_stored_separately': True}
    return directory


def validate_hierarchy(directory):
    hierarchy = directory.get('hierarchy')
    if hierarchy is None:
        return
    nodes, seen, leaves = hierarchy['nodes'], set(), []
    def walk(uid):
        if uid in seen:
            raise ValueError('Directory hierarchy cycle or repeated child')
        seen.add(uid)
        node = nodes[uid]
        if node['id'] != uid or len(node['children']) > hierarchy['fanout']:
            raise ValueError('Hierarchy node mismatch or unbounded fanout')
        if node['leaf']:
            if node['children']:
                raise ValueError('Leaf directory node has children')
            leaves.append(uid)
        else:
            if not node['children']:
                raise ValueError('Empty hierarchy parent')
            for child in node['children']:
                walk(child)
    walk(hierarchy['root'])
    if leaves != [s['id'] for s in directory['scenes']] or seen != set(nodes):
        raise ValueError('Hierarchy omitted or duplicated scenes')


def opened_keys(directory, opened):
    available = {c for s in directory['scenes'] if s['id'] in opened for c in s['caption_ids']}
    memory = directory.get('key_memory', {})
    ids = {k for s in directory['scenes'] if s['id'] in opened for k in s.get('key_ids', [])}
    cards = [memory['records'][memory['positions'][uid]] for uid in ids]
    return sorted((c for c in cards if set(c['before_caption_ids']+c['after_caption_ids']) <= available),
                  key=lambda c: (c['interval'], c['id']))


def rank_hierarchy(directory, query_text, query_vector, scope, cfg, meter):
    nodes = directory['hierarchy']['nodes']
    active = [directory['hierarchy']['root']]
    leaves, calls = [], []
    while active:
        candidates = []
        for uid in active:
            node = nodes[uid]
            if node['leaf']:
                leaves.append(uid)
            else:
                candidates.extend(node['children'])
        if not candidates:
            break
        documents = [{'id': uid, 'interval': nodes[uid]['interval'],
            'text': nodes[uid]['navigation_excerpt']+' '+' '.join(nodes[uid]['entity_terms'])} for uid in candidates]
        out, _ = meter.call('retrieve', {'documents': documents, 'vectors': [nodes[uid]['vector'] for uid in candidates],
            'query_text': query_text, 'query_vector': query_vector, 'scope': scope, 'cfg': cfg['retrieval']})
        active = [r['id'] for r in out['data'][:cfg['scene_memory']['hierarchy_beam']]]
        calls.append({'candidate_nodes': len(candidates), 'selected_nodes': list(active)})
    return list(dict.fromkeys(leaves)), calls


def key_candidates(directory, query_text, maximum):
    memory = directory.get('key_memory', {})
    postings = memory.get('postings', {})
    scores = Counter()
    # Visit selective postings first; avoid walking an hours-long common-word posting list.
    query_terms = sorted((t for t in terms(query_text) if t in postings), key=lambda t: (len(postings[t]), t))
    remaining = maximum*8
    touched = 0
    for term in query_terms:
        ids = postings[term]
        if remaining <= 0:
            break
        # Spread capped samples over the whole history rather than only its ending.
        take = min(len(ids), maximum, remaining)
        chosen = ids if take == len(ids) else [ids[round(i*(len(ids)-1)/max(1, take-1))] for i in range(take)]
        for uid in chosen:
            scores[uid] += math.log(2+len(memory.get('records', []))/max(1, len(ids)))
        remaining -= take
        touched += take
    return [uid for uid,_ in scores.most_common(maximum)], touched


def rank_keys(directory, query_text, query_vector, scope, cfg, meter):
    memory = directory.get('key_memory', {})
    ids, touched = key_candidates(directory, query_text, cfg['scene_memory']['key_candidate_limit'])
    if not ids:
        return [], {'candidates': 0, 'postings_visited': touched}
    positions = memory['positions']
    cards = [memory['records'][positions[uid]] for uid in ids]
    out, _ = meter.call('retrieve', {'documents': [{'id': c['id'], 'interval': c['interval'], 'text': key_text(c)} for c in cards],
        'vectors': [memory['vectors'][positions[uid]] for uid in ids], 'query_text': query_text,
        'query_vector': query_vector, 'scope': scope, 'cfg': cfg['retrieval']})
    lookup = {c['id']: c for c in cards}
    scenes = []
    for row in out['data']:
        for sid in lookup[row['id']]['scene_ids']:
            if sid not in scenes:
                scenes.append(sid)
    return scenes, {'candidates': len(ids), 'postings_visited': touched, 'ranked_key_ids': [r['id'] for r in out['data']]}


def fuse_rankings(scene_ranking, key_ranking):
    scores = Counter()
    for ranking in (scene_ranking, key_ranking):
        for rank, uid in enumerate(ranking, 1):
            scores[uid] += 1/(60+rank)
    return sorted(scores, key=lambda uid: (-scores[uid], uid))
