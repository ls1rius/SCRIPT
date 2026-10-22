"""Run VideoTree-compatible component ablations through SCRIPT answer_query."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'script'))
from run_summary_graph import read, rows, write, sha, prepare as prepare_packets, build_graph
from script_core.config import load_config, digest

PROFILES = {
    'c0': {},
    'cg': {'graph_retrieval': True},
    'cf': {'retrieval_feedback': True},
    'cv': {'visual_verification': True},
    'cgf': {'graph_retrieval': True, 'retrieval_feedback': True},
    'cgfv': {'graph_retrieval': True, 'retrieval_feedback': True, 'visual_verification': True},
    'cp': {'graph_retrieval': True, 'retrieval_feedback': True, 'visual_verification': True, 'answer_protection': True},
    'cqa': {'graph_retrieval': True, 'retrieval_feedback': True, 'visual_verification': True, 'final_qa': 'script'},
    'corder': {'graph_retrieval': True, 'retrieval_feedback': True, 'visual_verification': True, 'final_qa': 'script'},
}
PARENTS = {'cg': 'c0', 'cf': 'c0', 'cv': 'c0', 'cgf': 'cg', 'cgfv': 'cgf',
           'cp': 'cgfv', 'cqa': 'cgfv', 'corder': 'cqa'}


def prepare(output, reference, videotree, graph_source):
    from run_videotree_scaling import BASELINE_FILES
    output, reference, videotree = Path(output).resolve(), Path(reference).resolve(), Path(videotree).resolve()
    prepare_packets(output, reference)
    cfg = read(output/'config.json')
    cfg['compatibility'].update(enabled=True,
        feature_directory=str((ROOT/'data/EgoSchema/egoschema_features').resolve()),
        cache_directory=str(output/'common_tool_cache'))
    cfg['query']['answer_order'] = 'original'
    write(output/'config.json', cfg)
    load_config(output/'config.json')
    evaluation = read(output/'evaluation_only.json')
    reuse = read(output/'reuse_hashes.json')
    successful = 0
    for uid in read(output/'cohort.json'):
        directory = videotree/'videotree_runs'/uid
        path = directory/'standard_qa.json'
        old = read(path) if path.exists() else {}
        old = old.get('data', old).get(uid)
        if old:
            tree_path = directory/'depth_expansion_res.json'
            tree = next(x['sorted_values'] for x in read(tree_path) if x['name'] == uid)
            evaluation[uid]['videotree'] = {'prediction': str(old['pred']), 'selected_seconds': tree,
                'head': old['info']['message'][0]['content'], 'prompts': old['prompt'],
                'prompt_tokens': old['info']['prompt_tokens']}
            successful += 1
            for p in (path, tree_path): reuse[str(p)] = sha(p)
        else:
            evaluation[uid]['videotree'] = None
        packet = read(output/'inputs'/f'{uid}.json')
        feature = Path(cfg['compatibility']['feature_directory'])/f'{packet["question"]["video_id"]}.pt'
        reuse[str(feature)] = sha(feature)
    if successful != 299:
        raise ValueError('Require original 299 successful baseline cases, preserving the historical failed case')
    write(output/'evaluation_only.json', evaluation)
    write(output/'reuse_hashes.json', reuse)
    spec = read(output/'experiment.json')
    spec.update(mode='script_native_videotree_compatibility', profiles=PROFILES, parents=PARENTS,
        arms=PROFILES,
        graph_source=str(Path(graph_source).resolve()), original_source=str(videotree),
        source_successful_baselines=299, entrypoint='script_core.controller.answer_query',
        stages=['smoke', 'baseline0', 'baseline1', 'gate', 'variants0', 'variants1'],
        notes=['All profiles run the same SCRIPT query entry, memory, meter, store and local executor.',
            'Original unmodified VideoTree width/depth functions and Qwen.forward are worker tools.',
            'c0 rebuilds original trees and QA fresh. All 299 historical successes must reproduce before variants release.',
            'The one historical failure remains in the denominator; fresh success/failure is reported separately.',
            'Exact-input original tools share a disk cache across profiles; fresh and cache dependency costs are separate.',
            'Existing question-independent graphs are frozen after previous jobs finish; missing graphs can be built from captions only.',
            'No correct labels enter inference, retrieval feedback or observation inputs.',
            'cqa changes the complete QA interface; corder separately changes option presentation.',
            'Existing development cohort; no held-out performance claim.'])
    write(output/'experiment.json', spec)
    hashes = read(output/'source_hashes.json')
    original_hashes = read(videotree/'source_hashes.json')
    for name in BASELINE_FILES:
        if sha(ROOT/name) != original_hashes[name]:
            raise ValueError('Original VideoTree file changed: '+name)
        target = output/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(videotree/'source'/name, target)
        hashes[name] = sha(target)
    for name in ('script/run_compatibility.py', 'script/compatibility.pbs', 'configs/script_videotree.yaml'):
        target = output/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, target)
        hashes[name] = sha(target)
    write(output/'source_hashes.json', hashes)
    frozen = list(read(output/'input_hashes.json'))
    write(output/'input_hashes.json', {p: sha(output/p) for p in frozen})
    return {'prepared': 300, 'profiles': list(PROFILES), 'baseline_gate_cases': successful}


def verify(output):
    for filename, root in [('source_hashes.json', output/'source'), ('input_hashes.json', output)]:
        for p, expected in read(output/filename).items():
            if sha(root/p) != expected:
                raise ValueError('Frozen source/input changed: '+p)
    for p, expected in read(output/'reuse_hashes.json').items():
        if sha(p) != expected:
            raise ValueError('Source evidence/features changed: '+p)
    if (output/'graph_source_hashes.json').exists():
        for name, expected in read(output/'graph_source_hashes.json').items():
            if sha(output/'reused_graphs'/name) != expected:
                raise ValueError('Frozen graph changed: '+name)
    load_config(output/'config.json')


def freeze_graphs(output):
    from script_core.summary_graph import validate_graph
    if (output/'graph_source_hashes.json').exists():
        return
    source = Path(read(output/'experiment.json')['graph_source'])
    hashes, absent = {}, []
    for uid in read(output/'cohort.json'):
        packet = read(output/'inputs'/f'{uid}.json')
        name = packet['question']['video_id']+'.json'
        path = source/'graphs'/name
        if not path.exists():
            absent.append(name)
            continue
        graph = validate_graph(read(path), packet['manifest'])
        write(output/'reused_graphs'/name, graph)
        hashes[name] = sha(output/'reused_graphs'/name)
    write(output/'graph_source_hashes.json', hashes)
    write(output/'graph_reuse.json', {'reused': len(hashes), 'missing': absent,
          'source': str(source), 'scope': 'Question-independent graphs; historical extraction cost retained separately'})


def profile_config(cfg, name):
    result = deepcopy(cfg)
    result['compatibility'].update(enabled=True, graph_retrieval=False, retrieval_feedback=False,
        visual_verification=False, answer_protection=False, final_qa='videotree')
    result['compatibility'].update(PROFILES[name])
    result['query']['answer_order'] = 'content_hash' if name == 'corder' else 'original'
    return result


def run_profile(target, name, packet, cfg, backend, graph=None):
    from script_core.build import load_base
    from script_core.controller import answer_query
    uid = packet['question']['question_id']
    directory = target/'arms'/name/'runs'/digest(uid)[:20]
    if (directory/'result.json').exists():
        return read(directory/'result.json')
    started, cpu = time.monotonic(), time.process_time()
    base, metadata = load_base(packet['base'])
    metadata.update(base_restore_seconds=time.monotonic()-started, base_restore_cpu_seconds=time.process_time()-cpu)
    cfg = profile_config(cfg, name)
    write(directory/'run_config.json', cfg)
    result = answer_query(base, packet['question'], metadata, cfg, backend, directory,
                          summary_graph=graph if cfg['compatibility']['graph_retrieval'] else None,
                          resume=(directory/'query.json').exists())
    # Both cache-hit wall time and the work needed without that cache are explicit.
    reused = {c['tool_cache']['key']: c['tool_cache']['source_seconds']
              for c in rows(directory/'calls.jsonl') if c.get('tool_cache', {}).get('hit')}
    result['shared_tool_cost'] = {'reused_unique_inputs': len(reused),
        'source_processing_seconds': result.get('cost_summary', {}).get('shared_dependency_seconds', 0.),
        'scope': 'Dependency-equivalent original tool cost, not additional measured GPU time'}
    write(directory/'result.json', result)
    return result


def baseline_comparison(result, expected):
    if expected is None:
        return {'historical_failure': True, 'error': result.get('error')}
    actual = result.get('compatibility') or {}
    return {'tree_equal': actual.get('baseline_selected_seconds') == expected['selected_seconds'],
            'head_equal': actual.get('baseline_qa', {}).get('head') == expected['head'],
            'prompt_equal': actual.get('baseline_qa', {}).get('prompts') == expected['prompts'],
            'prediction_equal': actual.get('baseline_prediction') == expected['prediction'],
            'input_tokens_equal': actual.get('baseline_prompt_tokens') == expected['prompt_tokens'],
            'error': result.get('error')}


def gate(output, smoke=False):
    target = output/'smoke' if smoke else output
    ids = read(output/('smoke/ids.json' if smoke else 'cohort.json'))
    evaluation = read(output/'evaluation_only.json')
    checks, failures = {}, []
    for uid in ids:
        path = target/'arms/c0/runs'/digest(uid)[:20]/'result.json'
        if not path.exists():
            failures.append({'id': uid, 'reason': 'Missing baseline result'})
            continue
        baseline = read(path)
        checks[uid] = baseline_comparison(baseline, evaluation[uid]['videotree'])
        if baseline.get('cost_summary', {}).get('budget_violation'):
            failures.append({'id': uid, 'reason': 'Original baseline exceeded the processing budget'})
        if evaluation[uid]['videotree'] is not None and (checks[uid]['error'] or not all(v for k,v in checks[uid].items() if k.endswith('_equal'))):
            failures.append({'id': uid, 'reason': checks[uid]})
        if smoke:
            for name in PROFILES:
                p = target/'arms'/name/'runs'/digest(uid)[:20]/'result.json'
                if not p.exists():
                    failures.append({'id': uid, 'profile': name, 'reason': 'Missing result'})
                    continue
                result = read(p)
                if result.get('error') or result['cost_summary'].get('budget_violation') or result.get('compatibility', {}).get('extension_errors'):
                    failures.append({'id': uid, 'profile': name, 'reason': result.get('error') or result.get('compatibility', {}).get('extension_errors') or 'Budget violation'})
    report = {'passed': not failures, 'checked': len(ids), 'comparisons': checks, 'failures': failures,
              'scope': 'Exact original tree/prompt/prediction/token reproduction; historical failed case listed separately'}
    write(target/('status.json' if smoke else 'baseline_gate.json'), report)
    return report['passed']


def run_group(output, group, phase):
    from script_core.backend import preflight
    from script_core.budget import ModelWorker
    target = output/'smoke' if phase == 'smoke' else output
    ids = read(output/('smoke/ids.json' if phase == 'smoke' else f'shards/{group}/ids.json'))
    cfg = load_config(output/'config.json')
    if phase == 'smoke':
        cfg['compatibility']['cache_directory'] = str(target/'common_tool_cache')
    started = time.monotonic()
    cfg, artifacts = preflight(cfg)
    write(target/'setup'/f'{phase}-{group}-artifacts.json', artifacts)
    backend = ModelWorker(cfg)
    try:
        backend.call('prepare_vlm', {}, timeout=300.)
        write(target/'setup'/f'{phase}-{group}.json', {'shared_setup_seconds': time.monotonic()-started})
        for index, uid in enumerate(ids):
            packet = read(output/'inputs'/f'{uid}.json')
            names = ['c0'] if phase == 'baseline' else list(PROFILES) if phase == 'smoke' else [name for name in PROFILES if name != 'c0']
            graph = None
            if phase != 'baseline':
                old = output/'reused_graphs'/f'{packet["question"]["video_id"]}.json'
                graph = read(old) if old.exists() else build_graph(target, packet, backend, cfg)
            predictions = {}
            for name in names:
                result = run_profile(target, name, packet, cfg, backend, graph)
                predictions[name] = (result.get('answer') or {}).get('selected_option')
            write(target/'progress'/f'{phase}-{group}.json', {'completed': index+1, 'planned': len(ids), 'last_question': uid})
            print('COMPATIBILITY', phase, group, index+1, uid, predictions, flush=True)
    finally:
        backend.close()


def score(output):
    with (output/'score.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cohort, labels = read(output/'cohort.json'), read(output/'evaluation_only.json')
        groups, records = {}, {}
        for name in PROFILES:
            values = {}
            for uid in cohort:
                p = output/'arms'/name/'runs'/digest(uid)[:20]/'result.json'
                result = read(p) if p.exists() else None
                pred = ((result or {}).get('answer') or {}).get('selected_option')
                values[uid] = {'completed': result is not None, 'prediction': pred,
                    'correct': bool(result and not result.get('error') and pred == labels[uid]['truth']),
                    'error': (result or {}).get('error'), 'online_seconds': ((result or {}).get('cost_summary') or {}).get('online'),
                    'cache_source_seconds': ((result or {}).get('shared_tool_cost') or {}).get('source_processing_seconds', 0.),
                    'extension_errors': ((result or {}).get('compatibility') or {}).get('extension_errors', []),
                    'accepted_change': ((result or {}).get('compatibility') or {}).get('accepted_change', False)}
            records[name] = values
            done = [v for v in values.values() if v['completed']]
            groups[name] = {'completed': len(done), 'planned': len(cohort), 'correct': sum(v['correct'] for v in done),
                'errors': sum(bool(v['error']) for v in done), 'extension_errors': sum(bool(v['extension_errors']) for v in done),
                'accuracy': sum(v['correct'] for v in done)/len(cohort) if len(done) == len(cohort) else None,
                'mean_actual_online_seconds': statistics.mean(v['online_seconds'] for v in done) if done else None,
                'mean_dependency_equivalent_seconds': statistics.mean(v['cache_source_seconds'] for v in done) if done else None,
                'accepted_changes': sum(v['accepted_change'] for v in done)}
        paired = {}
        for after, before in PARENTS.items():
            ids = [u for u in cohort if records[before][u]['completed'] and records[after][u]['completed']]
            fixed = [u for u in ids if not records[before][u]['correct'] and records[after][u]['correct']]
            lost = [u for u in ids if records[before][u]['correct'] and not records[after][u]['correct']]
            paired[before+'_to_'+after] = {'completed_pairs': len(ids), 'fixed': len(fixed), 'lost': len(lost),
                'fixed_ids': fixed, 'lost_ids': lost, 'net_correct': len(fixed)-len(lost),
                'delta_pp': 100*(len(fixed)-len(lost))/len(cohort) if len(ids) == len(cohort) else None}
        report = {'complete': all(v['completed'] == len(cohort) for v in groups.values()), 'groups': groups,
            'paired': paired, 'baseline_gate': read(output/'baseline_gate.json') if (output/'baseline_gate.json').exists() else None,
            'notes': read(output/'experiment.json')['notes'], 'rows': records}
        write(output/'comparison.json', report)
        return {k:v for k,v in report.items() if k not in ('rows', 'baseline_gate')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'group', 'score'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--reference')
    parser.add_argument('--videotree')
    parser.add_argument('--graphs')
    parser.add_argument('--stage', choices=['smoke', 'baseline0', 'baseline1', 'gate', 'variants0', 'variants1'])
    parser.add_argument('--group')
    parser.add_argument('--phase', choices=['baseline', 'variants'])
    args = parser.parse_args()
    output = Path(args.out).resolve()
    if args.command == 'prepare':
        print(json.dumps(prepare(output, args.reference, args.videotree, args.graphs)))
        return 0
    verify(output)
    if args.command == 'score':
        print(json.dumps(score(output)))
        return 0
    if args.command == 'group':
        run_group(output, args.group, args.phase)
        return 0
    if args.stage == 'gate':
        passed = gate(output)
        print(json.dumps(score(output)))
        return 0 if passed else 2
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('Each inference job requires exactly one visible GPU')
    if args.stage == 'smoke':
        freeze_graphs(output)
        run_group(output, 'smoke', 'smoke')
        return 0 if gate(output, smoke=True) else 2
    phase = 'baseline' if args.stage.startswith('baseline') else 'variants'
    if not read(output/'smoke/status.json')['passed']:
        raise ValueError('Compatibility smoke must pass first')
    if phase == 'variants' and not read(output/'baseline_gate.json')['passed']:
        raise ValueError('Full original baseline must reproduce before ablations')
    groups = ['0', '2'] if args.stage.endswith('0') else ['1', '3']
    def run(group):
        with (output/'shards'/group/f'{phase}.log').open('a') as log:
            return subprocess.run([sys.executable, __file__, 'group', '--out', str(output), '--group', group, '--phase', phase],
                                  cwd=output/'source', stdout=log, stderr=subprocess.STDOUT).returncode
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(run, groups))
    print(json.dumps(score(output)), flush=True)
    return int(any(codes))


if __name__ == '__main__':
    raise SystemExit(main())
