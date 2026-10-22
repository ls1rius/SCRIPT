"""Caption-preserving scene directory and query-lazy local/cross-scene graphs."""
from copy import deepcopy
import json
import math
from pathlib import Path

from .accounting import metered_cpu
from .budget import ToolFailure, ResourceExhausted
from .config import digest
from .state import obj, write_json
from .summary_graph import source_records, assemble_graph, extraction_inputs, grounded_label


def caption_sources(manifest):
    from .video import load_captions
    return source_records(load_captions(manifest)['data'], manifest['cutoff'])


def cosine(left, right):
    return sum(a*b for a, b in zip(left, right))/max(1e-12,
        math.sqrt(sum(a*a for a in left)*sum(b*b for b in right)))


@metered_cpu('scene_directory_assembly')
def assemble_directory(video_id, caption_hash, captions, report, cfg, fallback_reason=None):
    """No entities/events/identity graphs are built at directory time."""
    settings = cfg['scene_memory']
    positions = {c['id']: i for i, c in enumerate(captions)}
    if not captions or len(positions) != len(captions):
        raise ValueError('Scene directory requires distinct source captions')
    phases, cursor = [], 0
    rows = list(report['scenes'])
    if 'final_scene' in report:
        rows.append(report['final_scene'])
    for row in rows:
        end = positions[row['end_caption_id']]+1
        if end <= cursor:
            raise ValueError('Scene directory boundaries are not increasing')
        phases.append((captions[cursor:end], row))
        cursor = end
    if cursor != len(captions):
        raise ValueError('Scene directory must retain every original caption')
    scenes = []
    for phase, row in phases:
        # A model cannot hide the whole video inside one supposedly local scene.
        parts, part = [], []
        for caption in phase:
            if part and caption['interval'][1]-part[0]['interval'][0] > settings['max_scene_seconds']:
                parts.append(part)
                part = []
            part.append(caption)
        if part:
            parts.append(part)
        for part in parts:
            raw = '\n'.join(c['text'] for c in part)
            scenes.append({'id': f'{video_id}:phase:{len(scenes)+1:03d}',
                'interval': [part[0]['interval'][0], part[-1]['interval'][1]],
                'description': row['description'],
                'entity_terms': [t for t in row.get('entity_terms', []) if grounded_label(t, raw)],
                'caption_ids': [c['id'] for c in part], 'status': 'directory_only'})
    return {'schema_version': 1, 'video_id': video_id, 'caption_hash': caption_hash,
        'source_records_hash': digest(captions), 'captions': deepcopy(captions), 'scenes': scenes,
        'pointers': [], 'vectors': [], 'build_diagnostics': {'fallback_reason': fallback_reason,
            'split_oversized_phases': sum(len(p) > 0 for p, _ in phases) < len(scenes)}}


def add_pointers(directory, vectors, count, cfg=None):
    """Sparse forward edges; reverse traversal uses the same edge, not new claims."""
    scenes = directory['scenes']
    if len(vectors) != len(scenes) or any(not v or not all(math.isfinite(x) for x in v) for v in vectors):
        raise ValueError('Scene vectors do not match directory')
    directory['vectors'] = vectors
    if cfg and cfg['scene_memory']['hierarchical_directory']:
        from .key_memory import bounded_pointer_targets
        pairs = {(i, i+1): 'temporal_next' for i in range(len(scenes)-1)}
        comparisons = 0
        for j, earlier, comparisons in bounded_pointer_targets(scenes, vectors, count,
                cfg['scene_memory']['pointer_candidate_limit']):
            for i in earlier:
                pairs[i, j] = 'semantic_candidate'
        directory['build_diagnostics']['semantic_pointer_comparisons'] = comparisons
    else:
        pairs = {}
        for i in range(len(scenes)):
            if i+1 < len(scenes):
                pairs[i, i+1] = 'temporal_next'
            candidates = sorted(range(i+2, len(scenes)), key=lambda j: (-cosine(vectors[i], vectors[j]), j))
            for j in candidates[:count]:
                pairs[i, j] = 'semantic_candidate'
    grouped = {}
    for (i, j), kind in pairs.items():
        grouped.setdefault(i, []).append((j, kind))
    for i, left in enumerate(scenes):
        for j, kind in sorted(grouped.get(i, [])):
            right = scenes[j]
            directory['pointers'].append({'id': f'{left["id"]}->{right["id"]}',
                'source': left['id'], 'target': right['id'], 'kind': kind,
                'description': f'Earlier phase: {left["description"]} Later phase: {right["description"]}',
                'gap_seconds': right['interval'][0]-left['interval'][1],
                'similarity': cosine(vectors[i], vectors[j]),
                'basis': 'caption_phase_descriptions_and_timestamps',
                'transition_status': 'directory_hint_not_observed_transition',
                'entity_correspondences': []})
    return directory


