"""Question-independent M0 construction and immutable source validation."""
from pathlib import Path
import json
import time

from .budget import BudgetLedger, Meter, ToolFailure
from .config import base_config, digest
from .coverage import complement
from .state import Memory, Store, memory_hash, obj, write_json
from .accounting import directory_stats


def evidence_record(memory, namespace, scene_ids, interval, frames, subtitles, observation, call, cfg):
    return obj(memory.new_id(namespace, 'ev'), video_id=memory.manifest['video_id'], scene_ids=scene_ids,
               interval=interval, source_type='visual' if frames else 'subtitle', frame_inputs=frames,
               text_spans=subtitles, observation=observation,
               input_hash=digest({'frames': frames, 'text_spans': subtitles}), output_hash=digest(observation),
               observer_config_hash=(call.get('replay') or {}).get('source_model_hash') or digest(cfg['models']),
               call_id=call['call_id'])


def summarize_all(meter, captions, cfg):
    """Recursive reduction includes every window and every uncertainty."""
    batch_size = cfg['initialization']['summary_reduce_batch_windows']
    max_chars = cfg['initialization']['summary_max_chinese_chars']
    current = captions
    uncertainties = sorted(set(u for c in captions for u in c.get('uncertainties', [])))
    if not captions:
        raise ToolFailure('Cannot summarize an empty source')
    if len(captions) == 1 and len(captions[0].get('caption', '')) <= max_chars:
        # A short single-window description already is a navigation summary.
        # Keep its full wording and uncertainty; do not pay for a paraphrase.
        return {'text': captions[0]['caption'], 'place': None, 'event_keywords': [], 'uncertainties': uncertainties}
    while True:
        reduced = []
        for start in range(0, len(current), batch_size):
            batch = current[start:start+batch_size]
            report, _ = meter.json('summarize', {'windows': batch, 'max_chars': max_chars},
                                   ('text', 'place', 'event_keywords', 'uncertainties'), build=True)
            if len(report['text']) > max_chars:
                raise ToolFailure('Summary exceeds configured character limit')
            report['uncertainties'] = sorted(set(report['uncertainties']+uncertainties))
            reduced.append(report)
        if len(reduced) == 1:
            return reduced[0]
        current = reduced


def build_video(item, cfg, backend, output, startup_measurement=None):
    from .accounting import ACTIVE
    previous = ACTIVE.get()
    try:
        return _build_video(item, cfg, backend, output, startup_measurement)
    except Exception as exc:
        meter = ACTIVE.get()
        cost = meter.ledger.summary() if meter is not previous and meter is not None else None
        if cost is None and (Path(output) / 'calls.jsonl').exists():
            rows = [json.loads(line) for line in (Path(output) / 'calls.jsonl').read_text().splitlines()]
            cost = {'unit': 'processing_seconds', 'initialization': 0.,
                    'online': sum(r['actual_cost'] for r in rows), 'call_count': len(rows)}
        if not (Path(output) / 'base_hash.json').exists():
            write_json(Path(output) / 'build_failure.json', {'error': {'code': 'build_failed', 'message': str(exc)},
                       'build_status': 'failed', 'cost_summary': cost})
        raise
    finally:
        meter = ACTIVE.get()
        if meter is not previous and meter is not None:
            meter.close()


