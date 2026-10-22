"""Fixed 300-question evaluation of caption-preserving, scene-lazy SCRIPT."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import shutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'script'))
from run_compatibility import prepare as prepare_compatibility, verify, baseline_comparison
from run_summary_graph import read, write, rows, sha
from script_core.config import digest, load_config

ARMS = {'c0': 'Fresh original VideoTree through SCRIPT',
        'scene_lazy': 'Caption-preserving phases, lazy local relations, pointer expansion and lazy correspondence'}


def profile_config(cfg, arm):
    cfg = deepcopy(cfg)
    cfg['compatibility'].update(enabled=arm == 'c0', graph_retrieval=False, retrieval_feedback=False,
        visual_verification=False, answer_protection=False, final_qa='videotree')
    cfg['scene_memory'].update(enabled=arm != 'c0', key_information=arm in ('scene_keys', 'scene_hierarchical'),
        hierarchical_directory=arm == 'scene_hierarchical')
    cfg['query']['answer_order'] = 'original'
    return cfg


def prepare(output, reference, videotree):
    prepare_compatibility(output, reference, videotree, reference)
    cfg = read(output/'config.json')
    cfg['compatibility']['cache_directory'] = ''  # Both the control and new method execute fresh.
    cfg['compatibility']['enabled'] = False
    cfg['scene_memory']['enabled'] = False
    write(output/'config.json', cfg)
    spec = read(output/'experiment.json')
    spec.update(mode='caption_preserving_scene_lazy', profiles=ARMS, arms=ARMS,
        require_scene_repairs=True,
        stages=['smoke', 'run0', 'run1'], parents={'scene_lazy': 'c0'},
        notes=['Same frozen 300 development questions, same models, captions, M0, original QA and option order.',
            'Directory construction sees captions only. No local relation graph or identity links are prebuilt.',
            'All original caption text/times are retained; semantic phases capped at 60 seconds.',
            'Initial directory cost is separate and added to lazy method accounting, even when reused.',
            'Only the highest-relevance scene is opened first; up to four scenes, expanded through explicit pointers after a concrete gap.',
            'Pointers store forward chronological descriptions and semantic candidates, with a reverse traversal index.',
            'Pointers describe phase endpoints; intervening physical transitions and causes remain unknown.',
            'Local graphs are built only for opened scenes. Cross-scene text correspondences require traversal and citations from both scenes.',
            'Text-only first experiment: no raw video decoding or pixel verification. Text sufficiency is not protocol sufficiency.',
            'Historical reference failures remain in denominator; fresh control failures counted wrong.',
            'Smoke checks execution and original control reproduction, not accuracy-based method selection.',
            'Existing development cohort; no held-out performance claim.'])
    spec['notes'] += [
        'Directory outputs reserve a mandatory final phase ending at the last supplied caption; coarse phase count is bounded.',
        'Question-only temporal scope classification precedes scene review; global questions require early/middle/late text coverage.',
        'Cross-stage questions require distinct stages; exhaustive questions require all scoped scenes before text sufficiency.',
        'Coverage is a necessary heuristic, not proof of answer correctness; four-visit and 600-second limits remain in force.',
        'Repair smoke rejects directory fallback, missing temporal classification and stopping despite unmet temporal coverage.']
    write(output/'experiment.json', spec)
    hashes = read(output/'source_hashes.json')
    for name in ('script/run_scene_lazy.py', 'script/scene_lazy.pbs', 'configs/script_scene_lazy.yaml'):
        target = output/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, target)
        hashes[name] = sha(target)
    write(output/'source_hashes.json', hashes)
    write(output/'input_hashes.json', {p: sha(output/p) for p in read(output/'input_hashes.json')})
    return {'prepared': 300, 'arms': ARMS, 'output': str(output)}


def get_directory(target, packet, backend, cfg):
    from script_core.budget import BudgetLedger, Meter
    from script_core.scene_memory import build_directory, validate_directory
    tag = 'hierarchical' if cfg['scene_memory']['hierarchical_directory'] else 'keys' if cfg['scene_memory']['key_information'] else 'basic'
    path = target/'directories'/tag/f'{packet["question"]["video_id"]}.json'
    if path.exists():
        return validate_directory(read(path), packet['manifest'])
    call_path = target/'directory_calls'/tag/packet['question']['video_id']
    ledger = BudgetLedger(600. if cfg['scene_memory']['key_information'] else 300., answer_reserve=0., calls_path=call_path/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, call_path/'raw_calls')
    try:
        directory = build_directory(packet['manifest'], cfg, meter)
        directory['cost_summary'] = ledger.summary()
        validate_directory(directory, packet['manifest'])
        write(path, directory)
        return directory
    finally:
        meter.close()


def run_query(target, arm, packet, cfg, backend, directory=None):
    from script_core.build import load_base
    from script_core.controller import answer_query
    output = target/'arms'/arm/'runs'/digest(packet['question']['question_id'])[:20]
    if (output/'result.json').exists():
        return read(output/'result.json')
    start, cpu = time.monotonic(), time.process_time()
    memory, metadata = load_base(packet['base'])
    metadata.update(base_restore_seconds=time.monotonic()-start, base_restore_cpu_seconds=time.process_time()-cpu)
    cfg = profile_config(cfg, arm)
    directory_seconds = (directory or {}).get('cost_summary', {}).get('online', 0.)
    if directory:
        # Fresh directory construction is genuinely additional preprocessing work.
        metadata['initialization_cost'] += directory_seconds
    write(output/'run_config.json', cfg)
    result = answer_query(memory, packet['question'], metadata, cfg, backend, output,
        scene_directory=directory, resume=(output/'query.json').exists())
    result['scene_directory_cost_seconds'] = directory_seconds
    result['scene_directory_budget_violation'] = bool((directory or {}).get('cost_summary', {}).get('budget_violation'))
    write(output/'result.json', result)
    return result


def run_group(output, group, smoke=False):
    from script_core.backend import preflight
    from script_core.budget import ModelWorker
    target = output/'smoke' if smoke else output
    ids = read(output/('smoke/ids.json' if smoke else f'shards/{group}/ids.json'))
    cfg, artifacts = preflight(load_config(output/'config.json'))
    write(target/'setup'/f'{group}-artifacts.json', artifacts)
    backend = ModelWorker(cfg)
    start = time.monotonic()
    try:
        backend.call('prepare_vlm', {}, timeout=300.)
        write(target/'setup'/f'{group}.json', {'shared_warmup_seconds': time.monotonic()-start})
        for i, uid in enumerate(ids):
            packet = read(output/'inputs'/f'{uid}.json')
            results = {}
            for arm in read(output/'experiment.json')['arms']:
                try:
                    directory = None if arm == 'c0' else get_directory(target, packet, backend, profile_config(cfg, arm))
                    results[arm] = run_query(target, arm, packet, cfg, backend, directory)
                except Exception as exc:
                    result = {'answer': None, 'error': {'code': 'scene_directory_failed', 'message': str(exc)}}
                    path = target/'arms'/arm/'runs'/digest(uid)[:20]/'result.json'
                    write(path, result)
                    results[arm] = result
            write(target/'progress'/f'{group}.json', {'completed': i+1, 'planned': len(ids), 'last_question': uid})
            print('SCENE_LAZY', group, i+1, uid, {name: (r.get('answer') or {}).get('selected_option')
                for name, r in results.items()}, flush=True)
    finally:
        backend.close()


def gate(output):
    target, evaluation = output/'smoke', read(output/'evaluation_only.json')
    failures, checks = [], {}
    for uid in read(output/'smoke/ids.json'):
        for arm in read(output/'experiment.json')['arms']:
            path = target/'arms'/arm/'runs'/digest(uid)[:20]/'result.json'
            if not path.exists():
                failures.append({'id': uid, 'arm': arm, 'reason': 'missing result'})
                continue
            r = read(path)
            if r.get('error') or r.get('cost_summary', {}).get('budget_violation') or r.get('scene_directory_budget_violation'):
                failures.append({'id': uid, 'arm': arm, 'reason': r.get('error') or 'budget violation'})
            if arm == 'c0':
                comparison = baseline_comparison(r, evaluation[uid]['videotree'])
                checks[uid] = comparison
                if evaluation[uid]['videotree'] and (comparison['error'] or not all(v for k,v in comparison.items() if k.endswith('_equal'))):
                    failures.append({'id': uid, 'reason': comparison})
            else:
                d = r.get('scene_memory', {})
                if d.get('extension_errors') or not d.get('opened_scenes') or d.get('pixel_reads') != 0:
                    failures.append({'id': uid, 'arm': arm, 'reason': d.get('extension_errors') or 'missing scene execution'})
                if len(d.get('opened_scenes', [])) != len(d.get('local_graphs', {})):
                    failures.append({'id': uid, 'reason': 'unvisited scene graph built'})
                if d.get('key_build_diagnostics', {}).get('failed_batches'):
                    failures.append({'id': uid, 'arm': arm, 'reason': 'key extraction batch failed'})
                if read(output/'experiment.json').get('require_scene_repairs'):
                    if d.get('directory_fallback'):
                        failures.append({'id': uid, 'arm': arm, 'reason': 'semantic directory fell back'})
                    if d.get('scope_requirement', {}).get('scope') not in ('local', 'global', 'multi_scene', 'exhaustive'):
                        failures.append({'id': uid, 'arm': arm, 'reason': 'missing temporal scope classification'})
                    reviews = d.get('reviews', [])
                    if not reviews or any(r['sufficient'] and not r.get('coverage_check', {}).get('met') for r in reviews):
                        failures.append({'id': uid, 'arm': arm, 'reason': 'invalid temporal sufficiency'})
                    if (reviews and not reviews[0].get('coverage_check', {}).get('met')
                            and d.get('total_scenes', 0) > 1 and len(d.get('opened_scenes', [])) < 2):
                        failures.append({'id': uid, 'arm': arm, 'reason': 'missing cross-scene exploration'})
    report = {'passed': not failures, 'failures': failures, 'control_checks': checks}
    if 'scene_keys' in read(output/'experiment.json')['arms']:
        for uid in read(output/'smoke/ids.json'):
            packet = read(output/'inputs'/f'{uid}.json')
            base_path = target/'directories/basic'/f'{packet["question"]["video_id"]}.json'
            key_path = target/'directories/keys'/f'{packet["question"]["video_id"]}.json'
            if base_path.exists() and key_path.exists():
                basic, keyed = read(base_path), read(key_path)
                projected = [{k:v for k,v in s.items() if k != 'key_ids'} for s in keyed['scenes']]
                if basic['scenes'] != projected:
                    failures.append({'id': uid, 'reason': 'Key-only arm changed the underlying scene partition'})
        report['passed'] = not failures
    write(target/'status.json', report)
    return report['passed']


def score(output):
    with (output/'score.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ids, labels = read(output/'cohort.json'), read(output/'evaluation_only.json')
        records, groups = {}, {}
        spec = read(output/'experiment.json')
        arms = spec.get('arms', ARMS)
        for arm in arms:
            records[arm] = {}
            for uid in ids:
                path = output/'arms'/arm/'runs'/digest(uid)[:20]/'result.json'
                r = read(path) if path.exists() else None
                pred = ((r or {}).get('answer') or {}).get('selected_option')
                d = (r or {}).get('scene_memory', {})
                costs = (r or {}).get('cost_summary', {})
                records[arm][uid] = {'completed': r is not None, 'prediction': pred,
                    'correct': bool(r and not r.get('error') and pred == labels[uid]['truth']),
                    'error': (r or {}).get('error'), 'extension_errors': d.get('extension_errors', []),
                    'online_seconds': costs.get('online'), 'total_seconds': costs.get('total'),
                    'directory_seconds': (r or {}).get('scene_directory_cost_seconds', 0.),
                    'opened_scenes': len(d.get('opened_scenes', [])), 'total_scenes': d.get('total_scenes'),
                    'read_captions': d.get('opened_caption_count'), 'total_captions': d.get('total_captions'),
                    'traversals': len(d.get('traversals', [])), 'correspondence_pairs': sum(len(x['matches']) for x in d.get('correspondences', [])),
                    'directory_fallback': d.get('directory_fallback'), 'budget_violation': bool(costs.get('budget_violation') or (r or {}).get('scene_directory_budget_violation')),
                    'evidence_scope': d.get('scope_requirement', {}).get('scope'),
                    'text_sufficient': d.get('text_sufficient'),
                    'coverage_met': (d.get('reviews') or [{}])[-1].get('coverage_check', {}).get('met'),
                    'graph_batch_recoveries': sum('graph_batches' in review for review in d.get('reviews', [])),
                    'opened_keys': d.get('opened_key_count', 0),
                    'key_build_failures': len(d.get('key_build_diagnostics', {}).get('failed_batches', [])),
                    'hierarchy_nodes_per_retrieval': max((c['candidate_nodes'] for c in d.get('hierarchy_retrieval', [])), default=0),
                    'historical_control_comparison': baseline_comparison(r, labels[uid]['videotree']) if r and arm == 'c0' else None}
            done = [v for v in records[arm].values() if v['completed']]
            def mean(field):
                values = [v[field] for v in done if v[field] is not None]
                return statistics.mean(values) if values else None
            groups[arm] = {'completed': len(done), 'planned': len(ids), 'correct': sum(v['correct'] for v in done),
                'accuracy': sum(v['correct'] for v in done)/len(ids) if len(done) == len(ids) else None,
                'errors': sum(bool(v['error']) for v in done), 'extension_errors': sum(bool(v['extension_errors']) for v in done),
                'budget_violations': sum(v['budget_violation'] for v in done),
                'directory_fallbacks': sum(bool(v['directory_fallback']) for v in done),
                'key_build_failures': sum(v['key_build_failures'] for v in done),
                'scope_counts': {kind: sum(v['evidence_scope'] == kind for v in done)
                    for kind in ('local', 'global', 'multi_scene', 'exhaustive', 'unresolved')},
                'questions_with_traversal': sum(v['traversals'] > 0 for v in done),
                'questions_missing_temporal_coverage': sum(v['coverage_met'] is False for v in done),
                'graph_batch_recoveries': sum(v['graph_batch_recoveries'] for v in done),
                **{'mean_'+k: mean(k) for k in ('online_seconds', 'total_seconds', 'directory_seconds', 'opened_scenes', 'read_captions', 'traversals', 'correspondence_pairs', 'opened_keys', 'hierarchy_nodes_per_retrieval')}}
        paired = [u for u in ids if all(records[a][u]['completed'] for a in arms)]
        fixed = [u for u in paired if not records['c0'][u]['correct'] and records['scene_lazy'][u]['correct']]
        lost = [u for u in paired if records['c0'][u]['correct'] and not records['scene_lazy'][u]['correct']]
        comparisons = {}
        for after,before in spec.get('parents', {'scene_lazy': 'c0'}).items():
            paired_ids = [u for u in ids if records[before][u]['completed'] and records[after][u]['completed']]
            fixes = [u for u in paired_ids if not records[before][u]['correct'] and records[after][u]['correct']]
            losses = [u for u in paired_ids if records[before][u]['correct'] and not records[after][u]['correct']]
            comparisons[before+'_to_'+after] = {'completed_pairs': len(paired_ids), 'fixed_ids': fixes,
                'lost_ids': losses, 'net_correct': len(fixes)-len(losses),
                'delta_pp': 100*(len(fixes)-len(losses))/len(ids) if len(paired_ids) == len(ids) else None}
        historical = read(output/'evaluation_only.json')
        report = {'complete': all(g['completed'] == len(ids) for g in groups.values()), 'groups': groups,
            'paired': {'completed': len(paired), 'fixed_ids': fixed, 'lost_ids': lost, 'net_correct': len(fixed)-len(lost)},
            'comparisons': comparisons,
            'historical_videotree_accuracy': sum(bool(v['videotree'] and v['videotree']['prediction'] == v['truth']) for v in historical.values())/len(ids),
            'rows': records, 'notes': read(output/'experiment.json')['notes']}
        write(output/'comparison.json', report)
        return {k:v for k,v in report.items() if k not in ('rows', 'notes')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'group', 'score'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--reference')
    parser.add_argument('--videotree')
    parser.add_argument('--stage', choices=['smoke', 'run0', 'run1'])
    parser.add_argument('--group')
    args = parser.parse_args()
    output = Path(args.out).resolve()
    if args.command == 'prepare':
        print(json.dumps(prepare(output, args.reference, args.videotree)))
        return 0
    verify(output)
    if args.command == 'score':
        print(json.dumps(score(output)))
        return 0
    if args.command == 'group':
        run_group(output, args.group)
        return 0
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('Each experiment job requires exactly one visible GPU')
    if args.stage == 'smoke':
        run_group(output, 'smoke', smoke=True)
        return 0 if gate(output) else 2
    if not (output/'smoke/status.json').exists() or not read(output/'smoke/status.json')['passed']:
        raise RuntimeError('Scene-lazy smoke must pass before the 300-question run')
    groups = ['0', '2'] if args.stage == 'run0' else ['1', '3']
    def run(group):
        log = output/'shards'/group/'run.log'
        with log.open('a') as stream:
            return subprocess.run([sys.executable, __file__, 'group', '--out', str(output), '--group', group],
                cwd=output/'source', stdout=stream, stderr=subprocess.STDOUT).returncode
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(run, groups))
    print(json.dumps(score(output)), flush=True)
    return int(any(codes))


if __name__ == '__main__':
    raise SystemExit(main())