def partition_captions(manifest, captions, cfg, meter):
    reason = None
    try:
        duration = captions[-1]['interval'][1]-captions[0]['interval'][0]
        report, _ = meter.json('scene_directory', {'captions': captions,
            'terminal_caption_id': captions[-1]['id'],
            'max_scenes': min(8, max(1, math.ceil(duration/30.))),
            'max_scene_seconds': cfg['scene_memory']['max_scene_seconds']},
            ('scenes', 'final_scene'), build=True)
        directory = assemble_directory(manifest['video_id'], manifest['caption_hash'], captions, report, cfg)
    except (ToolFailure, ValueError, KeyError) as exc:
        # Explicit temporal fallback preserves all text and is reported, never hidden.
        reason = str(exc)
        groups, part = [], []
        for c in captions:
            if part and c['interval'][1]-part[0]['interval'][0] > cfg['scene_memory']['fallback_scene_seconds']:
                groups.append(part)
                part = []
            part.append(c)
        if part:
            groups.append(part)
        report = {'scenes': [{'end_caption_id': p[-1]['id'],
            'description': f"Source descriptions at {p[0]['interval'][0]:g}-{p[-1]['interval'][1]:g}s: "+p[0]['text'],
            'entity_terms': []} for p in groups]}
        directory = assemble_directory(manifest['video_id'], manifest['caption_hash'], captions, report, cfg, reason)
    return directory


@metered_cpu('scene_directory_control')
def build_directory(manifest, cfg, meter):
    from .key_memory import embed_batches, augment_directory, build_hierarchy
    captions = caption_sources(manifest)
    if cfg['scene_memory']['hierarchical_directory']:
        size = cfg['scene_memory']['directory_block_captions']
        rows, failures = [], []
        for start in range(0, len(captions), size):
            block = partition_captions(manifest, captions[start:start+size], cfg, meter)
            rows.extend({'end_caption_id': s['caption_ids'][-1], 'description': s['description'],
                         'entity_terms': s['entity_terms']} for s in block['scenes'])
            if block['build_diagnostics']['fallback_reason']:
                failures.append({'block_start': start, 'reason': block['build_diagnostics']['fallback_reason']})
        directory = assemble_directory(manifest['video_id'], manifest['caption_hash'], captions,
            {'scenes': rows}, cfg, failures or None)
        directory['build_diagnostics']['partition_block_count'] = math.ceil(len(captions)/size)
    else:
        directory = partition_captions(manifest, captions, cfg, meter)
    by_id = {c['id']: c for c in captions}
    # Retrieval embeddings retain the original phase descriptions, including late details.
    texts = ['\n'.join(by_id[c]['text'] for c in s['caption_ids']) for s in directory['scenes']]
    if cfg['scene_memory']['hierarchical_directory']:
        vectors = embed_batches(meter, texts)
    else:
        embedded, _ = meter.call('embed', {'texts': texts}, build=True)
        vectors = embedded['vectors']
    add_pointers(directory, vectors, cfg['scene_memory']['semantic_links_per_scene'], cfg)
    if cfg['scene_memory']['key_information']:
        augment_directory(directory, cfg, meter)
    if cfg['scene_memory']['hierarchical_directory']:
        build_hierarchy(directory, cfg, meter)
    return directory