def _build_video(item, cfg, backend, output, startup_measurement=None):
    if set(item)-{'video_id', 'uri', 'cutoff', 'subtitle_uri', 'caption_uri'}:
        raise ValueError('Build manifest accepts only video_id, uri, cutoff, subtitle_uri and caption_uri')
    output = Path(output)
    if (output / 'base_hash.json').exists():
        memory, metadata = load_base(output)
        if metadata['base_config_hash'] != digest(base_config(cfg)):
            raise ValueError('Existing M0 has a different configuration; choose a new output directory')
        if str(Path(item['uri']).resolve()) != memory.manifest['uri'] or float(item.get('cutoff', memory.manifest['cutoff'])) != memory.manifest['cutoff']:
            raise ValueError('Existing base is for a different video or legal prefix')
        if (str(Path(item['caption_uri']).resolve()) if item.get('caption_uri') else None) != memory.manifest.get('caption_uri'):
            raise ValueError('Existing base uses a different caption source')
        return memory, metadata
    output.mkdir(parents=True, exist_ok=True)
    ledger = BudgetLedger(1e12, answer_reserve=0, calls_path=output / 'calls.jsonl')
    meter = Meter(backend, ledger, cfg, output / 'raw_calls')
    started = time.monotonic()
    if startup_measurement:
        ledger.record('model_snapshot_validation', {'in_process': True}, cfg['budget']['build_call_cap'],
                      time.monotonic()-startup_measurement['seconds'],
                      output={'usage': {'cpu_seconds': startup_measurement['cpu_seconds'], 'gpu_resident_seconds': 0.}})
    inspected, _ = meter.call('inspect', {'item': item, 'base_config': base_config(cfg)}, build=True)
    memory = Memory(inspected['data'])
    manifest, namespace = memory.manifest, item['video_id']
    imported, caption_call = None, None
    if cfg['initialization']['caption_source'] == 'provided':
        imported, caption_call = meter.call('import_captions', {'manifest': manifest}, build=True)
    frames_out = output / 'frames'
    features, sample_pts, sample_frame_ids = [], [], []
    # Bound image encoder packages while retaining the global 0,1,... sampling grid.
    fps = cfg['segmentation']['sample_fps']
    step = max(32, cfg['models']['batching']['visual_encoder'])/fps
    cursor = 0.
    while cursor < manifest['cutoff']:
        end = min(cursor+step, manifest['cutoff'])
        decoded, _ = meter.call('decode', {'manifest': manifest, 'interval': [cursor, end],
                                          'output': str(frames_out), 'fps': fps, 'longest_side': 768}, build=True)
        frames = decoded['data']
        if frames:
            encoded, _ = meter.call('features', {'frames': frames}, build=True)
            for frame, vector in zip(frames, encoded['vectors']):
                if frame['frame_id'] not in sample_frame_ids:
                    features.append(vector)
                    sample_pts.append(frame['pts'])
                    sample_frame_ids.append(frame['frame_id'])
        cursor = end
    if not features:
        raise ValueError('build_failed: no valid segmentation features')
    imputed, _ = meter.call('impute_features', {'manifest': manifest, 'features': features,
        'sample_pts': sample_pts, 'sample_frame_ids': sample_frame_ids, 'fps': fps}, build=True)
    features, sample_pts, sample_frame_ids = (imputed['data'][k] for k in ('features', 'pts', 'frame_ids'))
    if cfg['segmentation']['solver'] == 'shot_histogram':
        segmented, _ = meter.call('shot_partition', {'manifest': manifest, 'cfg': cfg['segmentation']}, build=True)
    else:
        segmented, _ = meter.call('segment', {'features': features, 'sample_pts': sample_pts,
                                          'frame_pts': [f['pts'] for f in manifest['frames']],
                                              'cutoff': manifest['cutoff'], 'cfg': cfg['segmentation']}, build=True)
    boundaries = segmented['data']
    scene_ids = [f'{namespace}:S{i+1:04d}' for i in range(len(boundaries))]
    for i, boundary in enumerate(boundaries):
        memory.put('scenes', obj(scene_ids[i], video_id=namespace, **boundary,
                                 prev_scene_id=scene_ids[i-1] if i else None,
                                 next_scene_id=scene_ids[i+1] if i+1 < len(scene_ids) else None,
                                 window_ids=[], occurrence_ids=[], claim_ids=[], summary_id=scene_ids[i]+':summary',
                                 boundary_config_hash=digest(cfg['segmentation'])))
    from .segmentation import windows
    from .video import load_subtitles
    for scene_id in scene_ids:
        scene = memory.get(scene_id)
        captions, window_ids, evidence_ids, mention_ids = [], [], [], []
        for interval in windows(scene['interval'], cfg['initialization']['window_seconds'],
                                cfg['initialization']['window_stride_seconds']):
            window_id = memory.new_id(scene_id, 'W')
            window_ids.append(window_id)
            if imported is None:
                decoded, _ = meter.call('decode', {'manifest': manifest, 'interval': interval,
                                              'output': str(frames_out),
                                              'count': cfg['initialization']['caption_frames_per_window'],
                                              'longest_side': cfg['initialization']['caption_longest_side']}, build=True)
                frames = decoded['data']
            else:
                frames = []
            caption_spans = ([dict(s, start=max(s['start'], interval[0]), end=min(s['end'], interval[1]))
                              for s in imported['data'] if max(s['start'], interval[0]) < min(s['end'], interval[1])]
                             if imported is not None else [])
            subtitles = load_subtitles(manifest, interval) if cfg['initialization']['use_provided_subtitles'] else []
            if (imported is not None and not caption_spans) or (imported is None and not frames and not subtitles):
                missing_caption = imported is not None
                message = '此窗口缺少外部描述；保留时间入口' if missing_caption else '此窗口无法解码；保留时间入口'
                memory.put('windows', obj(window_id, scene_id=scene_id, interval=interval, frame_ids=[],
                    caption=message, evidence_ids=[], coverage_ids=[], mention_ids=[],
                    caption_status='unreadable'))
                captions.append({'caption': message, 'window_id': window_id,
                                 'uncertainties': ['Missing provided caption' if missing_caption else 'Video decode gap'], 'evidence_ids': []})
                continue
            status = 'ok'
            try:
                if imported is not None:
                    report = {'caption': '\n'.join(f"[{s['start']:g},{s['end']:g}) {s['text']}" for s in caption_spans),
                              'mentions': [], 'event_keywords': [], 'source_frame_ids': [],
                              'uncertainties': ['外部视觉描述尚未经在线事实核验']}
                    call, status = caption_call, 'provided'
                else:
                    report, call = meter.json('caption', {'frames': frames, 'subtitles': subtitles, 'interval': interval},
                                          ('caption', 'mentions', 'event_keywords', 'source_frame_ids', 'uncertainties'), build=True)
                if not set(report['source_frame_ids']) <= {f['frame_id'] for f in frames}:
                    raise ToolFailure('Caption references a frame outside its input')
            except ToolFailure as exc:
                status = 'failed'
                call = ledger.calls[-1]
                report = {'caption': '无法可靠辨认此窗口', 'mentions': [], 'event_keywords': [],
                          'source_frame_ids': [], 'uncertainties': [str(exc)]}
            if any(max(interval[0], a) < min(interval[1], b) for a, b in manifest.get('missing_intervals', [])):
                report['uncertainties'].append('窗口包含无法解码的原始时间范围；未读取内容未知')
                status = 'partial'
            evidence = evidence_record(memory, namespace, [scene_id], interval, frames,
                                       caption_spans if imported is not None else subtitles, report['caption'], call, cfg)
            if imported is not None:
                evidence.update(source_type='caption', observer_config_hash=digest({
                    'source_hash': manifest['caption_hash'], 'producer': imported.get('producer')}))
            memory.put('evidence', evidence)
            evidence_ids.append(evidence['id'])
            local_mentions = []
            for mention in report['mentions']:
                m = obj(memory.new_id(namespace, 'mention'), scene_id=scene_id, window_ids=[window_id],
                        label=mention['label'], description=mention['description'], evidence_ids=[evidence['id']],
                        occurrence_id=None, entity_id=None, status='unlocalized')
                memory.put('mentions', m)
                local_mentions.append(m['id'])
                mention_ids.append(m['id'])
            coverage = obj(memory.new_id(namespace, 'cov'), interval=interval, target_requirement_ids=[],
                           evidence_ids=[evidence['id']], actual_pts=[f['pts'] for f in frames],
                           max_sample_gap=None, min_target_short_side=None, visibility='unknown',
                           level='sampled', result='unknown', protocol_hash=digest(cfg['initialization']), verifier_ref=None)
            memory.put('coverage', coverage)
            memory.put('windows', obj(window_id, scene_id=scene_id, interval=interval,
                                     frame_ids=[f['frame_id'] for f in frames], caption=report['caption'],
                                     evidence_ids=[evidence['id']], coverage_ids=[coverage['id']],
                                     mention_ids=local_mentions, caption_status=status))
            captions.append(dict(report, window_id=window_id, evidence_ids=[evidence['id']]))
        try:
            summary = summarize_all(meter, captions, cfg)
            status = 'current'
        except ToolFailure as exc:
            summary = {'text': '无法可靠汇总；请通过时间入口核查', 'place': None, 'event_keywords': [],
                       'uncertainties': [str(exc)]}
            status = 'unreadable'
        memory.put('summaries', obj(scene['summary_id'], scene_id=scene_id, **summary, claim_ids=[],
                                   entity_mentions=[{'mention_id': m, 'label': memory.get(m)['label'], 'occurrence_id': None} for m in mention_ids],
                                   evidence_ids=evidence_ids, all_window_ids=window_ids,
                                   source_revision_hash=digest(captions), status=status))
        memory.update('scenes', scene_id, window_ids=window_ids)
        print(f'BUILD {namespace} {scene_id} windows={len(window_ids)}', flush=True)
    docs = [{'id': s['scene_id'], 'text': ' '.join([s['text'], s['place'] or '', *s['event_keywords']]),
             'interval': memory.get(s['scene_id'])['interval']} for s in memory.tables['summaries'].values()]
    window_docs = [{'id': w['id'], 'text': w['caption'], 'interval': w['interval']} for w in memory.tables['windows'].values()]
    embedding, _ = meter.call('embed', {'texts': [d['text'] for d in docs+window_docs]}, build=True)
    metadata = {'base_config_hash': digest(base_config(cfg)), 'config_hash': digest(cfg),
                'segmentation_report': segmented.get('metadata', {'solver': 'exact_dp', 'scene_count': len(boundaries)}),
                'base_hash': memory_hash(memory), 'initialization_cost': ledger.actual,
                'wall_seconds': time.monotonic()-started, 'build_cost': ledger.summary(),
                'index': {'documents': docs, 'vectors': embedding['vectors'][:len(docs)],
                          'window_documents': window_docs, 'window_vectors': embedding['vectors'][len(docs):]},
                'feature_samples': {'pts': sample_pts, 'frame_ids': sample_frame_ids,
                                    'imputed': imputed['data']['imputed'],
                                    'missing_sample_pts': imputed['data']['missing_sample_pts']}}
    store = Store(output)
    try:
        store.checkpoint(memory, metadata)
        metadata['storage'] = directory_stats(output)
        metadata.update(initialization_cost=ledger.actual, build_cost=ledger.summary(),
                        wall_seconds=time.monotonic()-started+(startup_measurement or {}).get('seconds', 0.))
        store.finalize_accounting(memory, metadata)
    finally:
        store.close()
        meter.close()
    write_json(output / 'base_hash.json', {'base_hash': metadata['base_hash'], 'base_config_hash': metadata['base_config_hash']})
    write_json(output / 'build_cost.json', metadata['build_cost'])
    write_json(output / 'indices' / 'retrieval.json', metadata['index'])
    write_json(output / 'feature_arrays' / 'samples.json', dict(metadata['feature_samples'], vectors=features))
    return memory, metadata


def load_base(directory):
    if not (Path(directory) / 'state.sqlite3').exists() or not (Path(directory) / 'base_hash.json').exists():
        raise ValueError(f'No completed M0 in {directory}')
    store = Store(directory)
    try:
        memory, metadata = store.load()
    finally:
        store.close()
    expected = json.loads((Path(directory) / 'base_hash.json').read_text())['base_hash']
    if memory_hash(memory) != expected or metadata['base_hash'] != expected:
        raise ValueError('M0 content hash mismatch')
    from .video import file_hash
    if file_hash(memory.manifest['uri']) != memory.manifest['media_hash']:
        raise ValueError('Video content changed since M0 construction')
    if memory.manifest.get('caption_uri') and file_hash(memory.manifest['caption_uri']) != memory.manifest.get('caption_hash'):
        raise ValueError('Caption content changed since M0 construction')
    if any(memory.tables[t] for t in ('occurrences', 'entities', 'identity_links', 'claims')):
        raise ValueError('Shared M0 must be lightweight; eager details belong in M_eager')
    return memory, metadata
