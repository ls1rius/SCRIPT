"""SCRIPT closed-loop navigation, dependency recovery and deterministic stop gates."""
from copy import deepcopy
import json
from pathlib import Path
import time
import math

from .budget import BudgetLedger, Meter, ResourceExhausted, ToolFailure
from .cache import signature
from .config import base_config, digest
from .coverage import checked_intervals, complement, intersect, units, midpoint_observed, midpoint_attempted
from .identity import identity_index
from .materialize import Materializer
from .navigation import baseline_input, delivered_feedback
from .accounting import metered_cpu, directory_stats
from .retrieval import frontier_split
from .query_policy import (normalize_plan, global_checkpoints, spread_order, checked_choice,
                           plan_uncertainties, checked_ambiguities, option_distinctions,
                           option_check_rows, novel_claim_ids, retain_plan_context, query_observation_gaps)
from .caption_context import caption_context
from .answering import reconcile_answer, prepare_answer_inputs, restore_answer_order
from .state import DYNAMIC, PREDICATE_ROLES, Memory, Store, obj, valid_interval, write_json, write_jsonl


def validate_query(row, base, cfg, metadata):
    allowed = {'id', 'question_id', 'schema_version', 'revision', 'video_id', 'question', 'options',
               'scope', 'answer_mode', 'options_visible_to_retrieval', 'total_cap'}
    if set(row)-allowed:
        raise ValueError('Question input contains unsupported fields or evaluation annotations')
    id_ = row.get('question_id', row.get('id'))
    if not isinstance(id_, str) or not id_ or not isinstance(row.get('question'), str) or not row['question'].strip():
        raise ValueError('question_id and nonempty question are required')
    options = row.get('options', [])
    if not isinstance(options, list) or any(not isinstance(o, str) for o in options):
        raise ValueError('options must be a list of text strings')
    mode = row.get('answer_mode', 'choice' if options else 'open')
    if mode not in ('choice', 'open') or (mode == 'choice' and len(options) < 2):
        raise ValueError('Invalid answer mode or choice list')
    scope = row.get('scope', [0., base.manifest['cutoff']])
    if row['video_id'] != base.manifest['video_id'] or not valid_interval(scope, base.manifest['cutoff']):
        raise ValueError('Query video or scope does not match base')
    if metadata['base_config_hash'] != digest(base_config(cfg)):
        raise ValueError('Query models/initialization/segmentation differ from frozen M0')
    cap = row.get('total_cap', metadata['initialization_cost']+cfg['budget']['online_cap'])
    if not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0:
        raise ValueError('total_cap must be positive and finite')
    return obj(id_, video_id=row['video_id'], question=row['question'], options=options, scope=scope,
               answer_mode=mode, options_visible_to_retrieval=row.get('options_visible_to_retrieval', False), total_cap=float(cap))


@metered_cpu('query_plan_validation')
def load_plan(memory, query, report):
    if not isinstance(report['requirements'], list) or not report['requirements']:
        raise ToolFailure('Question parser produced no requirements')
    variables = report['variables']
    if not isinstance(variables, list) or any(not isinstance(v, dict) or not v.get('name') or 'description' not in v or 'constraints' not in v for v in variables):
        raise ToolFailure('Invalid query variables')
    keys = [r.get('key') for r in report['requirements']]
    if any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
        raise ToolFailure('Parser requirement keys must be unique strings')
    ids = {key: memory.new_id(query['id'], 'requirement') for key in keys}
    for row in report['requirements']:
        required = {'key', 'predicate', 'roles', 'scope', 'quantifier', 'prerequisites', 'acceptable_modalities', 'answer_slot'}
        if required-set(row) or row['predicate'] not in PREDICATE_ROLES:
            raise ToolFailure('Invalid parsed Requirement')
        if intersect(query['scope'], row['scope']) != row['scope']:
            raise ToolFailure('Parser widened the original query scope')
        if not set(row['prerequisites']) <= set(keys):
            raise ToolFailure('Unresolved requirement prerequisite')
        if str(row['answer_slot']).lower() in ('color', 'colour', '颜色', '材质', 'material') and row['predicate'] != 'other':
            raise ToolFailure('颜色或材质是属性需求：predicate必须为other，roles只用object；不要把属性误解析成at空间关系')
        if set(row['roles'].values()) - {v['name'] for v in variables}:
            raise ToolFailure('Requirement references an undefined variable')
        entity_roles = [v for k, v in row['roles'].items() if k != 'content']
        if row['predicate'] != 'other' and len(entity_roles) != len(set(entity_roles)):
            raise ToolFailure('Distinct participant/object roles must not reuse one variable')
        if not set(row['acceptable_modalities']) <= {'visual', 'subtitle', 'audio'} or not row['acceptable_modalities']:
            raise ToolFailure('Requirement contains an invalid acceptable modality')
        if row['predicate'] != 'other' and set(row['roles']) != set(PREDICATE_ROLES[row['predicate']]):
            raise ToolFailure('Requirement role names do not match the predicate contract')
        memory.put('requirements', obj(ids[row['key']], predicate=row['predicate'], roles=row['roles'], scope=row['scope'],
                                        quantifier=row['quantifier'], prerequisite_ids=[ids[k] for k in row['prerequisites']],
                                        acceptable_modalities=row['acceptable_modalities'], answer_slot=row['answer_slot'],
                                        support_sets=[], status='unresolved'))
    memory.validate()
    return variables


def extraction_scope(requirement, strategy='goal_lazy'):
    if strategy in ('scene_lazy', 'eager'):
        predicates = list(PREDICATE_ROLES)
        mode = 'all'
    else:
        table = {'at': ['at', 'place', 'hold', 'transfer'], 'place': ['place', 'at', 'hold'],
                 'transfer': ['transfer', 'hold']}
        predicates = table.get(requirement['predicate'], [requirement['predicate']])
        mode = 'goal'
    return {'predicates': predicates, 'target_occurrence_ids': [], 'include_context': True, 'mode': mode}


@metered_cpu('structural_gate')
def structural_gate(memory, query, variables, ambiguities, cfg):
    gaps = []
    if ambiguities:
        gaps.extend(f'ambiguity:{a}' for a in ambiguities)
    for r in memory.tables['requirements'].values():
        if not memory.is_valid(r['id']):
            gaps.append(r['id'])
            continue
        if r['quantifier'] in ('last', 'all', 'count', 'negative'):
            scope = list(r['scope'])
            if r['quantifier'] == 'last':
                facts = [memory.get(c) for g in r['support_sets'] for c in g
                         if c in memory.tables['claims'] and memory.is_valid(c)]
                if facts:
                    scope[0] = max(c['interval'][1] for c in facts)
            if scope[0] < scope[1] and complement(scope, checked_intervals(memory, r['id'])):
                gaps.append('coverage:'+r['id'])
            if r['quantifier'] in ('all', 'count'):
                if any(c['duplicate_group'] and memory.is_valid(c['id']) for c in memory.tables['claims'].values()
                       if c['predicate'] == r['predicate']):
                    gaps.append('duplicates:'+r['id'])
        if r['quantifier'] == 'global':
            if r['answer_slot'] == 'temporal_pattern':
                facts = [memory.get(ref) for group in r['support_sets'] for ref in group
                         if ref in memory.tables['claims'] and memory.is_valid(ref)]
                if not any(a['interval'][1] <= b['interval'][0] or b['interval'][1] <= a['interval'][0]
                           for i, a in enumerate(facts) for b in facts[i+1:]):
                    gaps.append('temporal_comparison:'+r['id'])
            summaries = [s for s in memory.tables['summaries'].values()
                         if intersect(memory.get(s['scene_id'])['interval'], r['scope'])]
            if any(s['status'] != 'current' for s in summaries):
                gaps.append('summaries:'+r['id'])
            observed = [e for e in memory.tables['evidence'].values() if e.get('online')]
            for i, midpoint in enumerate(global_checkpoints(r['scope'], cfg)):
                if not any(midpoint_observed(e, midpoint, cfg) for e in observed):
                    gaps.append(f'global_bucket:{r["id"]}:{i}')
    if cfg['construction']['strategy'] == 'scene_lazy':
        entered = {s for b in memory.tables['build_records'].values() for s in b['scene_ids']}
        for scene_id in entered:
            scene = memory.get(scene_id)
            completed = [b['interval'] for b in memory.tables['build_records'].values()
                         if scene_id in b['scene_ids'] and b['status'] == 'complete' and b['extraction_scope']['mode'] == 'all']
            if complement(intersect(scene['interval'], query['scope']) or scene['interval'], completed):
                gaps.append('scene_build:'+scene_id)
    return not gaps, gaps