def validate_directory(directory, manifest, captions=None):
    captions = caption_sources(manifest) if captions is None else captions
    if (directory['schema_version'] != 1 or directory['video_id'] != manifest['video_id']
            or directory['caption_hash'] != manifest['caption_hash']
            or directory['captions'] != captions or directory['source_records_hash'] != digest(captions)):
        raise ValueError('Scene directory source changed')
    ids = [c for s in directory['scenes'] for c in s['caption_ids']]
    if ids != [c['id'] for c in captions]:
        raise ValueError('Scene directory omitted, reordered or duplicated source captions')
    lookup = {c['id']: c for c in captions}
    scene_ids = {s['id'] for s in directory['scenes']}
    if len(scene_ids) != len(directory['scenes']) or len(directory['vectors']) != len(scene_ids):
        raise ValueError('Invalid scene directory IDs or vectors')
    for s in directory['scenes']:
        if s['interval'] != [lookup[s['caption_ids'][0]]['interval'][0], lookup[s['caption_ids'][-1]]['interval'][1]]:
            raise ValueError('Scene interval does not match source captions')
        if s.get('entities') or s.get('events') or s.get('relations'):
            raise ValueError('Initial directory must not contain a prebuilt local graph')
    scenes = {s['id']: s for s in directory['scenes']}
    for p in directory['pointers']:
        if p['source'] not in scene_ids or p['target'] not in scene_ids or p.get('entity_correspondences'):
            raise ValueError('Initial pointer has invalid endpoints or prebuilt identity')
        if scenes[p['source']]['interval'][1] > scenes[p['target']]['interval'][0]:
            raise ValueError('Stored scene pointers must point forward in time')
    from .key_memory import validate_keys, validate_hierarchy
    validate_keys(directory)
    validate_hierarchy(directory)
    return directory


def frontier(directory, opened):
    result = []
    for p in directory['pointers']:
        a, b = p['source'], p['target']
        if (a in opened) == (b in opened):
            continue
        result.append(dict(p, opened_scene=a if a in opened else b,
            next_scene=b if a in opened else a, direction='forward' if a in opened else 'backward'))
    return result


def build_local_graph(directory, scene, cfg, meter):
    allowed = set(scene['caption_ids'])
    captions = [c for c in directory['captions'] if c['id'] in allowed]
    reports, failures = [], []
    size = cfg['scene_memory']['graph_batch_captions']
    for start in range(0, len(captions), size):
        try:
            inputs = extraction_inputs(captions[start:start+size])
            inputs['max_events'] = cfg['scene_memory']['max_events_per_batch']
            report, _ = meter.json('summary_graph', inputs, ('events',))
            reports.append(report)
        except (ToolFailure, ResourceExhausted) as exc:
            failures.append(str(exc))
    graph = assemble_graph(scene['id'], directory['caption_hash'], captions, reports, failures)
    graph.update(scene_id=scene['id'], identity_scope='scene_local_text_labels')
    return graph


def validate_matches(report, left, right):
    left_entities = {e['id']: e for e in left['entities']}
    right_entities = {e['id']: e for e in right['entities']}
    lc, rc = {c['id'] for c in left['captions']}, {c['id'] for c in right['captions']}
    matches = []
    for m in report['matches']:
        if 'left' in m and 'right' in m:
            # Each side is decoded against the selected entity's own source IDs.
            m = {k: v for k, v in m.items() if k not in ('left', 'right')} | {
                'left_entity': m['left']['entity'], 'right_entity': m['right']['entity'],
                'caption_ids': list(dict.fromkeys(m['left']['caption_ids']+m['right']['caption_ids']))}
        refs = set(m['caption_ids'])
        if (m['left_entity'] not in left_entities or m['right_entity'] not in right_entities
                or not refs <= lc | rc or not refs & lc or not refs & rc):
            raise ValueError('Cross-scene correspondence must cite both opened scenes')
        if (not refs & set(left_entities[m['left_entity']]['caption_ids'])
                or not refs & set(right_entities[m['right_entity']]['caption_ids'])):
            raise ValueError('Correspondence citations do not contain the named entities')
        if 'relation' in m:
            m = dict(m, decision={'same_instance': 'same', 'different_instance': 'different'}.get(m['relation'], 'unknown'))
        certain_text = m.get('relation', m['decision']) not in ('unknown',)
        matches.append(dict(m, status='text_inferred' if certain_text else 'unresolved',
                            physical_identity_confirmed=False))
    return matches


