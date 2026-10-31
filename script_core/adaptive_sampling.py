"""Question/gap-directed scene and frame retrieval; sampled text coverage only."""
import numpy as np
from .accounting import metered_cpu


class Sampler:
    def __init__(self, directory, features, question_vector):
        self.directory = directory
        self.scenes = {s['id']: s for s in directory['scenes']}
        self.captions = directory['captions']
        self.positions = {c['id']: i for i, c in enumerate(self.captions)}
        x = np.asarray(features, dtype=float)
        centers = np.asarray([s['center'] for s in directory['scenes']], dtype=float)
        q = np.asarray(question_vector, dtype=float)
        if (x.ndim != 2 or x.shape[0] != len(self.captions) or q.shape != (x.shape[1],)
                or centers.shape != (len(self.scenes), x.shape[1]) or not np.isfinite(x).all()
                or not np.isfinite(q).all() or not np.isfinite(centers).all()):
            raise ValueError('Frame features, captions, centers and question vector must share one feature grid/space')
        if (np.linalg.norm(x, axis=1) <= 0).any() or np.linalg.norm(q) <= 0:
            raise ValueError('Invalid zero-norm retrieval features')
        self.features = x / np.linalg.norm(x, axis=1, keepdims=True)
        norms = np.linalg.norm(centers, axis=1, keepdims=True)
        if (norms <= 0).any():
            raise ValueError('Invalid scene center')
        self.centers = centers / norms
        self.question = q / np.linalg.norm(q)
        self.query = self.question.copy()
        self.gap = ''
        self.trace = []

    @metered_cpu('adaptive_gap_rank_update')
    def set_gap(self, gap, vector=None):
        if vector is None or not gap.strip():
            self.query = self.question.copy()
        else:
            v = np.asarray(vector, dtype=float)
            if v.shape != self.question.shape or not np.isfinite(v).all() or np.linalg.norm(v) <= 0:
                raise ValueError('Gap embedding does not share the retrieval feature space')
            combined = .7*self.question + .3*v/np.linalg.norm(v)
            self.query = combined / np.linalg.norm(combined)
        self.gap = gap

    @metered_cpu('adaptive_scene_ranking')
    def rank_scenes(self, allowed=None):
        allowed = set(self.scenes if allowed is None else allowed)
        # A rare relevant frame need not resemble the dominant scene center.
        frame_scores = self.features @ self.query
        rows = []
        for i, scene in enumerate(self.directory['scenes']):
            if scene['id'] not in allowed:
                continue
            indices = [self.positions[c] for c in scene['caption_ids']]
            score = .5*float(self.centers[i] @ self.query) + .5*float(frame_scores[indices].max())
            rows.append(dict(scene_id=scene['id'], score=score))
        return sorted(rows, key=lambda r: (-r['score'], r['scene_id']))

    @metered_cpu('adaptive_frame_ranking')
    def read(self, scene_id, seen, count=2, priority=(), temporal_context=False, allowed_caption_ids=None):
        ids = self.scenes[scene_id]['caption_ids']
        unread = [cid for cid in ids if cid not in seen and
                  (allowed_caption_ids is None or cid in allowed_caption_ids)]
        scores = self.features @ self.query
        priority = set(priority)
        ranked = sorted(unread, key=lambda cid: (
            -(float(scores[self.positions[cid]]) + (.05 if cid in priority else 0.)), self.positions[cid]))
        selected = []
        if temporal_context and seen:
            # This is triggered by a query's action/order/change gap, never by the cluster center.
            positions = [self.positions[c] for c in seen if c in ids]
            neighbors = [c for c in ranked if any(abs(self.positions[c]-p) == 1 for p in positions)]
            selected.extend(neighbors[:min(2, count)])
        for cid in ranked:
            if len(selected) >= count:
                break
            if cid in selected:
                continue
            # Prefer diversity among similarly ranked repeated views; keep a fallback below.
            i = self.positions[cid]
            if any(abs(i-self.positions[old]) <= 1 and
                   float(self.features[i] @ self.features[self.positions[old]]) > .98 for old in selected):
                continue
            selected.append(cid)
        for cid in ranked:
            if len(selected) >= count:
                break
            if cid not in selected:
                selected.append(cid)
        seen.update(selected)
        self.trace.append(dict(scene_id=scene_id, gap=self.gap, selected_caption_ids=selected,
            selected_frame_indices=[self.positions[c] for c in selected],
            question_scores=[float(self.features[self.positions[c]] @ self.question) for c in selected],
            query_gap_scores=[float(scores[self.positions[c]]) for c in selected],
            temporal_context=bool(temporal_context), policy='question_gap_frame_similarity'))
        return selected

    @metered_cpu('adaptive_sampled_coverage')
    def coverage(self, scope, seen):
        kind = scope['scope']
        read = [c for c in self.captions if c['id'] in seen]
        missing = []
        points = []
        if kind == 'global':
            a, b = self.captions[0]['interval'][0], self.captions[-1]['interval'][1]
            # Coarse temporal bins; opening a scene never counts as reading its entire interval.
            bins = [(a + (b-a)*i/3, a + (b-a)*(i+1)/3) for i in range(3)]
            missing = [[lo, hi] for lo, hi in bins if not any(
                c['interval'][0] < hi and c['interval'][1] > lo for c in read)]
            points = [(lo+hi)/2 for lo, hi in bins]
            met = not missing
        elif kind == 'exhaustive':
            missing = [c['id'] for c in self.captions if c['id'] not in seen]
            met = not missing
        elif kind == 'multi_scene':
            covered = [s for s in self.scenes.values() if set(s['caption_ids']) & seen]
            met = len(covered) >= min(2, len(self.scenes))
            missing = [] if met else ['another_sampled_stage']
        else:
            met = kind == 'local' and bool(read)
        return dict(scope=kind, met=met, missing=missing, sample_times=points,
            read_caption_count=len(read), total_caption_count=len(self.captions),
            status='sampled_text_coverage_not_full_scene_or_answer_proof')

    def coverage_candidates(self, scope, seen, opened):
        """Filter for missing coverage, then retain question-based order."""
        coverage = self.coverage(scope, seen)
        candidates = []
        for row in self.rank_scenes():
            scene = self.scenes[row['scene_id']]
            unread = [c for c in scene['caption_ids'] if c not in seen]
            if not unread:
                continue
            if scope['scope'] == 'global' and not coverage['met']:
                unread = [cid for cid in unread if any(self.captions[self.positions[cid]]['interval'][0] < hi
                    and self.captions[self.positions[cid]]['interval'][1] > lo for lo, hi in coverage['missing'])]
                if not unread:
                    continue
            if scope['scope'] == 'multi_scene' and not coverage['met'] and scene['id'] in opened:
                continue
            candidates.append(dict(row, unread_caption_ids=unread))
        return candidates
