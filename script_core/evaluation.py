"""Independent offline scoring. Annotations never enter the query controller."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import random

from .config import digest
from .coverage import complement, intersect
from .state import write_json


def read_jsonl(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def evidence_match(requirement, evidence):
    actual_frames = {f['frame_id'] for e in evidence for f in e['frame_inputs']}
    if requirement.get('frame_ids'):
        return set(requirement['frame_ids']) <= actual_frames
    interval = requirement.get('interval')
    if interval:
        # A package envelope is not observation: require actual frame PTS within annotation.
        pts = {f['pts'] for e in evidence for f in e['frame_inputs'] if interval[0] <= f['pts'] < interval[1]}
        return len(pts) >= requirement.get('min_frames', 1)
    raise ValueError('Evidence annotations need frame_ids or interval and min_frames')


def paired_cluster_bootstrap(rows, first='B2', second='B1S', repeats=2000, seed=0):
    by_query = defaultdict(dict)
    for row in rows:
        if row['correct'] is not None:
            by_query[(row['cluster_id'], row['question_id'])].setdefault(row['method'], []).append(int(row['correct']))
    clusters = defaultdict(list)
    for (cluster, _), methods in by_query.items():
        if first in methods and second in methods:
            clusters[cluster].append(sum(methods[first])/len(methods[first])-sum(methods[second])/len(methods[second]))
    if not clusters:
        return {'comparison': f'{first}-{second}', 'difference': None, 'ci95': None, 'paired_questions': 0}
    rng = random.Random(seed)
    keys = sorted(clusters)
    all_diff = [x for xs in clusters.values() for x in xs]
    samples = []
    for _ in range(repeats):
        diffs = [x for k in rng.choices(keys, k=len(keys)) for x in clusters[k]]
        samples.append(sum(diffs)/len(diffs))
    samples.sort()
    return {'comparison': f'{first}-{second}', 'difference': sum(all_diff)/len(all_diff),
            'ci95': [samples[int(.025*repeats)], samples[min(repeats-1, int(.975*repeats))]],
            'paired_questions': len(all_diff), 'independent_clusters': len(keys),
            'per_cluster': {k: sum(v)/len(v) for k, v in clusters.items()}, 'seed': seed}


def evaluate(runs, annotations_path, output):
    source = json.loads(Path(annotations_path).read_text())
    annotations = source['questions'] if isinstance(source, dict) else source
    lookup = {r['question_id']: r for r in annotations}
    rows = []
    entries = {p: p for p in Path(runs).rglob('result.json')}
    for failure in Path(runs).rglob('query_failures/*.json'):
        entries[failure.parent.parent/failure.stem/'result.json'] = failure
    for logical_path, path in sorted(entries.items()):
        directory = logical_path.parent
        query_file = directory / 'query.json'
        result = json.loads(path.read_text())
        if not query_file.exists() and not result.get('query'):
            continue
        query = json.loads(query_file.read_text()) if query_file.exists() else result['query']
        if query['id'] not in lookup:
            continue
        annotation = lookup[query['id']]
        cfg = result.get('run_config') or (json.loads((directory / 'run_config.json').read_text()) if (directory / 'run_config.json').exists() else {})
        answer = result['answer']
        correct = None
        if answer and 'selected_option' in annotation:
            correct = answer['selected_option'] == str(annotation['selected_option'])
        if answer and 'acceptable_answers' in annotation:
            correct = answer['text'].strip() in annotation['acceptable_answers']
        if result['error'] and ('selected_option' in annotation or 'acceptable_answers' in annotation):
            correct = False
        evidence = read_jsonl(directory / 'evidence.jsonl')
        observations = read_jsonl(directory / 'observations.jsonl')
        online_ids = {e for o in observations for e in o['evidence_ids']}
        actually_read = [e for e in evidence if e['id'] in online_ids]
        retained = [e for e in evidence if answer and e['id'] in answer['evidence_ids']]
        sets = annotation.get('sufficient_evidence_sets', [])
        fractions = [sum(evidence_match(r, actually_read) for r in group)/len(group) for group in sets if group]
        retained_fractions = [sum(evidence_match(r, retained) for r in group)/len(group) for group in sets if group]
        first_cost = None
        local_results = {r['task_id']: r for r in read_jsonl(directory / 'local_results.jsonl')}
        accumulated = set()
        for step in read_jsonl(directory / 'steps.jsonl'):
            local = local_results.get(step['task_id'], {})
            accumulated.update(local.get('evidence_ids', []))
            snapshot = [e for e in evidence if e['id'] in accumulated]
            if sets and any(group and all(evidence_match(r, snapshot) for r in group) for group in sets):
                first_cost = result['cost_summary']['initialization']+step['cumulative_online_cost']
                break
        stop = result['stop_decision']
        scope = query.get('scope')
        span = scope[1]-scope[0] if scope else None
        fine_intervals = [e['interval'] for e in actually_read]
        missing = complement(scope, fine_intervals) if scope else []
        refined = (span-sum(b-a for a, b in missing))/span if span else None
        steps = read_jsonl(directory / 'steps.jsonl')
        judgments = annotation.get('run_judgments', {}).get(str(path.relative_to(runs)), {})
        mode = result.get('experiment_mode', 'live')
        default_cap = result['cost_summary']['initialization']+cfg.get('budget', {}).get('online_cap', 0.)
        budget = ({'rule': 'initialization_plus_online', 'online_cap': cfg.get('budget', {}).get('online_cap')}
                  if abs((result['cost_summary'].get('total_cap') or 0.)-default_cap) < 1e-6
                  else {'rule': 'common_total_cap', 'total_cap': result['cost_summary'].get('total_cap')})
        cohort = {'strategy': cfg.get('construction', {}).get('strategy', 'unknown'),
                  'cache_protocol': cfg.get('cache', {}).get('scope', 'unknown'),
                  'experiment_mode': mode, 'models': digest(cfg.get('models', {})),
                  'ablations': cfg.get('ablations', {}), 'segmentation': cfg.get('segmentation', {}), 'budget': budget}
        task_judgments = judgments.get('local_tasks', {})
        evaluated_tasks = [v for v in task_judgments.values() if v.get('success') is not None]
        patch_judgments = judgments.get('patches', {})
        evaluated_patches = [v for v in patch_judgments.values() if v.get('correct') is not None]
        calls = read_jsonl(directory / 'calls.jsonl')
        replay_source_cost = sum((c.get('replay') or {}).get('source_processing_seconds', 0.) for c in calls)
        rows.append({'question_id': query['id'], 'video_id': query['video_id'],
                     'cluster_id': annotation.get('cluster_id', query['video_id']), 'method': cfg.get('method', 'unknown'),
                     'strategy': cfg.get('construction', {}).get('strategy', 'unknown'), 'correct': correct,
                     'cohort': cohort, 'cohort_id': digest(cohort)[:16], 'experiment_mode': mode,
                     'question_type': annotation.get('question_type', 'unclassified'),
                     'local_task_successes': sum(v['success'] for v in evaluated_tasks),
                     'evaluated_local_tasks': len(evaluated_tasks),
                     'unevaluated_local_tasks': max(0, len(local_results)-len(evaluated_tasks)),
                     'correct_patches': sum(v['correct'] for v in evaluated_patches),
                     'evaluated_patches': len(evaluated_patches),
                     'harmful_patches': sum(v.get('introduced_error') is True for v in evaluated_patches),
                     'replayed_original_perception_cost': replay_source_cost,
                     'cache_statistics': result.get('cache_statistics'), 'storage': result.get('storage'),
                     'stop_reason': stop['reason'] if stop else None, 'error': result['error'],
                     'independent_sufficiency': judgments.get('evidence_sufficient'),
                     'evidence_read_fraction': max(fractions) if fractions else None,
                     'evidence_retained_fraction': max(retained_fractions) if retained_fractions else None,
                     'sufficient_set_reached': max(fractions) == 1 if fractions else None,
                     'first_sufficient_evidence_cost': first_cost,
                     'censored_at_cost': result['cost_summary']['total'] if sets and first_cost is None else None,
                     'refinement_coverage': refined,
                     'unproductive_visits': sum(not s['progress_refs'] for s in steps), 'visits': len(steps),
                     'cost': result['cost_summary'], 'wall_seconds': result.get('wall_seconds'),
                     'budget_violation': result['cost_summary'].get('budget_violation', False),
                     'path': str(path.relative_to(runs))})
    groups = defaultdict(list)
    for r in rows:
        groups[(r['method'], r['strategy'], r['cohort_id'])].append(r)
    aggregate = {}
    for (method, strategy, cohort_id), rs in groups.items():
        scored = [r for r in rs if r['correct'] is not None]
        judged_stops = [r for r in rs if r['stop_reason'] == 'protocol_sufficient' and r['independent_sufficiency'] is not None]
        aggregate[f'{method}/{strategy}/{rs[0]['experiment_mode']}/{cohort_id}'] = {
            'cohort': rs[0]['cohort'],
            'questions': len(rs), 'scored_answers': len(scored),
            'accuracy': sum(r['correct'] for r in scored)/len(scored) if scored else None,
            'stop_counts': dict(Counter(r['stop_reason'] for r in rs)),
            'successful_stop_fraction': sum(r['stop_reason'] == 'protocol_sufficient' for r in rs)/len(rs),
            'wrong_sufficient_stop_rate': sum(not r['independent_sufficiency'] for r in judged_stops)/len(judged_stops) if judged_stops else None,
            'independently_judged_sufficient_stops': len(judged_stops),
            'mean_total_cost': sum(r['cost']['total'] for r in rs)/len(rs),
            'unproductive_visit_rate': sum(r['unproductive_visits'] for r in rs)/sum(r['visits'] for r in rs) if sum(r['visits'] for r in rs) else None,
            'budget_violations': sum(r['budget_violation'] for r in rs),
            'local_task_success_rate': (sum(r['local_task_successes'] for r in rs)/sum(r['evaluated_local_tasks'] for r in rs)
                                        if sum(r['evaluated_local_tasks'] for r in rs) else None),
            'unevaluated_local_tasks': sum(r['unevaluated_local_tasks'] for r in rs),
            'patch_precision': (sum(r['correct_patches'] for r in rs)/sum(r['evaluated_patches'] for r in rs)
                                if sum(r['evaluated_patches'] for r in rs) else None),
            'harmful_patch_rate': (sum(r['harmful_patches'] for r in rs)/sum(r['evaluated_patches'] for r in rs)
                                   if sum(r['evaluated_patches'] for r in rs) else None),
            'accuracy_by_question_type': {kind: sum(r['correct'] for r in scored if r['question_type'] == kind)/
                                                sum(r['question_type'] == kind for r in scored)
                                          for kind in sorted({r['question_type'] for r in scored})},
        }
    cohorts = defaultdict(list)
    for row in rows:
        cohorts[row['cohort_id']].append(row)
    metrics = {'annotation_hash': digest(source), 'rows': rows, 'groups': aggregate,
               'paired_bootstrap': {key: paired_cluster_bootstrap(rs) for key, rs in cohorts.items()},
               'notes': ['Evidence matching measures actual inputs, not independent interpretation correctness.',
                         'Null denotes unavailable annotation or an empty denominator.',
                         'Live and replay runs, budgets, models, cache protocols and ablations are separate cohorts.',
                         'Replay IO costs are reported separately from original recorded perception costs.',
                         'Labeled query failures count as incorrect; a recorded failed attempt is not an unstarted question.',
                         'Open answers require independent acceptable_answers; no model self-scoring.']}
    write_json(Path(output) / 'metrics.json', metrics)
    return metrics