def relation_context(graph, include_entity_sources=True):
    """Lossless reference view: original quotes occur once in the captions table."""
    context = {'id': graph['scene_id'], 'scene_id': graph['scene_id'],
        'identity_scope': graph['identity_scope'],
        'status': 'caption_asserted; entities are text labels, not confirmed physical identities',
        'entities': [{k: e[k] for k in ('id', 'label', 'kind', 'caption_ids')} for e in graph['entities']],
        'events': [{k: e[k] for k in ('id', 'action', 'roles', 'interval', 'caption_ids')} for e in graph['events']],
        'temporal_edges': [{k: e[k] for k in ('before', 'after')} for e in graph['temporal_edges']],
        'temporal_edge_status': 'source_timestamps_only; not causal'}
    if not include_entity_sources:
        for entity in context['entities']:
            # Assembly derives these exact IDs from event roles. Repeating the
            # actor's full caption list in every graph inflated joint reviews.
            entity.pop('caption_ids')
        context['entity_source_rule'] = 'Union of caption_ids of events naming this entity in roles'
    return context


def key_context(records):
    # Source quotes/times are already present verbatim in the visible captions.
    fields = ('id', 'kind', 'description', 'entity_terms', 'before_caption_ids', 'after_caption_ids',
              'interval', 'pre_state_known', 'last_observed_at', 'state_valid_until')
    return [{k: card[k] for k in fields} for card in records]


def correspondence_context(records):
    return [{'id': r['pointer_id'], 'from_scene': r['from_scene'], 'to_scene': r['to_scene'],
             'matches': r['matches']} for r in records]


def compact_review_inputs(inputs):
    """Retain every caption, event, role, key and frontier candidate without repeated quotes."""
    result = dict(inputs)
    result['local_graphs'] = [relation_context(g, include_entity_sources=False) for g in inputs['local_graphs']]
    result['correspondences'] = correspondence_context(inputs['correspondences'])
    fields = ('id', 'kind', 'direction', 'description', 'gap_seconds', 'similarity', 'key_hints', 'key_hints_status')
    result['frontier'] = [{**{k: p[k] for k in fields if k in p},
        'from_scene': {'id': p['opened_scene']}, 'to_scene': {'id': p['next_scene']}}
        for p in inputs['frontier']]
    if 'key_memory' in inputs:
        result['key_memory'] = key_context(inputs['key_memory'])
    return result


def short_context_ids(context):
    """QA supplements need no storage IDs; keep caption references and all content intact."""
    aliases = {}
    def collect(value):
        if isinstance(value, dict):
            if isinstance(value.get('id'), str):
                aliases.setdefault(value['id'], f'R{len(aliases)+1}')
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    def replace(value):
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        return aliases.get(value, value) if isinstance(value, str) else value
    collect(context)
    return replace(context)


def review_scenes(operation, inputs, meter):
    """On overflow, batch graph supplements; every batch still reads ALL captions."""
    required = ('selected_option', 'reason', 'caption_ids', 'sufficient', 'gap', 'next_pointer')
    try:
        return meter.json(operation, compact_review_inputs(inputs), required)[0]
    except ToolFailure as exc:
        graphs = inputs['local_graphs']
        if 'Context overflow' not in str(exc) or len(graphs) < 2:
            raise
        # Every relation/key and navigation candidate remains in every call.
        # Entity labels from the other graph batch keep cross-scene links legible.
        shared = dict(inputs)
        shared.setdefault('entity_index', [{k: e[k] for k in ('id', 'label', 'kind')}
            for graph in graphs for e in graph['entities']])
        shared['graph_batch_status'] = 'Partial graph supplement; complete opened captions and cross-scene links retained'
        middle = len(graphs)//2
        reports = [review_scenes(operation, dict(shared, local_graphs=batch), meter)
            for batch in (graphs[:middle], graphs[middle:])]
        enough = all(r['sufficient'] for r in reports) and len({r['selected_option'] for r in reports}) == 1
        unresolved = next((r for r in reports if not r['sufficient']), None)
        combined = {'selected_option': reports[0]['selected_option'],
            'reason': 'Independent graph batches with complete original captions; all batches must agree to stop',
            'caption_ids': list(dict.fromkeys(c for r in reports for c in r['caption_ids'])),
            'sufficient': enough, 'gap': '' if enough else (
                unresolved['gap'] if unresolved else 'Graph batches disagree on the answer'),
            'next_pointer': unresolved['next_pointer'] if unresolved else None,
            'graph_batches': {'overflow': str(exc), 'all_captions_preserved': True,
                'scene_ids': [g['scene_id'] for g in graphs], 'reports': reports}}
        return combined