class Navigator:
    def __init__(self, memory, query, metadata, cfg, meter, variables, state):
        self.memory, self.query, self.metadata, self.cfg, self.meter = memory, query, metadata, cfg, meter
        self.variables, self.state = variables, state

    @metered_cpu('retrieval_maintenance')
    def search(self, text=None):
        text = text or self.query['question']
        if self.query['options_visible_to_retrieval']:
            text += ' '+ ' '.join(self.query['options'])
        out, _ = self.meter.call('embed', {'texts': [text]})
        index = self.metadata['index']
        documents = deepcopy(index['documents'])
        # Corrections mask refuted phrases in this query only; re-embed changed documents.
        vectors = deepcopy(index['vectors'])
        for correction in self.memory.tables['summary_corrections'].values():
            if not self.memory.is_valid(correction['id']):
                continue
            scene_id = self.memory.get(correction['summary_id'])['scene_id']
            i = next(i for i, d in enumerate(documents) if d['id'] == scene_id)
            documents[i]['text'] = documents[i]['text'].replace(correction['affected_text'], correction['replacement_text'] or '')
            updated, _ = self.meter.call('embed', {'texts': [documents[i]['text']]})
            vectors[i] = updated['vectors'][0]
        result, _ = self.meter.call('retrieve', {'documents': documents, 'vectors': vectors,
                                              'query_text': text, 'query_vector': out['vectors'][0],
                                              'scope': self.query['scope'], 'cfg': self.cfg['retrieval']})
        if self.cfg['ablations']['retrieval_entry'] == 'window':
            ranked_windows, _ = self.meter.call('retrieve', {'documents': index['window_documents'],
                'vectors': index['window_vectors'], 'query_text': text, 'query_vector': out['vectors'][0],
                'scope': self.query['scope'], 'cfg': self.cfg['retrieval']})
            scenes = {}
            for row in ranked_windows['data']:
                scene_id = self.memory.get(row['id'])['scene_id']
                scenes.setdefault(scene_id, dict(row, id=scene_id,
                    interval=self.memory.get(scene_id)['interval']))
            result = {'data': list(scenes.values())}
        elif self.cfg['ablations']['retrieval_entry'] in ('event', 'fusion'):
            claims = [c for c in self.memory.tables['claims'].values() if self.memory.is_valid(c['id'])]
            docs = [{'id': c['id'], 'interval': c['interval'], 'text': c['predicate']+' '+c['description']}
                    for c in claims]
            ranked_events = []
            if docs:
                encoded, _ = self.meter.call('embed', {'texts': [d['text'] for d in docs]})
                events, _ = self.meter.call('retrieve', {'documents': docs, 'vectors': encoded['vectors'],
                    'query_text': text, 'query_vector': out['vectors'][0], 'scope': self.query['scope'], 'cfg': self.cfg['retrieval']})
                seen = set()
                for event in events['data']:
                    for scene_id in self.memory.get(event['id'])['scene_ids']:
                        if scene_id not in seen:
                            ranked_events.append(dict(event, id=scene_id, interval=self.memory.get(scene_id)['interval']))
                            seen.add(scene_id)
            if self.cfg['ablations']['retrieval_entry'] == 'event':
                result = {'data': ranked_events}
            else:
                scores = {}
                for ranking in (result['data'], ranked_events):
                    for rank, row in enumerate(ranking, 1):
                        scores[row['id']] = scores.get(row['id'], 0.)+1/(self.cfg['retrieval']['rrf_constant']+rank)
                result = {'data': [{'id': sid, 'score': score, 'interval': self.memory.get(sid)['interval']}
                                  for sid, score in sorted(scores.items(), key=lambda row: (-row[1], row[0]))]}
        self.state['rankings'] = result['data']
        self.state['search_version'] = self.state.get('search_version', 0)+1
        return result['data']

    def requirement_needs_coverage(self, r):
        return r['quantifier'] in ('all', 'count', 'negative', 'global') or (memory_valid(self.memory, r) and r['quantifier'] == 'last')

    def request_signature(self, requirement, interval, spec, scope, bindings):
        relevant = [b for b in bindings if b['variable'] in requirement['roles'].values()]
        target = signature([v for v in self.variables if v['name'] in requirement['roles'].values()], scope['predicates'], relevant,
                           {k: requirement[k] for k in ('predicate', 'quantifier', 'answer_slot', 'acceptable_modalities')})
        return target, digest({'requirement': requirement['id'], 'interval': interval, 'spec': spec,
                               'target': target, 'scope': scope, 'search_version': 0,
                               'partial_progress': sorted({o for b in self.memory.tables['build_records'].values()
                                    if b['interval'] == interval and b['target_signature'] == target and b['status'] == 'partial'
                                    for o in b.get('processed_occurrence_ids', [])})})

    @metered_cpu('candidate_maintenance')
    def candidates(self):
        memory, cfg = self.memory, self.cfg
        bindings = [b for b in memory.tables['bindings'].values() if memory.is_valid(b['id'])]
        active_requirements = [r for r in memory.tables['requirements'].values()
                               if all(memory.is_valid(p) for p in r['prerequisite_ids'])]
        rankings = self.state.get('rankings', [])
        candidates = []
        for requirement in active_requirements:
            completed = memory.is_valid(requirement['id'])
            need_coverage = self.requirement_needs_coverage(requirement)
            if completed and not need_coverage and cfg['construction']['strategy'] != 'scene_lazy' and not self.state.get('semantic_gaps'):
                continue
            scope = intersect(requirement['scope'], self.query['scope'])
            if not scope:
                continue
            inspection_rank = {}
            if need_coverage and requirement['quantifier'] != 'global':
                if requirement['quantifier'] == 'last':
                    facts = [memory.get(c) for g in requirement['support_sets'] for c in g if c in memory.tables['claims'] and memory.is_valid(c)]
                    if facts:
                        scope[0] = max(c['interval'][1] for c in facts)
                if scope[0] >= scope[1]:
                    continue
                covered = checked_intervals(memory, requirement['id'])
                requests = []
                for core in units(scope, cfg['coverage']['unit_seconds']):
                    if not complement(core, covered):
                        continue
                    context = cfg['coverage']['boundary_context_seconds']
                    interval = [max(self.query['scope'][0], core[0]-context), min(self.query['scope'][1], core[1]+context)]
                    requests.append((interval, core, 'time', 2, [requirement['id']]))
                centers = [(core[0]+core[1])/2 for _, core, *_ in requests]
                for rank, index in enumerate(spread_order(centers)):
                    interval, core, *_ = requests[index]
                    sampled = any(e.get('online') and midpoint_observed(e, centers[index], cfg)
                                  for e in memory.tables['evidence'].values())
                    inspection_rank[tuple(interval)] = rank+(len(requests) if sampled else 0)
            elif need_coverage and requirement['quantifier'] == 'global':
                requests = []
                checkpoints = global_checkpoints(scope, cfg)
                if self.state.get('script_guided'):
                    for rank, check in enumerate(self.state['read_plan']['checks']):
                        window = memory.get(check['window_id'])
                        interval = intersect(window['interval'], scope)
                        if interval:
                            requests.append((interval, None, 'script', 1, [window['id']]))
                            inspection_rank[tuple(interval)] = rank
                for rank, i in enumerate([] if self.state.get('script_guided') else spread_order(checkpoints)):
                    midpoint = checkpoints[i]
                    if any(e.get('online') and midpoint_attempted(e, midpoint, cfg) for e in memory.tables['evidence'].values()):
                        continue
                    window = next(w for w in memory.tables['windows'].values() if w['interval'][0] <= midpoint < w['interval'][1])
                    interval = intersect(window['interval'], scope)
                    requests.append((interval, None, 'time', 2, [window['id']]))
                    inspection_rank[tuple(interval)] = rank
                # Once the shared overview is read, use the remaining rounds to resolve
                # semantic/option gaps through ordinary ranked windows.
                if not requests and not self.state.get('script_guided'):
                    distinctions = option_distinctions(self.query['options'])
                    text = self.query['question']+' '+' '.join(self.state.get('option_evidence_gaps', []))+' '+' '.join(
                        row['difference_text'] for row in distinctions)
                    rank_key = digest(['semantic_gaps', text, scope])
                    ranked = self.state.setdefault('window_rankings', {}).get(rank_key)
                    if ranked is None:
                        encoded, _ = self.meter.call('embed', {'texts': [text]})
                        result, _ = self.meter.call('retrieve', {'documents': self.metadata['index']['window_documents'],
                            'vectors': self.metadata['index']['window_vectors'], 'query_text': text,
                            'query_vector': encoded['vectors'][0], 'scope': scope, 'cfg': cfg['retrieval']})
                        ranked = result['data']
                        self.state['window_rankings'][rank_key] = ranked
                    for rank, row in enumerate(ranked):
                        window = memory.get(row['id'])
                        interval = intersect(window['interval'], scope)
                        if interval:
                            requests.append((interval, None, 'summary', 1, [window['id']]))
                            inspection_rank[tuple(interval)] = rank
            else:
                requests = []
                index = self.metadata['index']
                # Ranking scene-internal descriptions is paid and uses the same dense+BM25 retrieval.
                rank_key = digest([requirement['id'], scope, self.query['question']])
                stored = self.state.setdefault('window_rankings', {}).get(rank_key)
                if stored is None:
                    out, _ = self.meter.call('embed', {'texts': [self.query['question']+' '+requirement['predicate']]})
                    ranked, _ = self.meter.call('retrieve', {'documents': index['window_documents'],
                                                           'vectors': index['window_vectors'], 'query_text': self.query['question'],
                                                           'query_vector': out['vectors'][0], 'scope': scope, 'cfg': cfg['retrieval']})
                    self.state['window_rankings'][rank_key] = ranked['data']
                else:
                    ranked = {'data': stored}
                rank_lookup = {w['id']: i for i, w in enumerate(ranked['data'])}
                ordered_scenes = [r['id'] for r in rankings]
                if cfg['construction']['strategy'] == 'scene_lazy':
                    entered = self.state.get('scene_requests', {})
                    pending = [s for s, status in entered.items() if status == 'pending']
                    if pending:
                        ordered_scenes = pending
                    elif completed:
                        ordered_scenes = []
                for scene_id in ordered_scenes:
                    scene = memory.get(scene_id)
                    if not intersect(scene['interval'], scope):
                        continue
                    ws = sorted((memory.get(w) for w in scene['window_ids']), key=lambda w: (rank_lookup.get(w['id'], 1e9), w['interval'][0]))
                    for window in ws:
                        interval = intersect(window['interval'], scope)
                        if interval:
                            if cfg['construction']['strategy'] == 'scene_lazy' and any(
                                b['status'] == 'complete' and b['extraction_scope']['mode'] == 'all'
                                and intersect(interval, b['interval']) == interval
                                for b in memory.tables['build_records'].values()):
                                continue
                            requests.append((interval, None, 'summary', 1, [window['id'], scene['summary_id']]))
                # Known identity routes provide candidates; absent entries never delete summary/time routes.
                for binding in bindings:
                    if binding['variable'] not in requirement['roles'].values():
                        continue
                    indexed = identity_index(memory, binding['entity_id'], scope, requirement['id'])
                    for occurrence_id, link_id in zip(indexed['occurrence_ids'], indexed['link_ids']):
                        occurrence = memory.get(occurrence_id)
                        windows_ = [memory.get(w) for w in memory.get(occurrence['scene_id'])['window_ids']]
                        for window in windows_:
                            interval = intersect(window['interval'], scope)
                            if interval and any(intersect(interval, v) for v in occurrence['visible_intervals']):
                                requests.append((interval, None, 'entity', 1, [link_id]))
                # Feedback may nominate another route, but only with already readable source IDs.
                for lead in self.state.get('last_result', {}).get('next_leads', []):
                    if lead['requirement_id'] != requirement['id']:
                        continue
                    scene = memory.get(lead['scene_id'])
                    for window_id in scene['window_ids']:
                        interval = intersect(memory.get(window_id)['interval'], scope)
                        if interval:
                            requests.append((interval, None, lead['route'], 1, lead['basis_refs']))
                # Empty or failed captions remain reachable through the independent time directory.
                for scene in sorted(memory.tables['scenes'].values(), key=lambda s: s['interval'][0]):
                    if cfg['construction']['strategy'] == 'scene_lazy' and completed:
                        continue
                    for window_id in scene['window_ids']:
                        interval = intersect(memory.get(window_id)['interval'], scope)
                        if interval:
                            requests.append((interval, None, 'time', 3, [window_id]))
            scope_obj = extraction_scope(requirement, cfg['construction']['strategy'])
            for interval, core, route, priority, basis in requests:
                if any(e.get('online') and e['frame_inputs']
                       and not any(f.get('visual_usable', True) for f in e['frame_inputs'])
                       and intersect(interval, e['interval']) == interval
                       for e in memory.tables['evidence'].values()):
                    continue
                if route == 'entity' and not cfg['ablations']['entity_navigation']:
                    continue
                if route == 'time' and not cfg['ablations']['time_navigation']:
                    continue
                scene_ids = [s['id'] for s in memory.tables['scenes'].values() if intersect(s['interval'], interval)]
                spec = dict(cfg['observation'])
                target, request_key = self.request_signature(requirement, interval, spec, scope_obj, bindings)
                if request_key in self.state['visited']:
                    continue
                expected_cost = min(cfg['budget']['local_task_cap'], self.meter.ledger.remaining)
                if expected_cost < .001:
                    continue
                id_ = self.query['id']+':candidate:'+request_key[:20]+':'+route
                if id_ in memory.tables['candidate_queue']:
                    candidate = memory.get(id_)
                    if candidate['status'] == 'invalidated':
                        continue
                    if candidate['status'] != 'pending':
                        memory.update('candidate_queue', id_, status='pending')
                    rank = inspection_rank.get(tuple(interval), 0)
                    if candidate.get('navigation_rank', 0) != rank:
                        memory.update('candidate_queue', id_, navigation_rank=rank, local_rank=rank)
                    candidates.append(memory.get(id_))
                    continue
                branch_id = self.query['id']+':branch:'+digest([scene_ids, requirement['id']])[:20]
                if branch_id not in memory.tables['branches']:
                    memory.put('branches', obj(branch_id, parent_branch_id=None, hypothesis_ids=[b['id'] for b in bindings],
                                               requirement_ids=[requirement['id']], candidate_ids=[], status='active', no_progress_count=0))
                stalled = memory.get(branch_id)['status'] == 'stalled'
                if stalled and not cfg['ablations']['global_backtrack']:
                    continue
                candidate = obj(id_, scene_ids=scene_ids, requirement_id=requirement['id'], branch_id=branch_id,
                                goal_type='coverage' if core else 'relation', route=route, basis_refs=basis,
                                binding_ids=[b['id'] for b in bindings if b['variable'] in requirement['roles'].values()],
                                priority_class=(1 if cfg['method'] == 'B0' else 3 if stalled else priority),
                                basis_rank=(2 if cfg['method'] == 'B0' else 0 if route == 'entity' else 2),
                                expected_cost=expected_cost, interval=interval, status='pending',
                                navigation_rank=inspection_rank.get(tuple(interval), 0),
                                request_key=request_key, target_signature=target, extraction_scope=scope_obj, core=core,
                                local_rank=min((rank_lookup.get(ref, 1e9) for ref in basis), default=1e9)
                                           if not need_coverage else inspection_rank.get(tuple(interval), 0))
                memory.put('candidate_queue', candidate)
                candidates.append(candidate)
        # Preserve all deferred candidates; the bounded frontier is only a selection view.
        frontier, deferred = frontier_split(candidates, cfg['retrieval']['frontier_max'])
        for c in deferred:
            if memory.get(c['id'])['status'] != 'deferred':
                memory.update('candidate_queue', c['id'],
                              status='deferred' if cfg['ablations']['retain_candidates'] else 'invalidated')
        self.state['deferred'] = [c['id'] for c in deferred]
        self.state['frontier'] = [c['id'] for c in frontier]
        return frontier

    @metered_cpu('navigation_maintenance')
    def choose(self, frontier):
        selected = frontier[0]
        if self.state['global_round'] == 0:
            scene_ranks = {r['id']: i for i, r in enumerate(self.state.get('rankings', []))}
            # Initial entry follows summary retrieval; feedback rounds use the candidate priority tuple.
            selected = min(frontier, key=lambda c: (c['priority_class'],
                           min(scene_ranks.get(s, 1e9) for s in c['scene_ids']), c.get('local_rank', 1e9), c['interval'][0], c['id']))
        if selected['route'] == 'summary' and selected['goal_type'] != 'coverage':
            # Global selection chooses a scene; its highest-ranked unvisited local window may be
            # outside the bounded global frontier. Deferred entries remain available for this selection.
            local = [c for c in self.memory.tables['candidate_queue'].values()
                     if c['scene_ids'] == selected['scene_ids'] and c['requirement_id'] == selected['requirement_id']
                     and c['route'] == 'summary' and c['status'] in ('pending', 'deferred')
                     and c['request_key'] not in self.state['visited'] and c['target_signature'] == selected['target_signature']]
            if local:
                selected = min(local, key=lambda c: (c.get('local_rank', 1e9), c['interval'][0], c['id']))
        period = self.cfg['query']['explore_every_nonexploration_tasks']
        if self.state.get('nonexploration_tasks', 0) >= period:
            unexplored = [c for c in frontier if c['route'] in ('time', 'summary', 'recovery')
                          and not any(s in self.state.get('visited_scenes', []) for s in c['scene_ids'])]
            if unexplored:
                selected = unexplored[0]
                self.state['nonexploration_tasks'] = 0
                self.memory.update('candidate_queue', selected['id'], priority_class=3)
                selected = self.memory.get(selected['id'])
        requirement = self.memory.get(selected['requirement_id'])
        self.state['planned_candidate_id'] = selected['id']
        inputs = {'question': self.query['question'], 'requirement': requirement,
                  'options': self.query['options'], 'unresolved': self.state.get('semantic_gaps', []),
                  'observation_gaps': self.state.get('last_result', {}).get('observation_gaps', []),
                  'variables': self.variables, 'bindings': [b for b in self.memory.tables['bindings'].values() if self.memory.is_valid(b['id'])],
                  'candidate': selected,
                  'summary': [self.memory.get(self.memory.get(s)['summary_id']) for s in selected['scene_ids']]}
        if selected['route'] == 'script':
            check = next(c for c in self.state['read_plan']['checks'] if c['window_id'] in selected['basis_refs'])
            report = {'goal': check['goal'], 'acceptance_checks': [
                'Record only visible details that distinguish the candidate actions; do not infer a purpose.']}
        elif self.cfg['method'] in ('B0', 'B1', 'B1S'):
            # Flat variants share every tool/state field, but choose and execute without goal handoff.
            report, _ = self.meter.json('flat_plan', baseline_input(self.cfg['method'], inputs, frontier,
                                                                  self.memory, self.state),
                                        ('candidate_id', 'goal', 'acceptance_checks'))
            selected = next((c for c in frontier if c['id'] == report['candidate_id']), selected)
        elif not self.cfg['ablations']['explicit_local_goal']:
            report = {'goal': self.query['question'], 'acceptance_checks': []}
        elif requirement['quantifier'] == 'global' and not requirement['roles']:
            # A scope-independent observation goal needs no model call per time bin.
            report = {'goal': self.query['question'], 'acceptance_checks': [
                'Record visible actions and context in the authorized window.',
                'Compare the actions and purposes that distinguish the choices.']}
        else:
            report, _ = self.meter.json('goal', inputs, ('goal', 'acceptance_checks'))
        self.state['planned_candidate_id'] = selected['id']
        task = obj(self.memory.new_id(self.query['id'], 'task'), candidate_id=selected['id'],
                   scene_ids=selected['scene_ids'], allowed_interval=selected['interval'],
                   requirement_ids=[selected['requirement_id']], target_binding_ids=selected['binding_ids'],
                   goal=report['goal'], acceptance_checks=report['acceptance_checks'], route=selected['route'],
                   basis_refs=selected['basis_refs'], relevant_versions={b: self.memory.get(b)['revision'] for b in selected['binding_ids']},
                   max_new_observations=self.cfg['query']['new_observations_per_task'],
                   cost_cap=min(self.cfg['budget']['local_task_cap'], self.meter.ledger.remaining),
                   extraction_scope=selected['extraction_scope'], target_signature=selected['target_signature'],
                   question=self.query['question'], variables=self.variables)
        task['options'] = self.query['options']
        task['observation_gaps'] = inputs['observation_gaps']
        if selected['route'] == 'script':
            task['max_new_observations'] = 1
        task['execution_mode'] = 'local_executor' if self.cfg['method'] in ('B2', 'B3') else 'global_tools'
        self.memory.put('tasks', task)
        requests = [{'interval': selected['interval'], 'core': selected.get('core')}]
        if self.cfg['ablations']['observation_policy'] == 'uniform_dense':
            requests[0]['fps'] = self.cfg['observation']['dense_fps']
        if self.cfg['construction']['strategy'] == 'scene_lazy' and not selected.get('core'):
            scene_id = selected['scene_ids'][0]
            scene = self.memory.get(scene_id)
            self.state.setdefault('scene_requests', {})[scene_id] = 'pending'
            allowed = intersect(scene['interval'], self.query['scope'])
            task = self.memory.update('tasks', task['id'], allowed_interval=allowed, scene_ids=[scene_id])
            complete_intervals = [b['interval'] for b in self.memory.tables['build_records'].values()
                                  if scene_id in b['scene_ids'] and b['status'] == 'complete' and b['extraction_scope']['mode'] == 'all']
            requests = [{'interval': interval} for wid in scene['window_ids']
                        if (interval := intersect(self.memory.get(wid)['interval'], allowed))
                        and complement(interval, complete_intervals)][:task['max_new_observations']]
        # A second, denser input is chosen only after feedback shows an observation gap.
        return selected, task, requests


