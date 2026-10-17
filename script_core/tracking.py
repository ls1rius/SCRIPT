"""Detection NMS, motion/appearance assignment and conservative overlap ID reuse."""
from copy import deepcopy
import math

from .state import obj


def iou(a, b):
    inter = max(0., min(a[2], b[2])-max(a[0], b[0])) * max(0., min(a[3], b[3])-max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter
    return inter/union if union else 0.


def cosine(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    norm = math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return dot/norm if norm else 0.


def compatible(a, b):
    aliases = {'people': 'person', 'man': 'person', 'woman': 'person'}
    return aliases.get(a, a) == aliases.get(b, b)


def suppress(detections, threshold=.6):
    by_frame = {}
    for d in sorted(detections, key=lambda d: (-d['score'], d['category'], d['bbox'])):
        kept = by_frame.setdefault(d['frame_id'], [])
        duplicate = None
        for k in kept:
            overlap = iou(d['bbox'], k['bbox'])
            if compatible(d['category'], k['category']) and overlap > threshold:
                duplicate = k
                break
            if overlap > .8 and cosine(d['feature'], k['feature']) > .95:
                duplicate = k
                k['aliases'] = sorted(set(k.get('aliases', []) + [d['category']]))
                break
        if duplicate is None:
            kept.append(deepcopy(d))
    return sorted((d for ds in by_frame.values() for d in ds), key=lambda d: (d['pts'], d['category'], d['bbox']))


def predict(track, pts):
    last = track[-1]
    box = list(last['bbox'])
    if len(track) > 1:
        previous = track[-2]
        dt = last['pts']-previous['pts']
        if dt > 0:
            for axis in (0, 1):
                shift = ((last['bbox'][axis]+last['bbox'][axis+2])
                         -(previous['bbox'][axis]+previous['bbox'][axis+2]))/2*(pts-last['pts'])/dt
                box[axis] += shift
                box[axis+2] += shift
    return box


def track_detections(detections, cfg):
    import numpy as np
    from scipy.optimize import linear_sum_assignment
    detections = suppress(detections, cfg.get('same_class_nms_iou', .6))
    tracks, active = [], []
    timestamps = sorted(set(d['pts'] for d in detections))
    for timestamp in timestamps:
        current = [d for d in detections if d['pts'] == timestamp]
        active = [i for i in active if timestamp-tracks[i][-1]['pts'] < cfg['max_gap_seconds']]
        scores = np.full((len(active), len(current)+len(active)), cfg['accept_score'])
        for row, index in enumerate(active):
            track = tracks[index]
            for col, det in enumerate(current):
                overlap = iou(predict(track, timestamp), det['bbox'])
                appearance = cosine(track[-1]['feature'], det['feature'])
                score = cfg['iou_weight']*overlap+cfg['appearance_weight']*appearance
                legal = (compatible(track[-1]['category'], det['category'])
                         and (overlap >= cfg['gate_iou'] or appearance >= cfg['gate_cosine'])
                         and score >= cfg['accept_score'])
                scores[row, col] = score if legal else -1.
        used = set()
        if len(active):
            rows, cols = linear_sum_assignment(scores, maximize=True)
            for row, col in zip(rows, cols):
                if col < len(current) and scores[row, col] >= cfg['accept_score']:
                    tracks[active[row]].append(current[col])
                    used.add(col)
        for i, det in enumerate(current):
            if i not in used:
                tracks.append([det])
                active.append(len(tracks)-1)
    return tracks


def representatives(track, count=4):
    ordered = sorted(track, key=lambda d: (-d['score']*(d['bbox'][2]-d['bbox'][0])*(d['bbox'][3]-d['bbox'][1]), d['pts']))
    chosen = []
    span = max(.001, track[-1]['pts']-track[0]['pts'])
    for d in ordered:
        if not chosen or all(abs(d['pts']-c['pts']) >= span/max(1, count) for c in chosen):
            chosen.append(d)
            if len(chosen) == count:
                break
    return sorted(chosen, key=lambda d: d['pts'])


def register_tracks(memory, tracks, scene_id, namespace, evidence_id, cfg):
    """Only shared real frames with compatible boxes can extend an existing ID."""
    result, reused = [], set()
    scene = memory.get(scene_id)
    for track in tracks:
        matching = []
        for old in memory.tables['occurrences'].values():
            if old['scene_id'] != scene_id or old['id'] in reused or not compatible(old['category'], track[0]['category']):
                continue
            old_boxes = {b['frame_id']: b for b in old['boxes']}
            shared = [d for d in track if d['frame_id'] in old_boxes]
            if (shared and all(iou(d['bbox'], old_boxes[d['frame_id']]['bbox']) >= .8 for d in shared)
                    and max(cosine(d['feature'], v) for d in shared for v in old.get('features', [])) >= .85):
                matching.append(old)
        old = matching[0] if len(matching) == 1 else None
        id_ = old['id'] if old else memory.new_id(f'{namespace}:{scene_id}', 'O')
        boxes = {b['frame_id']: b for b in old['boxes']} if old else {}
        for d in track:
            boxes[d['frame_id']] = {'frame_id': d['frame_id'], 'pts': d['pts'], 'bbox': d['bbox'], 'detection_score': d['score']}
        box_list = sorted(boxes.values(), key=lambda b: b['pts'])
        reps = representatives(track, cfg['representative_crops'])
        crops = list(dict.fromkeys((old['crop_refs'] if old else [])+[d['crop_ref'] for d in reps]))[-cfg['representative_crops']:]
        feature_vectors = ((old.get('features', []) if old else [])+[d['feature'] for d in reps])[-cfg['representative_crops']:]
        from pathlib import Path
        from .video import file_hash
        crop_inputs = {(f['frame_id'], f['path']): f for f in old.get('crop_inputs', [])} if old else {}
        for d in reps:
            if Path(d['crop_ref']).is_file():
                crop_inputs[(d['frame_id'], d['crop_ref'])] = {
                    'frame_id': d['frame_id'], 'pts': d['pts'], 'path': d['crop_ref'],
                    'width': d['width'], 'height': d['height'], 'crop': d['bbox'],
                    'content_hash': file_hash(d['crop_ref'])}
        last_end = min(scene['interval'][1], box_list[-1]['pts']+1e-6)
        record = obj(id_, scene_id=scene_id, category=track[0]['category'],
                     aliases=sorted(set(a for d in track for a in d.get('aliases', []))),
                     visible_intervals=[[box_list[0]['pts'], last_end]], boxes=box_list,
                     crop_refs=crops, crop_inputs=list(crop_inputs.values())[-cfg['representative_crops']:],
                     appearance_feature_ref=id_+':features', features=feature_vectors,
                     description=old['description'] if old else '',
                     evidence_ids=list(dict.fromkeys((old['evidence_ids'] if old else [])+[evidence_id])),
                     entity_candidate_ids=old['entity_candidate_ids'] if old else [],
                     stable_track=len(box_list) >= cfg['min_detections_stable'])
        if old:
            record['revision'] = old['revision']+1
            reused.add(id_)
        memory.put('occurrences', record)
        result.append(record)
    return result