def navigation_coverage(requirement, scenes, opened, scope):
    """Necessary text coverage only; never establishes semantic or pixel sufficiency."""
    kind = requirement['scope']
    missing = []
    points = []
    if kind == 'global':
        points = [scope[0]+(scope[1]-scope[0])*fraction for fraction in (1/6, 1/2, 5/6)]
        missing = [t for t in points if not any(scenes[s]['interval'][0] <= t < scenes[s]['interval'][1]
            for s in opened)]
        met = not missing
    elif kind == 'exhaustive':
        missing = [sid for sid in scenes if sid not in opened]
        met = not missing
    elif kind == 'multi_scene':
        met = len(opened) >= min(2, len(scenes))
        missing = [] if met else ['another_relevant_stage']
    else:
        met = kind == 'local'
    return {'scope': kind, 'met': met, 'missing': missing,
        'sample_times': points, 'status': 'necessary_text_coverage_not_answer_proof'}


def coverage_pointer(candidates, coverage, scenes, rankings):
    """Follow existing pointers toward a missing stage; unopened summaries stay hints."""
    if not candidates:
        return None
    ranks = {sid: i for i, sid in enumerate(rankings)}
    def priority(pointer):
        sid = pointer['next_scene']
        a, b = scenes[sid]['interval']
        if coverage['scope'] == 'global':
            missing = coverage['missing']
            hits = sum(a <= t < b for t in missing)
            distance = min((max(a-t, t-b, 0.) for t in missing), default=0.)
            return (-hits, distance, ranks.get(sid, len(scenes)), pointer['id'])
        return (0, 0, ranks.get(sid, len(scenes)), pointer['id'])
    return min(candidates, key=priority)