def memory_valid(memory, requirement):
    return memory.is_valid(requirement['id'])


def answer_query(base, row, metadata, cfg, backend, output, initial_details=None, resume=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    query = validate_query(row, base, cfg, metadata)
    if (output / 'query.json').exists():
        existing = json.loads((output / 'query.json').read_text())
        if existing != query or not resume:
            raise ValueError('Query output already exists; use matching --resume or a new directory')
    else:
        write_json(output / 'query.json', query)
    from .replay import ReplayBackend
    mode = ('recorded_calls' if backend.replay_only else 'fixed_observation_replay') if isinstance(backend, ReplayBackend) else 'live'
    if resume and (output / 'result.json').exists():
        completed = json.loads((output / 'result.json').read_text())
        if (completed['config_hash'] != digest(cfg) or completed['base_hash'] != metadata['base_hash']
                or completed.get('experiment_mode', 'live') != mode):
            raise ValueError('Resume configuration, M0 or replay protocol mismatch')
        if completed.get('answer') and completed.get('stop_decision') and not completed.get('error'):
            return completed
    started = time.monotonic()
    envelope = {'answer': None, 'stop_decision': None, 'error': None, 'config_hash': digest(cfg),
                'base_hash': metadata['base_hash'], 'development_run': True, 'experiment_mode': mode}
    try:
        ledger = BudgetLedger(query['total_cap'], metadata['initialization_cost'], cfg['budget']['final_answer_reserve'], output / 'calls.jsonl')
    except ResourceExhausted as exc:
        envelope['error'] = {'code': 'infeasible', 'message': str(exc)}
        envelope['cost_summary'] = {'initialization': metadata['initialization_cost'], 'online': 0., 'total': metadata['initialization_cost']}
        write_json(output / 'result.json', envelope)
        return envelope
    meter = Meter(backend, ledger, cfg, output / 'raw_calls')
    # These are held inside the unchanged online cap, not added to it. A local
    # timeout may kill the worker, so finishing must also afford a cold reload.
    ledger.warmup_reserve = min(cfg['budget']['tool_call_cap'], cfg['budget']['online_cap']/10)
    ledger.cleanup_reserve = min(10., cfg['budget']['online_cap']/20)
    memory = deepcopy(base)
    if initial_details:
        for table in ('evidence', 'occurrences', 'entities', 'identity_links', 'claims', 'cannot_links',
                      'observations', 'build_records', 'cache_entries'):
            for r in initial_details.tables[table].values():
                if r['id'] not in memory.tables[table]:
                    memory.put(table, r)
    state = {'visited': {}, 'frontier': [], 'deferred': [], 'recovery_round': 0, 'global_round': 0,
             'progress_history': [], 'visited_scenes': [], 'nonexploration_tasks': 0, 'ambiguities': []}
    store = Store(output)
    variables = []
    loaded_resume = False
    try:
        if resume:
            if (output / 'calls.jsonl').exists():
                calls = [json.loads(line) for line in (output / 'calls.jsonl').read_text().splitlines() if line.strip()]
                ledger.calls, ledger.actual = calls, sum(c['actual_cost'] for c in calls)
                ledger.budget_violation = any(c['actual_cost'] > c['reservation']+.05 for c in calls)
            for pending_path in sorted((output / 'raw_calls').glob('*.pending.json')):
                pending = json.loads(pending_path.read_text())
                if not any(c['call_id'] == pending['call_id'] and c['attempt'] == pending['attempt'] for c in ledger.calls):
                    pending.update(actual_cost=pending['reservation'], error='unsettled_after_crash',
                                   started_at=None, ended_at=None, output_hash=None,
                                   input_tokens=0, output_tokens=0, input_pixels=0)
                    ledger.calls.append(pending)
                    ledger.actual += pending['actual_cost']
                    with (output / 'calls.jsonl').open('a') as f:
                        f.write(json.dumps(pending)+'\n')
                pending_path.unlink()
        if resume and store.db.execute('SELECT COUNT(*) FROM objects').fetchone()[0]:
            memory, saved = store.load()
            if saved['config_hash'] != digest(cfg) or saved['query_hash'] != digest(query) or saved['base_hash'] != metadata['base_hash']:
                raise ValueError('Resume configuration, query or M0 mismatch')
            state, variables = saved['state'], saved['variables']
            loaded_resume = True
        else:
            if (output / 'result.json').exists() or store.db.execute('SELECT COUNT(*) FROM objects').fetchone()[0]:
                raise ValueError('Query output already exists; use --resume or a new directory')
            for name in ('base_restore', 'model_snapshot_validation'):
                seconds = metadata.get(name+'_seconds', 0.)
                if seconds:
                    ledger.record(name, {'in_process': True}, ledger.allowance(cfg['budget']['build_call_cap']),
                                  time.monotonic()-seconds,
                                  output={'usage': {'cpu_seconds': metadata.get(name+'_cpu_seconds'), 'gpu_resident_seconds': 0.}})
            plan, _ = meter.json('parse', {'question': query['question'], 'scope': query['scope']},
                                 ('requirements', 'variables', 'ambiguities'))
            for repair_attempt in range(cfg['query']['json_repair_retries']+1):
                try:
                    with memory.transaction():
                        plan = normalize_plan(plan, query)
                        variables = load_plan(memory, query, plan)
                    break
                except (ValueError, ToolFailure) as exc:
                    if repair_attempt == cfg['query']['json_repair_retries']:
                        raise ToolFailure(f'parse_failed: {exc}') from exc
                    constraints = []
                    for requirement in plan.get('requirements', []):
                        if str(requirement.get('answer_slot')).lower() in ('color', 'colour', '颜色', '材质', 'material'):
                            variable = requirement.get('roles', {}).get('object')
                            if variable:
                                constraints.append({'key': requirement['key'], 'predicate': 'other', 'roles': {'object': variable}})
                    if len(plan.get('requirements', [])) != 1:
                        constraints = []
                    repaired, _ = meter.json('repair', {'original_operation': 'parse', 'original_output': {'data': plan},
                                                   'original_inputs': {'question': query['question'], 'scope': query['scope'],
                                                                       'repair_constraints': constraints},
                                                   'errors': str(exc), 'question': query['question'], 'scope': query['scope']},
                                         ('requirements', 'variables', 'ambiguities'))
                    plan = retain_plan_context(repaired, plan)
            state['ambiguities'], state['evidence_gaps'] = plan_uncertainties(plan, query['question'])
            state['task_type'] = plan.get('task_type')
        if loaded_resume:
            for name in ('base_restore', 'model_snapshot_validation'):
                seconds = metadata.get(name+'_seconds', 0.)
                if seconds:
                    ledger.record(name, {'in_process': True}, ledger.allowance(cfg['budget']['build_call_cap']),
                                  time.monotonic()-seconds,
                                  output={'usage': {'cpu_seconds': metadata.get(name+'_cpu_seconds'), 'gpu_resident_seconds': 0.}})
        from .selective import eligible as guided_eligible, prepare_inputs as read_inputs, normalize_plan as normalize_reads, script_outline
        state['script_guided'] = bool(guided_eligible(memory, query, cfg))
        if state['script_guided'] and 'read_plan' not in state:
            state['read_plan'] = {'reason': 'Plan one inspection at a time using verified feedback.', 'checks': []}
            state['read_plan_history'] = []
        navigator = Navigator(memory, query, metadata, cfg, meter, variables, state)
        materializer = Materializer(memory, meter, cfg, query['id'], output)
        materializer.cache.scope = query['scope']
        if initial_details and not state['script_guided'] and not state.get('cached_facts_assessed') and any(memory.is_valid(c) for c in memory.tables['claims']):
            eligible = [r['id'] for r in memory.tables['requirements'].values()
                        if all(memory.is_valid(p) for p in r['prerequisite_ids'])]
            if eligible:
                cache_task = obj(memory.new_id(query['id'], 'task'), candidate_id=None,
                    scene_ids=list(memory.tables['scenes']), allowed_interval=query['scope'], requirement_ids=eligible,
                    target_binding_ids=[], goal=query['question'], acceptance_checks=[], route='cached_facts', basis_refs=[],
                    relevant_versions={}, max_new_observations=0, cost_cap=min(cfg['budget']['local_task_cap'], ledger.remaining),
                    extraction_scope={'predicates': list(PREDICATE_ROLES), 'target_occurrence_ids': [],
                                      'include_context': True, 'mode': 'goal'},
                    target_signature=digest(query['question']), question=query['question'], variables=variables,
                    execution_mode='cached_fact_interpretation')
                memory.put('tasks', cache_task)
                materializer.execute(cache_task, [], interpret=True)
            state['cached_facts_assessed'] = True
        if not state['script_guided'] and any(r['quantifier'] == 'global' for r in memory.tables['requirements'].values()) and not state.get('global_summary_view'):
            summaries = [s for s in memory.tables['summaries'].values() if intersect(memory.get(s['scene_id'])['interval'], query['scope'])]
            if len(summaries) <= cfg['retrieval']['all_summaries_if_scene_count_le']:
                state['global_summary_view'] = {'summaries': summaries, 'source_summary_ids': [s['id'] for s in summaries]}
            else:
                from .build import summarize_all
                aggregate = summarize_all(meter, [{'caption': s['text'], 'uncertainties': s['uncertainties'],
                                                   'window_id': s['id']} for s in summaries], cfg)
                state['global_summary_view'] = {'aggregate': aggregate, 'source_summary_ids': [s['id'] for s in summaries]}
        materializer.global_summary_view = state.get('global_summary_view')
        if not state['script_guided'] and not state.get('rankings'):
            navigator.search()
        steps = []
        if resume and (output / 'steps.jsonl').exists():
            steps = [json.loads(line) for line in (output / 'steps.jsonl').read_text().splitlines() if line.strip()]
        reason, semantic_version, gaps = None, None, []
        while True:
            if state['script_guided']:
                read_limit = min(cfg['query']['max_script_reads'], cfg['query']['global_task_limit'])
                if state['global_round'] >= read_limit:
                    reason = 'exploration_stop'
                    state['exploration_limit'] = 'script_read_limit'
                    break
                if state['global_round'] >= len(state['read_plan']['checks']):
                    inputs = read_inputs(memory, query, cfg, state['read_plan']['checks'],
                                         read_limit-state['global_round'])
                    try:
                        plan, _ = meter.json('read_plan', inputs, ('reason', 'checks'))
                        plan = normalize_reads(plan, inputs)
                        state.setdefault('read_plan_history', []).append(plan)
                        state['read_plan']['reason'] = plan['reason']
                        state['read_plan']['checks'].extend(plan['checks'])
                    except ResourceExhausted:
                        reason = 'resource_stop'
                        state['stop_limit'] = 'processing_budget'
                        break
                    except (ToolFailure, ValueError) as exc:
                        state['read_plan_error'] = str(exc)
                        reason = 'exploration_stop'
                        state['exploration_limit'] = 'script_planning_failed'
                        break
                    if not plan['checks']:
                        reason = 'exploration_stop'
                        state['exploration_limit'] = 'script_read_plan_complete'
                        break
            # Text ambiguities are resolved against evidence by the semantic gate;
            # they must not prevent the resolving gate from ever being invoked.
            structurally_passed, gaps = structural_gate(memory, query, variables, [], cfg)
            gaps.extend('question_ambiguity:'+str(a) for a in state['ambiguities'])
            version = digest([{t: list(memory.tables[t].values()) for t in ('requirements', 'bindings', 'claims', 'identity_links', 'coverage')}, state['ambiguities']])
            if structurally_passed and version != semantic_version:
                try:
                    gate_claims = [c for c in memory.tables['claims'].values() if memory.is_valid(c['id'])]
                    gate, _ = meter.json('gate', {'question': query['question'], 'options': query['options'],
                                                'option_ids': [str(i) for i in range(len(query['options']))],
                                                'requirements': list(memory.tables['requirements'].values()),
                                                'claims': gate_claims,
                                                'coarse_context': caption_context(memory, query, cfg),
                                                'observation_gaps': query_observation_gaps(memory, query['id']),
                                                'bindings': [b for b in memory.tables['bindings'].values() if memory.is_valid(b['id'])],
                                                'coverage': list(memory.tables['coverage'].values()), 'ambiguities': state['ambiguities']},
                                         ('passed', 'unresolved'))
                    semantic_version = version
                    state['option_evidence_gaps'] = [c['reason'] for c in option_check_rows(gate)
                                                     if c['result'] == 'unresolved']
                    choice_passed = checked_choice(gate, query['options'], gate_claims)
                    ambiguities_passed = checked_ambiguities(gate, state['ambiguities'], gate_claims)
                    if gate['passed'] is True and not gate['unresolved'] and choice_passed and ambiguities_passed:
                        reason = 'protocol_sufficient'
                        state['resolved_ambiguities'] = gate.get('ambiguity_checks', [])
                        state['ambiguities'] = []
                        state['selected_option'] = gate.get('selected_option')
                        state['semantic_gaps'] = []
                        break
                    gaps.extend(gate['unresolved'])
                    if not choice_passed:
                        gaps.append('Options are not uniquely distinguished by verified facts')
                    state['semantic_gaps'] = list(gaps)
                except ResourceExhausted:
                    reason = 'resource_stop'
                    state['stop_limit'] = 'processing_budget'
                    break
                except ToolFailure as exc:
                    # An invalid semantic response cannot turn six overview reads
                    # into a successful or terminal decision. Use remaining tasks.
                    semantic_version = version
                    state['last_tool_error'] = str(exc)
                    state.setdefault('gate_failures', []).append(str(exc))
                    gaps.append('semantic_gate_failed:'+str(exc)[:160])
                    state['semantic_gaps'] = list(gaps)
            if state['global_round'] >= cfg['query']['global_task_limit'] or ledger.remaining < .001:
                reason = 'resource_stop'
                state['stop_limit'] = 'global_task_limit' if state['global_round'] >= cfg['query']['global_task_limit'] else 'processing_budget'
                break
            # After the overview and one focused follow-up, repeated descriptions
            # without a new distinction do not justify another identical cycle.
            global_requirements = list(memory.tables['requirements'].values())
            if (query['options'] and global_requirements
                    and all(r['quantifier'] == 'global' and not r['roles'] for r in global_requirements)
                    and state['global_round'] > len(global_checkpoints(query['scope'], cfg))
                    and state.get('semantic_no_progress', 0) >= cfg['query']['no_progress_before_backtrack']
                    and semantic_version is not None
                    and all(any(e.get('online') and midpoint_observed(e, point, cfg)
                                for e in memory.tables['evidence'].values())
                            for point in global_checkpoints(query['scope'], cfg))):
                reason = 'exploration_stop'
                state['exploration_limit'] = 'no_new_discriminating_evidence'
                break
            try:
                round_before = state['global_round']
                task = None
                state.pop('planned_candidate_id', None)
                frontier = navigator.candidates()
                if not frontier:
                    if cfg['ablations']['global_backtrack'] and state['recovery_round'] < cfg['query']['global_recovery_rounds']:
                        state['recovery_round'] += 1
                        additions = ' '.join(d for e in memory.tables['entities'].values() for d in e['descriptors']) if state['recovery_round'] == 1 else ''
                        navigator.search(query['question']+' '+additions)
                        frontier = navigator.candidates()
                    if not frontier:
                        reason = 'exploration_stop'
                        break
                candidate, task, requests = navigator.choose(frontier)
                hierarchical = cfg['method'] in ('B2', 'B3')
                result = materializer.execute(task, requests, interpret=hierarchical and not state['script_guided'])
                # Temporal refinement addresses motion; bound targets may instead need a crop.
                if (result['task_status'] in ('partial', 'not_found') and task['max_new_observations'] > 1
                        and len(requests) == 1 and cfg['construction']['strategy'] == 'goal_lazy'
                        and (set(task['extraction_scope']['predicates']) & DYNAMIC or task['target_binding_ids'])
                        and result['actual_cost'] < task['cost_cap'] and not result['coverage_ids']):
                    remaining_cap = task['cost_cap']-result['actual_cost']
                    second_task = memory.update('tasks', task['id'], max_new_observations=1, cost_cap=remaining_cap)
                    request = {'interval': candidate['interval'], 'core': candidate.get('core'), 'fps': cfg['observation']['dense_fps']}
                    target_occurrences = [oid for bid in task['target_binding_ids'] for oid in memory.get(bid)['anchor_occurrence_ids']]
                    small = []
                    for oid in target_occurrences:
                        for box in memory.get(oid)['boxes']:
                            for eid in result['evidence_ids']:
                                frame = next((f for f in memory.get(eid)['frame_inputs'] if f['frame_id'] == box['frame_id']), None)
                                if frame and min((box['bbox'][2]-box['bbox'][0])*frame['width'],
                                                 (box['bbox'][3]-box['bbox'][1])*frame['height']) < cfg['observation']['min_target_short_side']:
                                    small.append(box['bbox'])
                    if small:
                        crop = [max(0., min(b[0] for b in small)-.02), max(0., min(b[1] for b in small)-.02),
                                min(1., max(b[2] for b in small)+.02), min(1., max(b[3] for b in small)+.02)]
                        request.update(crop=crop, longest_side=None, fps=cfg['observation']['base_fps'])
                    second = materializer.execute(second_task, [request], interpret=hierarchical)
                    changes = {}
                    for field in ('supported_claim_ids', 'refuted_claim_ids', 'evidence_ids', 'coverage_ids',
                                  'new_object_ids', 'build_record_ids', 'cache_hit_ids', 'call_ids'):
                        changes[field] = list(dict.fromkeys(result[field]+second[field]))
                    for field in ('requirement_updates', 'candidate_discriminations', 'next_leads', 'unresolved'):
                        changes[field] = result[field]+second[field]
                    changes['actual_cost'] = result['actual_cost']+second['actual_cost']
                    result = memory.update('local_results', second['id'], **changes)
                if not hierarchical and any(memory.is_valid(c) for c in memory.tables['claims']):
                    # Flat agents interpret directly in the global state. They receive the same observations,
                    # predicates, bindings and tools without the separate bounded local feedback handoff.
                    eligible = [r['id'] for r in memory.tables['requirements'].values()
                                if all(memory.is_valid(p) for p in r['prerequisite_ids'])]
                    flat_report, _ = meter.json('assess', {
                        'question': query['question'], 'goal': task['goal'],
                        'requirements': [memory.get(r) for r in eligible], 'variables': variables,
                        'claims': [c for c in memory.tables['claims'].values() if memory.is_valid(c['id'])],
                        'bindings': [b for b in memory.tables['bindings'].values() if memory.is_valid(b['id'])],
                        'occurrences': list(memory.tables['occurrences'].values()), 'method': cfg['method'],
                        'entities': list(memory.tables['entities'].values()),
                        'identity_links': list(memory.tables['identity_links'].values()),
                        'allowed_scene_ids': list(memory.tables['scenes']), 'allowed_binding_ids': list(memory.tables['bindings'])},
                        ('bindings', 'requirement_updates', 'candidate_discriminations', 'next_leads', 'unresolved'))
                    updates, discriminations, leads = materializer.integrate_assessment(dict(task, requirement_ids=eligible), flat_report)
                    result = memory.update('local_results', result['id'], requirement_updates=updates,
                                           candidate_discriminations=discriminations, next_leads=leads,
                                           task_status='resolved' if all(memory.is_valid(r) for r in task['requirement_ids']) else result['task_status'])
                if cfg['construction']['strategy'] == 'scene_lazy':
                    for scene_id in task['scene_ids']:
                        complete_intervals = [b['interval'] for b in memory.tables['build_records'].values()
                                              if scene_id in b['scene_ids'] and b['status'] == 'complete'
                                              and b['extraction_scope']['mode'] == 'all']
                        allowed = intersect(memory.get(scene_id)['interval'], query['scope'])
                        pending = complement(allowed, complete_intervals) if allowed else []
                        state.setdefault('scene_requests', {})[scene_id] = ('blocked' if result['task_status'] == 'blocked'
                                                                            else 'pending' if pending else 'completed')
                for requirement in list(memory.tables['requirements'].values()):
                    if requirement['quantifier'] not in ('negative', 'all', 'count'):
                        continue
                    covered_units = [c for c in memory.tables['coverage'].values()
                                     if requirement['id'] in c['target_requirement_ids'] and memory.is_valid(c['id'])
                                     and memory.is_valid(c['verifier_ref'])]
                    if complement(requirement['scope'], [c['interval'] for c in covered_units]):
                        continue
                    related_claims = [c for c in memory.tables['claims'].values()
                                      if c['predicate'] == requirement['predicate'] and memory.is_valid(c['id'])
                                      and intersect(c['interval'], requirement['scope'])]
                    valid_bindings = {b['variable']: b for b in memory.tables['bindings'].values() if memory.is_valid(b['id'])}
                    needed_variables = {v for role, v in requirement['roles'].items() if role != 'content'}
                    if needed_variables - set(valid_bindings):
                        continue
                    def matching_roles(claim):
                        return all(role == 'content' or any(l.get('occurrence_id') == claim['roles'].get(role)
                            and l.get('entity_id') == valid_bindings[variable]['entity_id'] and memory.is_valid(ref)
                            for ref, l in [(l['id'], l) for l in memory.tables['identity_links'].values()]+
                            [(n['id'], n['content']) for n in memory.tables['derived_claims'].values()
                             if ':identity_use' in n['id']])
                            for role, variable in requirement['roles'].items())
                    related_claims = [c for c in related_claims if matching_roles(c)]
                    if requirement['quantifier'] == 'negative' and (related_claims or any(c['result'] != 'no_change_seen' for c in covered_units)):
                        continue
                    if any(c['duplicate_group'] for c in related_claims):
                        continue
                    proofs = ([c['id'] for c in covered_units] + [c['id'] for c in related_claims] + requirement['prerequisite_ids']
                              + [valid_bindings[v]['id'] for v in sorted(needed_variables)])
                    if proofs and all(memory.is_valid(p) for p in proofs):
                        memory.update('requirements', requirement['id'], support_sets=[proofs], status='satisfied')
                state['global_round'] += 1
                state['visited'][candidate['request_key']] = result['id']
                state['visited_scenes'] = sorted(set(state['visited_scenes']+candidate['scene_ids']))
                memory.update('candidate_queue', candidate['id'], status='visited')
                checked = [c for c in result['coverage_ids'] if memory.get(c)['level'] == 'protocol_checked']
                novel = novel_claim_ids(memory, result['supported_claim_ids'])
                state['semantic_no_progress'] = 0 if novel else state.get('semantic_no_progress', 0)+1
                progress_refs = result['supported_claim_ids']+checked+[u['requirement_id'] for u in result['requirement_updates'] if u['result'] == 'supported']
                progress_refs += [d['hypothesis_id'] for d in result['candidate_discriminations'] if d['result'] == 'different']
                branch = memory.get(candidate['branch_id'])
                no_progress = 0 if progress_refs else branch['no_progress_count']+1
                memory.update('branches', branch['id'], no_progress_count=no_progress,
                              status='stalled' if no_progress >= cfg['query']['no_progress_before_backtrack'] else 'active')
                state['nonexploration_tasks'] += candidate['priority_class'] != 3
                state['last_result'] = delivered_feedback(result, cfg)
                step = {'round': state['global_round'], 'candidate_id': candidate['id'], 'task_id': task['id'],
                        'novel_claim_ids': novel, 'semantic_no_progress': state['semantic_no_progress'],
                        'interval': candidate['interval'], 'route': candidate['route'], 'result_id': result['id'],
                        'task_status': result['task_status'], 'progress_refs': progress_refs,
                        'cumulative_online_cost': ledger.actual, 'frontier_before': state['frontier'],
                        'recovery_round': state['recovery_round'],
                        'feedback_fields': {k: len(state['last_result'][k]) for k in ('unresolved', 'next_leads')},
                        'execution_mode': task['execution_mode'],
                        'frontier_after': [c['id'] for c in navigator.candidates()] if ledger.remaining > .001 else []}
                steps.append(step)
                state['progress_history'].append(step)
                store.checkpoint(memory, {'state': state, 'variables': variables, 'config_hash': digest(cfg),
                                          'query_hash': digest(query), 'base_hash': metadata['base_hash']})
                write_jsonl(output / 'steps.jsonl', steps)
                print(f'QUERY {query["id"]} round={state["global_round"]} status={result["task_status"]} cost={ledger.actual:.2f}s', flush=True)
            except ResourceExhausted:
                reason = 'resource_stop'
                state['stop_limit'] = 'processing_budget'
                break
            except ToolFailure as exc:
                state['last_tool_error'] = str(exc)
                gaps.append('local_tool_failed:'+str(exc)[:160])
                state['semantic_gaps'] = list(gaps)
                failed = memory.get(state.get('planned_candidate_id'), None)
                if not failed:
                    reason = 'exploration_stop'
                    break
                state['visited'][failed['request_key']] = 'tool_failed'
                memory.update('candidate_queue', failed['id'], status='visited')
                if state['global_round'] == round_before:
                    state['global_round'] += 1
                    steps.append({'round': state['global_round'], 'candidate_id': failed['id'],
                        'task_id': task['id'] if task else None, 'interval': failed['interval'],
                        'route': failed['route'], 'result_id': None, 'task_status': 'blocked',
                        'progress_refs': [], 'cumulative_online_cost': ledger.actual, 'error': str(exc)})
                    write_jsonl(output/'steps.jsonl', steps)
                store.checkpoint(memory, {'state': state, 'variables': variables, 'config_hash': digest(cfg),
                    'query_hash': digest(query), 'base_hash': metadata['base_hash']})
            except ValueError as exc:
                state['last_tool_error'] = str(exc)
                gaps.append('state_validation_failed:'+str(exc)[:160])
                reason = 'resource_stop' if ledger.remaining < .001 else 'exploration_stop'
                break
        decision = obj(memory.new_id(query['id'], 'stop'), reason=reason,
                       evidence_status='sufficient' if reason == 'protocol_sufficient' else 'insufficient',
                       checked_requirement_ids=[r for r in memory.tables['requirements'] if memory.is_valid(r)],
                       remaining_gap_refs=[], gate_hash=digest([cfg['coverage'], cfg['observation'],
                                                               global_checkpoints(query['scope'], cfg)]),
                       actual_cost=ledger.actual, budget_violation=ledger.budget_violation)
        if reason == 'resource_stop':
            decision['resource_limit'] = state.get('stop_limit', 'processing_budget')
        if reason == 'exploration_stop' and state.get('exploration_limit'):
            decision['exploration_limit'] = state['exploration_limit']
        if reason == 'protocol_sufficient' and query['options']:
            decision['candidate_option'] = state['selected_option']
        for gap in gaps:
            if memory.get(gap, None) is not None:
                decision['remaining_gap_refs'].append(gap)
            else:
                gap_record = obj(memory.new_id(query['id'], 'gap'), node_type='query_fact',
                                 content={'reason': gap}, support_sets=[], status='unresolved')
                memory.put('derived_claims', gap_record)
                decision['remaining_gap_refs'].append(gap_record['id'])
        memory.put('stop_decisions', decision)
        envelope['stop_decision'] = decision
        retained_refs = set()
        def collect_support(ref):
            if ref in retained_refs or not memory.is_valid(ref):
                return
            retained_refs.add(ref)
            record = memory.get(ref)
            for group in record.get('support_sets', []):
                if group and all(memory.is_valid(r) for r in group):
                    for r in group:
                        collect_support(r)
            for evidence_ref in record.get('evidence_ids', []):
                collect_support(evidence_ref)
        for requirement_id in memory.tables['requirements']:
            collect_support(requirement_id)
        # A failed whole-question proof does not invalidate independently verified
        # local facts. Keep those facts, their identities and their provenance.
        for claim in memory.tables['claims'].values():
            if memory.is_valid(claim['id']) and intersect(claim['interval'], query['scope']) == claim['interval']:
                collect_support(claim['id'])
        valid_claims = [c for c in memory.tables['claims'].values() if c['id'] in retained_refs and memory.is_valid(c['id'])]
        evidence_ids = sorted(r for r in retained_refs if r in memory.tables['evidence'])
        coarse_context = caption_context(memory, query, cfg)
        answer_inputs = {'question': query['question'], 'options': query['options'], 'answer_mode': query['answer_mode'],
                                        'option_ids': [str(i) for i in range(len(query['options']))],
                                        'claims': valid_claims, 'evidence': [memory.get(e) for e in evidence_ids],
                                        'requirements': list(memory.tables['requirements'].values()),
                                        'bindings': [b for b in memory.tables['bindings'].values() if b['id'] in retained_refs and memory.is_valid(b['id'])],
                                        'identity_links': [l for l in memory.tables['identity_links'].values() if l['id'] in retained_refs and memory.is_valid(l['id'])],
                                        'query_facts': [n for n in memory.tables['derived_claims'].values() if n['id'] in retained_refs and memory.is_valid(n['id'])],
                                        'coarse_context': coarse_context,
                                        'script_outline': script_outline(memory, query, cfg) if state.get('script_guided') else None,
                                        'observation_gaps': query_observation_gaps(memory, query['id']),
                                        'stop_decision': decision}
        presented_inputs, option_map = prepare_answer_inputs(answer_inputs, cfg['query']['answer_strategy'],
                                                             cfg['query']['answer_order'], cfg['seed'])
        report, _ = meter.json('answer', presented_inputs,
                              ('text', 'selected_option', 'claim_ids', 'evidence_ids'), final=True)
        report = restore_answer_order(reconcile_answer(report, presented_inputs), option_map)
        if (not set(report['claim_ids']) <= {c['id'] for c in valid_claims}
                or not set(report['evidence_ids']) <= set(evidence_ids)):
            raise ToolFailure('Final answer cites invalid or unseen evidence')
        if query['answer_mode'] == 'choice' and report['selected_option'] not in [str(i) for i in range(len(query['options']))]:
            raise ToolFailure('Answerer returned an illegal option ID')
        if query['answer_mode'] == 'choice':
            report['explanation'] = report['text']
            report['text'] = query['options'][int(report['selected_option'])]
        if decision.get('candidate_option') is not None:
            if report['selected_option'] != decision['candidate_option']:
                # The full-timeline answer must be allowed to correct a local
                # proof candidate. Disagreement cannot retain sufficient status.
                decision = memory.update('stop_decisions', decision['id'], reason='answer_conflict',
                                         evidence_status='insufficient')
                state['answer_conflict'] = {'gate_option': decision['candidate_option'],
                                            'answer_option': report['selected_option']}
            else:
                decision = memory.update('stop_decisions', decision['id'], selected_option=report['selected_option'])
        if query['answer_mode'] == 'open' and decision['evidence_status'] != 'sufficient':
            report['text'] = '无法从已核验的视频证据确定答案。'
        answer = obj(memory.new_id(query['id'], 'answer'), query_id=query['id'], **report, stop_decision_id=decision['id'])
        memory.put('answers', answer)
        envelope.update(answer=answer, stop_decision=decision)
    except (ToolFailure, ResourceExhausted, ValueError, KeyError, TypeError) as exc:
        envelope['error'] = {'code': 'parse_failed' if not memory.tables['requirements'] else 'tool_failed', 'message': str(exc)}
    finally:
        try:
            store.checkpoint(memory, {'state': state, 'variables': variables, 'config_hash': digest(cfg),
                                      'query_hash': digest(query), 'base_hash': metadata['base_hash']}, final=True)
            envelope['storage'] = directory_stats(output, final=True)
            if 'materializer' in locals():
                envelope['cache_statistics'] = materializer.cache.statistics()
            if envelope['stop_decision']:
                decision = envelope['stop_decision']
                decision = memory.update('stop_decisions', decision['id'], actual_cost=ledger.actual,
                                         budget_violation=ledger.budget_violation)
                envelope['stop_decision'] = decision
            store.finalize_accounting(memory, {'cost_summary': ledger.summary()}, ('stop_decisions',))
        except (ValueError, KeyError, TypeError) as exc:
            envelope['error'] = {'code': 'checkpoint_failed', 'message': str(exc)}
        finally:
            try:
                store.close()
            finally:
                meter.close()
        envelope['cost_summary'] = ledger.summary()
        envelope['diagnostics'] = {'gate_failures': state.get('gate_failures', []),
                                   'reading': {'policy': cfg['query']['reading_policy'], 'script_guided_active': state.get('script_guided', False),
                                               'plan': state.get('read_plan'), 'planning_error': state.get('read_plan_error'),
                                               'plan_history': state.get('read_plan_history', []),
                                               'visual_reads': sum(c['operation'] in ('observe', 'extract') for c in ledger.calls),
                                               'assess_calls': sum(c['operation'] == 'assess' for c in ledger.calls)},
                                   'answer_conflict': state.get('answer_conflict'),
                                   'last_tool_error': state.get('last_tool_error'),
                                   'evidence_gaps': state.get('evidence_gaps', []),
                                   'question_ambiguities': state.get('ambiguities', []),
                                   'resolved_ambiguities': state.get('resolved_ambiguities', []),
                                   'internal_tool_errors': sum(bool(c.get('error')) for c in ledger.calls),
                                   'soft_deadline_stops': sum('SoftDeadlineExceeded' in (c.get('error') or '') for c in ledger.calls),
                                   'hard_timeouts': sum((c.get('error') or '').startswith('TimeoutError:') for c in ledger.calls),
                                   'model_preparations': sum(c['operation'] == 'prepare_vlm' for c in ledger.calls)}
        envelope['wall_seconds'] = (time.monotonic()-started+metadata.get('base_restore_seconds', 0.)+
                                    metadata.get('model_snapshot_validation_seconds', 0.))
        write_jsonl(output / 'coverage_delta.jsonl', [c for c in memory.tables['coverage'].values() if c['id'] not in base.tables['coverage']])
        write_json(output / 'result.json', envelope)
    return envelope
