"""Question/gap retrieval with lexical postings and bounded action windows.

Postings are text locations, not physical-identity assertions. Only selected
captions are read by the planner/extractor/QA model.
"""
from collections import defaultdict
import re

from .accounting import metered_cpu
from .adaptive_sampling import Sampler


STOP = set('a an the this that these those of to in on at for and or is are was were '
           'what which who how does do did video person c main overall'.split())


def terms(text):
    return set(re.findall(r'\w+', text.casefold())) - STOP


def required_scope(question, proposed='local'):
    # Conservative guards: ordinal/count/last claims cannot use local sampling
    # as proof of completeness. A future version may accept an explicit bound.
    if re.search(r'\b(how many|total|every|all occurrences|first time|second|third|last|final time)\b|第[一二三四五六七八九十\d]+次|最后|总共|多少次', question, re.I):
        return 'exhaustive'
    if proposed != 'exhaustive' and re.search(r'\b(main|overall|throughout|entire|purpose|strategy)\b|主要|整体|全程|目的', question, re.I):
        return 'global'
    return proposed


def needs_window(question):
    return bool(re.search(r'\b(before|after|during|hand|handed|transfer|give|gave|open|close|cut|change|happen|happens|why|process)\b|交接|之前|之后|过程|变化|为什么', question, re.I))


class HybridSampler(Sampler):
    @metered_cpu('optimized_candidate_index')
    def __init__(self, directory, features, question_vector, question):
        super().__init__(directory, features, question_vector)
        self.question_text = question
        postings = defaultdict(list)
        for c in self.captions:
            for word in sorted(terms(c['text'])):
                postings[word].append(c['id'])
        self.postings = dict(postings)
        self.ownership = {cid: s['id'] for s in directory['scenes'] for cid in s['caption_ids']}

    def scores(self):
        visual = self.features @ self.query
        original, gap = terms(self.question_text), terms(self.gap)
        lexical = defaultdict(float)
        for words, weight in ((original, .7), (gap, .3)):
            for word in words:
                for cid in self.postings.get(word, []):
                    lexical[cid] += weight / max(1, len(words))
        return {c['id']: .75 * float(visual[i]) + .25 * lexical[c['id']]
                for i, c in enumerate(self.captions)}

    @metered_cpu('optimized_scene_ranking')
    def rank_scenes(self, allowed=None):
        allowed = set(self.scenes if allowed is None else allowed)
        scores = self.scores()
        rows = [dict(scene_id=s['id'], score=.5 * float(self.centers[i] @ self.query)
                     + .5 * max(scores[c] for c in s['caption_ids']))
                for i, s in enumerate(self.directory['scenes']) if s['id'] in allowed]
        return sorted(rows, key=lambda r: (-r['score'], r['scene_id']))

    @metered_cpu('optimized_batch_read')
    def read_batch(self, seen, opened, *, count=8, max_scenes=4, temporal=False,
                   preferred=(), scope='local', source_priority=()):
        scores = self.scores()
        # Bounded preference cannot displace arbitrary question-relevant scenes.
        scene_rank = {r['scene_id']: i for i, r in enumerate(self.rank_scenes())}
        preferred = set(preferred) & {sid for sid, rank in scene_rank.items() if rank < 3}
        priority = set(source_priority)
        eligible = [c['id'] for c in self.captions if c['id'] not in seen]
        coverage = self.coverage(dict(scope=scope), seen)
        if scope == 'global' and not coverage['met']:
            missing = [cid for cid in eligible if any(
                self.captions[self.positions[cid]]['interval'][0] < hi and
                self.captions[self.positions[cid]]['interval'][1] > lo
                for lo, hi in coverage['missing'])]
            # Missing thirds come first; all ordering within them remains query-directed.
            missing = set(missing)
        else:
            missing = set()
        ranked = sorted(eligible, key=lambda c: (c not in missing,
            -(scores[c] + .03 * (self.ownership[c] in preferred) + .03 * (c in priority)), self.positions[c]))
        selected = []

        def add(cid):
            if cid in seen or len(selected) >= count:
                return
            sid = self.ownership[cid]
            if sid not in opened:
                if len(opened) >= max_scenes:
                    return
                opened.append(sid)
            seen.add(cid)
            selected.append(cid)

        for anchor in ranked:
            if len(selected) >= count:
                break
            if anchor in seen:
                continue
            add(anchor)
            if anchor not in seen:
                continue
            if temporal:
                i = self.positions[anchor]
                # Expand around a QUESTION-ranked anchor, including an adjacent
                # scene only within the same scene-count/reading budget.
                for delta in (-1, 1, -2, 2):
                    j = i + delta
                    if 0 <= j < len(self.captions):
                        add(self.captions[j]['id'])
        self.trace.append(dict(gap=self.gap, selected_caption_ids=selected,
            temporal_context=temporal, policy='question_gap_visual_lexical_windows',
            scores=[scores[c] for c in selected]))
        return selected