@metered_cpu('scene_navigation')
def run_pipeline(memory, query, metadata, cfg, meter, output, state, directory=None):
    from .compatibility import qa_inputs
    if query['answer_mode'] != 'choice' or len(query['options']) != 5:
        raise ValueError('Initial scene-memory evaluation supports five-choice questions')
    if directory is None:
        directory = build_directory(memory.manifest, cfg, meter)
    validate_directory(directory, memory.manifest)
    if cfg['scene_memory']['key_information'] and 'key_memory' not in directory:
        raise ValueError('Key-memory mode requires a key-augmented directory')
    if cfg['scene_memory']['hierarchical_directory'] and 'hierarchy' not in directory:
        raise ValueError('Hierarchical mode requires a hierarchy directory')
    captions = directory['captions']
    if [c['interval'] for c in captions] != [[i, i+1] for i in range(len(captions))]:
        raise ValueError('Original QA adapter requires the one-second caption grid')
    scope = query['scope']
    scenes = {s['id']: s for s in directory['scenes'] if scope[0] <= s['interval'][0] and s['interval'][1] <= scope[1]}
    if not scenes:
        raise ValueError('No complete scene in query scope')
    text = query['question']+' '+' '.join(query['options'])
    encoded, _ = meter.call('embed', {'texts': [text]})
    hierarchy_trace, key_trace = [], {}
    if cfg['scene_memory']['hierarchical_directory']:
        from .key_memory import rank_hierarchy
        rankings, hierarchy_trace = rank_hierarchy(directory, text, encoded['vectors'][0], scope, cfg, meter)
    else:
        source_text = {c['id']: c['text'] for c in captions}
        documents = [{'id': s['id'], 'interval': s['interval'], 'text': s['description']+' '+
            ' '.join(s['entity_terms'])+'\n'+'\n'.join(source_text[c] for c in s['caption_ids'])}
            for s in directory['scenes']]
        retrieved, _ = meter.call('retrieve', {'documents': documents, 'vectors': directory['vectors'],
            'query_text': text, 'query_vector': encoded['vectors'][0], 'scope': scope, 'cfg': cfg['retrieval']})
        rankings = [r['id'] for r in retrieved['data']]
    if cfg['scene_memory']['key_information']:
        from .key_memory import rank_keys, fuse_rankings
        key_ranking, key_trace = rank_keys(directory, text, encoded['vectors'][0], scope, cfg, meter)
        rankings = fuse_rankings(rankings, key_ranking)
    rankings = [sid for sid in rankings if sid in scenes]
    if not rankings:
        raise ValueError('Scene retrieval returned no candidates')
    diagnostics = {'engine': 'scene_lazy_memory', 'entry_scene': rankings[0], 'opened_scenes': [],
        'local_graphs': {}, 'traversals': [], 'reviews': [], 'correspondences': [], 'extension_errors': [],
        'directory_hash': digest(directory), 'directory_fallback': directory['build_diagnostics']['fallback_reason'],
        'total_scenes': len(scenes), 'total_captions': len(captions), 'pixel_reads': 0,
        'hierarchy_retrieval': hierarchy_trace, 'key_retrieval': key_trace,
        'key_information_enabled': cfg['scene_memory']['key_information'],
        'hierarchy_enabled': cfg['scene_memory']['hierarchical_directory'],
        'key_build_diagnostics': directory.get('key_memory', {}).get('build_diagnostics', {}), 'opened_key_count': 0}
    state['scene_memory'] = diagnostics
    try:
        requirement, _ = meter.json('scene_scope', {'question': query['question'],
            'options': query['options']}, ('scope', 'reason'))
    except (ToolFailure, ResourceExhausted) as exc:
        diagnostics['extension_errors'].append(str(exc))
        requirement = {'scope': 'unresolved', 'reason': 'Temporal evidence requirement unavailable'}
    diagnostics['scope_requirement'] = requirement
    opened, graphs = diagnostics['opened_scenes'], diagnostics['local_graphs']
    next_scene, crossing = rankings[0], None
    while next_scene is not None and len(opened) < cfg['scene_memory']['max_scene_visits']:
        graph = build_local_graph(directory, scenes[next_scene], cfg, meter)
        graphs[next_scene] = graph
        opened.append(next_scene)
        diagnostics['extension_errors'].extend(graph['build_diagnostics']['failures'])
        write_json(Path(output)/'scene_memory'/f'{digest(next_scene)[:16]}.json', graph)
        if crossing is not None:
            left, right = graphs[crossing['opened_scene']], graph
            try:
                if left['entities'] and right['entities']:
                    operation = 'scene_correspondence_lineage' if cfg['scene_memory']['key_information'] else 'scene_correspondence'
                    matches, _ = meter.json(operation, {'question': query['question'],
                        'gap': diagnostics['reviews'][-1]['gap'],
                        'left': dict(relation_context(left), captions=left['captions']),
                        'right': dict(relation_context(right), captions=right['captions'])}, ('matches',))
                    matches = validate_matches(matches, left, right)
                else:
                    matches = []
                diagnostics['correspondences'].append({'pointer_id': crossing['id'],
                    'from_scene': left['scene_id'], 'to_scene': right['scene_id'], 'matches': matches})
            except (ToolFailure, ResourceExhausted, ValueError) as exc:
                diagnostics['extension_errors'].append(str(exc))
        visible = [c for c in captions if any(c['id'] in scenes[s]['caption_ids'] for s in opened)]
        candidates = [p for p in frontier(directory, set(opened)) if p['next_scene'] in scenes]
        if cfg['scene_memory']['hierarchical_directory']:
            ranks = {sid: i for i,sid in enumerate(rankings)}
            candidates.sort(key=lambda p: (ranks.get(p['next_scene'], len(scenes)),
                -p.get('similarity', 0.), p['id']))
            candidates = candidates[:cfg['scene_memory']['hierarchy_frontier_limit']]
        key_records = []
        if cfg['scene_memory']['key_information']:
            from .key_memory import opened_keys
            key_records = opened_keys(directory, set(opened))
            diagnostics['opened_key_count'] = len(key_records)
            key_index = directory['key_memory']
            for pointer in candidates:
                key_ranks = {uid: i for i,uid in enumerate(key_trace.get('ranked_key_ids', []))}
                ids = sorted(scenes[pointer['next_scene']].get('key_ids', []),
                    key=lambda uid: (key_ranks.get(uid, len(key_index['records'])), uid))[:3]
                pointer['key_hints'] = [{k: key_index['records'][key_index['positions'][uid]][k]
                    for k in ('kind', 'description')} for uid in ids]
                pointer['key_hints_status'] = 'unopened_navigation_only'
        inputs = {'question': query['question'], 'options': query['options'], 'option_ids': list('01234'),
            'captions': visible, 'local_graphs': list(graphs.values()),
            'correspondences': diagnostics['correspondences'], 'frontier': candidates,
            'opened_scene_ids': list(opened), 'total_scenes': len(scenes), 'video_scope': scope}
        coverage = navigation_coverage(requirement, scenes, opened, scope)
        inputs.update(scope_requirement=requirement, coverage_check=coverage)
        if cfg['scene_memory']['key_information']:
            inputs['key_memory'] = key_records
            inputs['key_memory_status'] = 'caption_hints; last_observed_at does not imply persistence'
        try:
            review_operation = 'scene_review_keyed' if cfg['scene_memory']['key_information'] else 'scene_review'
            review = review_scenes(review_operation, inputs, meter)
        except (ToolFailure, ResourceExhausted) as exc:
            diagnostics['extension_errors'].append(str(exc))
            break
        review['model_decision'] = deepcopy(review)
        review['coverage_check'] = coverage
        if review['sufficient'] and not review['caption_ids']:
            review.update(sufficient=False, gap='No source captions cited for the sufficiency decision')
        if not coverage['met']:
            review['sufficient'] = False
            if requirement['scope'] != 'unresolved':
                review['gap'] = (review['gap']+'; ' if review['gap'] else '')+(
                    f"Question requires {requirement['scope']} evidence; missing temporal coverage: {coverage['missing']}")
                requested = next((p for p in candidates if p['id'] == review['next_pointer']), None)
                needed = (requested if requested and requirement['scope'] != 'global'
                    else coverage_pointer(candidates, coverage, scenes, rankings))
                if needed:
                    review['next_pointer'] = needed['id']
        diagnostics['reviews'].append(review)
        write_json(Path(output)/'scene_memory/progress.json', diagnostics)
        if review['sufficient']:
            break
        crossing = next((p for p in candidates if p['id'] == review['next_pointer']), None)
        if crossing is None:
            break
        if len(opened) >= cfg['scene_memory']['max_scene_visits']:
            break
        diagnostics['traversals'].append(dict(crossing, gap=review['gap']))
        next_scene = crossing['next_scene']
    selected = [i for i, c in enumerate(captions) if any(c['id'] in scenes[s]['caption_ids'] for s in opened)]
    final = qa_inputs(query, captions, selected)
    final_context = {'local_graphs': [relation_context(g) for g in graphs.values()],
        'cross_scene_correspondences': correspondence_context(diagnostics['correspondences'])}
    if cfg['scene_memory']['key_information']:
        from .key_memory import opened_keys
        final_context['key_memory'] = key_context(opened_keys(directory, set(opened)))
    final['prompts'][-1] += ('\n\nCaption-derived scene memory (unverified text interpretations; original descriptions above are primary; '
        'identity links are provisional, temporal order does not establish causation; '
        'caption TS<n> refers to Frame n-1 above; event/key source text is in those original descriptions; '
        'key last_observed_at does not imply persistence, state_valid_until=null means unknown):\n'+json.dumps(
            short_context_ids(final_context), ensure_ascii=False))
    raw, _ = meter.call('videotree_qa', final, final=True)
    chosen = raw['data']
    last = diagnostics['reviews'][-1] if diagnostics['reviews'] else None
    diagnostics.update(final_selected_seconds=selected, opened_caption_count=len(selected),
        text_sufficient=bool(last and last['sufficient']), final_prediction=chosen['selected_option'])
    decision = obj(memory.new_id(query['id'], 'stop'), reason='exploration_stop', evidence_status='insufficient',
        checked_requirement_ids=[], remaining_gap_refs=[], gate_hash=digest(cfg['scene_memory']),
        actual_cost=meter.ledger.actual, budget_violation=meter.ledger.budget_violation,
        exploration_limit='scene_text_sufficient' if diagnostics['text_sufficient'] else 'scene_exploration_limit')
    memory.put('stop_decisions', decision)
    answer = obj(memory.new_id(query['id'], 'answer'), query_id=query['id'], selected_option=chosen['selected_option'],
        text=query['options'][int(chosen['selected_option'])], explanation=chosen['text'], claim_ids=[], evidence_ids=[],
        stop_decision_id=decision['id'])
    memory.put('answers', answer)
    state['answer_source'] = 'scene_memory_original_qa'
    write_json(Path(output)/'scene_memory/progress.json', diagnostics)
    return {'answer': answer, 'stop_decision': decision, 'scene_memory': diagnostics}
