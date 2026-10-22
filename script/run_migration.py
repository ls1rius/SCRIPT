"""Paired module migration on frozen VideoTree captions and real SCRIPT evidence.

QA stages are fresh inference. Visual observations are frozen replays, not new
end-to-end runs. Two complete original pipeline checks precede the replay suite.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'script'))
from script_core.config import load_config, digest
from script_core.migration import STAGES, PARENTS, original_call, scripted_call, restore_script, review_call, distinguishing_claims, accept_guard, guard_call


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def lines(path):
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def source_facts(run):
    calls = lines(run / 'calls.jsonl') if (run / 'calls.jsonl').exists() else []
    answer = next((c for c in reversed(calls) if c['operation'] == 'answer' and not c.get('error')), None)
    inputs = read(answer['output_ref'])['inputs'] if answer else {}
    return {'claims': inputs.get('claims', []), 'evidence': inputs.get('evidence', []),
            'source_run': str(run), 'source_answer_call': answer['output_ref'] if answer else None,
            'historical_acquisition_seconds': sum(c['actual_cost'] for c in calls if c['operation'] != 'answer')}, inputs


def prepare(output, repairs, vt_root):
    from run_videotree_scaling import BASELINE_FILES
    output, repairs, vt_root = Path(output).resolve(), Path(repairs).resolve(), Path(vt_root).resolve()
    if output.exists():
        raise ValueError('Choose a new experiment directory')
    cohort = read(repairs / 'cohort.json')
    reference = read(repairs / 'comparison.json')
    if len(cohort) != 300 or not reference['complete']:
        raise ValueError('Require the completed fixed 300-question source')
    source_hashes = read(vt_root / 'source_hashes.json')
    for name in BASELINE_FILES:
        if sha(vt_root / 'source' / name) != source_hashes[name] or sha(ROOT / name) != source_hashes[name]:
            raise ValueError('Original VideoTree implementation changed: ' + name)
    questions, base_index, shard_for = {}, {}, {}
    for i in range(4):
        for q in lines(repairs / f'shards/{i}/questions.jsonl'):
            questions[q['question_id']], shard_for[q['question_id']] = q, i
        base_index.update(read(repairs / f'shards/{i}/script_base/videos.json'))
    if set(questions) != set(cohort):
        raise ValueError('Cohort mismatch')
    cfg = load_config(repairs / 'arms/script_guided/config.yaml')
    cfg['query']['answer_strategy'] = 'direct'
    write(output / 'config.json', cfg)
    # Exercise the same serialized configuration path used by GPU workers.
    load_config(output / 'config.json')
    labels = {r['question_id']: {'truth': r['truth']} for r in reference['rows']['script_guided']}
    reuse, all_captions = {}, {}
    for uid in cohort:
        q, shard = questions[uid], shard_for[uid]
        base = Path(base_index[q['video_id']])
        manifest = read(base / 'manifest.json')
        source = read(manifest['caption_uri'])
        spans = source['spans']
        if [(s['start'], s['end']) for s in spans] != [(i, i+1) for i in range(len(spans))]:
            raise ValueError('This controlled study requires original one-second captions')
        captions = [s['text'] for s in spans]
        all_captions[uid] = captions
        facts, coarse = {}, {}
        for arm in ('script_guided', 'coverage_first'):
            run = repairs / f'arms/{arm}/shards/{shard}/runs' / digest(uid)[:20]
            facts[arm], answer_input = source_facts(run)
            if arm == 'script_guided':
                coarse = answer_input
            if facts[arm]['source_answer_call']:
                p = Path(facts[arm]['source_answer_call'])
                reuse[str(p)] = sha(p)
        if not coarse.get('coarse_context'):
            windows = [{k: w[k] for k in ('id', 'interval', 'caption', 'caption_status')} for w in lines(base / 'windows.jsonl')]
            coarse = {'coarse_context': {'status': 'unverified_caption_context', 'windows': windows,
                       'eligible_windows': len(windows), 'selected_windows': len(windows), 'omitted_windows': 0}, 'script_outline': None}
        directory = vt_root / 'videotree_runs' / uid
        tree_path = directory / 'depth_expansion_res.json'
        tree = read(tree_path) if tree_path.exists() else []
        selected = next((r['sorted_values'] for r in tree if r['name'] == uid), None)
        qa_path = directory / 'standard_qa.json'
        cached = read(qa_path) if qa_path.exists() else {}
        cached = cached.get('data', cached)
        item = cached.get(uid)
        if item is None:
            selected = None  # Preserve the original baseline's failed case in the denominator.
        packet = {'question_id': uid, 'video_id': q['video_id'], 'question': q['question'], 'options': q['options'],
                  'duration': int(manifest['duration']), 'captions': captions, 'selected_seconds': selected,
                  'coarse_context': coarse['coarse_context'], 'script_outline': coarse.get('script_outline'), 'facts': facts}
        if item is not None:
            call = original_call(packet)
            if call['prompts'] != item['prompt'] or call['head'] != item['info']['message'][0]['content']:
                raise ValueError('Original QA prompt is not reproduced byte-for-byte: ' + uid)
            labels[uid].update(original_prediction=str(item['pred']), original_prompt_tokens=item['info']['prompt_tokens'])
            reuse[str(qa_path.resolve())] = sha(qa_path)
            reuse[str(tree_path.resolve())] = sha(tree_path)
        else:
            labels[uid].update(original_prediction=None, original_prompt_tokens=None)
        reuse[str(base / 'base_hash.json')] = sha(base / 'base_hash.json')
        reuse[str(Path(manifest['caption_uri']))] = sha(manifest['caption_uri'])
        write(output / 'inputs' / f'{uid}.json', packet)
    write(output / 'captions.json', all_captions)
    write(output / 'cohort.json', cohort)
    write(output / 'evaluation_only.json', labels)
    priority = ['45fd8bca', '8345d177', 'b5865ae0', 'd30c1b6c', 'f1d2978d', '263666b2', '0c17076e', '329c45dd']
    smoke = [next(uid for uid in cohort if uid.startswith(prefix)) for prefix in priority]
    for i in range(4):
        write(output / 'shards' / str(i) / 'ids.json', cohort[i::4])
    write(output / 'smoke/ids.json', smoke)
    spec = {'mode': 'fixed_evidence_incremental_migration', 'stages': STAGES, 'parents': PARENTS,
            'questions_per_stage': 300, 'repairs_source': str(repairs), 'videotree_source': str(vt_root),
            'model': cfg['models']['vlm'], 'temperature': 0, 'max_new_tokens': cfg['models']['generation']['max_new_tokens'],
            'max_simultaneous_gpus': 2, 'streams_per_gpu': 2, 'complete_pipeline_check_ids': smoke[:2],
            'historical_controls': {
                'caption_first': str((ROOT / 'workdirs/script-caption-first-300-20261017-r1').resolve()),
                'live_coverage': str((ROOT / 'workdirs/script-caption-first-matched-300-20261017-r1/coverage_first').resolve())},
            'cost_notes': ['Stages use freshly generated QA on fixed original trees/captions and frozen real visual facts.',
                'Visual acquisition costs are historical and reported separately; replay time is not end-to-end latency.',
                'Two complete unchanged VideoTree tree+QA checks precede all-stage smoke checks.',
                'One historical VideoTree tree/QA failure remains an error in v0; later stages may still answer.',
                'g7 is a branch from v0 and s5; it is not another independent visual acquisition.',
                'All questions are the existing development cohort; no held-out generalization claim.']}
    data = read(vt_root / 'scaling_spec.json')['data_root']
    spec['data_root'] = data
    spec['feature_path'] = str((ROOT / 'data/EgoSchema/egoschema_features').resolve())
    for uid in spec['complete_pipeline_check_ids']:
        clean = {uid: {'question': questions[uid]['question'], **{f'option {i}': t for i, t in enumerate(questions[uid]['options'])}}}
        write(output / 'smoke/tree_inputs' / f'{uid}.json', clean)
        feature = Path(spec['feature_path']) / f'{uid}.pt'
        reuse[str(feature)] = sha(feature)
    duration = Path(data) / 'duration.json'
    reuse[str(duration)] = sha(duration)
    write(output / 'experiment.json', spec)
    write(output / 'reuse_hashes.json', reuse)
    files = [ROOT / 'script/run_migration.py', ROOT / 'script/migration_backend.py', ROOT / 'script/migration.pbs',
             ROOT / 'requirements.txt', *sorted((ROOT / 'script_core').glob('*.py'))]
    hashes = {}
    for path in files:
        name = str(path.relative_to(ROOT))
        target = output / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        hashes[name] = sha(target)
    for name in BASELINE_FILES:
        target = output / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(vt_root / 'source' / name, target)
        hashes[name] = sha(target)
    for name in ('data', 'checkpoints'):
        (output / 'source' / name).symlink_to((ROOT / name).resolve(), target_is_directory=True)
    write(output / 'source_hashes.json', hashes)
    frozen = [p for p in output.rglob('*.json') if 'source' not in p.parts and p.name != 'source_hashes.json']
    write(output / 'input_hashes.json', {str(p.relative_to(output)): sha(p) for p in frozen})
    return spec


def verify(output):
    for name, expected in read(output / 'source_hashes.json').items():
        if sha(output / 'source' / name) != expected:
            raise ValueError('Frozen source changed: ' + name)
    for name, expected in read(output / 'input_hashes.json').items():
        if sha(output / name) != expected:
            raise ValueError('Frozen input changed: ' + name)
    for name, expected in read(output / 'reuse_hashes.json').items():
        if sha(name) != expected:
            raise ValueError('Source evidence/cache changed: ' + name)


def run_stage(output, uid, packet, stage, backend, cfg, previous):
    from script_core.budget import BudgetLedger, Meter, ToolFailure, ResourceExhausted
    run = output / 'results' / uid / stage
    if (run / 'result.json').exists():
        return read(run / 'result.json')
    started = time.monotonic()
    ledger = BudgetLedger(600., calls_path=run / 'calls.jsonl')
    meter = Meter(backend, ledger, cfg, run / 'raw_calls')
    result = {'stage': stage, 'question_id': uid, 'prediction': None, 'error': None,
              'visual_evidence_mode': 'frozen_replay', 'fresh_visual_reads': 0, 'historical_acquisition_seconds': 0.}
    try:
        if stage in ('v0', 'v1'):
            inputs = original_call(packet, all_seconds=stage == 'v1')
            if stage == 'v0':
                result['original_input_sha256'] = digest(inputs)
                result['extensions_disabled'] = True
            raw, call = meter.call('videotree_qa', inputs, final=True)
            result.update(prediction=raw['data']['selected_option'], answer=raw['data'], input_tokens=call['input_tokens'])
        elif stage == 'c8':
            inputs, mapping = review_call(packet)
            result['input_sha256'] = digest(inputs)
            report, _ = meter.json('caption_review', inputs, ('text', 'selected_option', 'caption_ids', 'checks'))
            report = restore_script(report, mapping)
            result.update(prediction=report['selected_option'], answer=report, requested_reads=len(report['checks']))
        elif stage == 'g7':
            baseline, candidate = previous['v0']['prediction'], previous['s5']['prediction']
            baseline = int(baseline) if baseline is not None else None
            candidate = int(candidate) if candidate is not None else None
            eligible = distinguishing_claims(packet, baseline, candidate)
            result.update(baseline_prediction=previous['v0']['prediction'], candidate_prediction=previous['s5']['prediction'],
                          eligible_claim_ids=[c['id'] for c in eligible], accepted=False)
            report = None
            if eligible:
                try:
                    raw, _ = meter.call('migration_guard', guard_call(packet, baseline, candidate, eligible), final=True)
                    report = raw['data']
                    result['accepted'] = isinstance(report, dict) and accept_guard(report, eligible)
                except (ToolFailure, ResourceExhausted, ValueError) as exc:
                    result['extension_error'] = str(exc)
            source = previous['s5'] if result['accepted'] else previous['v0']
            result.update(prediction=source['prediction'], answer=source.get('answer'), error=source.get('error'), guard=report)
            result['historical_acquisition_seconds'] = packet['facts']['script_guided']['historical_acquisition_seconds']
        else:
            inputs, mapping = scripted_call(packet, stage)
            result['input_sha256'] = digest(inputs)
            # Identical no-fact inputs must preserve the earlier stage exactly.
            same = next((r for r in previous.values() if r.get('input_sha256') == result['input_sha256']), None)
            if same:
                result.update(prediction=same['prediction'], answer=same.get('answer'), error=same.get('error'),
                              reused_identical_input=same['stage'], standalone_qa_seconds=same['standalone_qa_seconds'],
                              standalone_input_tokens=same.get('standalone_input_tokens', 0))
            else:
                report, _ = meter.json('answer', inputs, ('text', 'selected_option', 'claim_ids', 'evidence_ids'), final=True)
                report = restore_script(report, mapping)
                result.update(prediction=report['selected_option'], answer=report)
            if stage in ('s5', 's6'):
                result['historical_acquisition_seconds'] = packet['facts']['script_guided' if stage == 's5' else 'coverage_first']['historical_acquisition_seconds']
        if result['prediction'] is not None and result['prediction'] not in list('01234'):
            raise ValueError('Illegal canonical prediction')
    except (ValueError, KeyError, TypeError, ToolFailure, ResourceExhausted) as exc:
        result['error'] = str(exc)
        result['prediction'] = None
    finally:
        meter.close()
    result['cost_summary'] = ledger.summary()
    result['actual_input_tokens'] = sum(c.get('input_tokens', 0) or 0 for c in ledger.calls)
    result.setdefault('standalone_input_tokens', result['actual_input_tokens'])
    result['caption_windows_omitted'] = sum(len((c.get('model_caption_context') or {}).get('omitted_window_ids', [])) for c in ledger.calls)
    result['replay_wall_seconds'] = time.monotonic() - started
    result.setdefault('standalone_qa_seconds', result['replay_wall_seconds'])
    if stage == 'g7':
        result['standalone_qa_seconds'] += previous['v0']['standalone_qa_seconds'] + previous['s5']['standalone_qa_seconds']
        result['standalone_input_tokens'] += previous['v0'].get('standalone_input_tokens', 0) + previous['s5'].get('standalone_input_tokens', 0)
    write(run / 'result.json', result)
    return result


def run_group(output, group):
    from migration_backend import MigrationWorker
    cfg = load_config(output / 'config.json')
    ids = read(output / ('smoke/ids.json' if group == 'smoke' else f'shards/{group}/ids.json'))
    backend = MigrationWorker(cfg)
    try:
        # Model/tokenizer initialization is reported separately from all stages.
        setup = time.monotonic()
        backend.call('prepare_vlm', {}, timeout=300.)
        write(output / 'setup' / f'{group}.json', {'seconds': time.monotonic()-setup, 'scope': 'Shared worker setup, excluded from QA-only stage latency'})
        for uid in ids:
            packet = read(output / 'inputs' / f'{uid}.json')
            previous = {}
            for stage in STAGES:
                # Smoke results are isolated and never reused as the 300-question results.
                target = output / 'smoke' if group == 'smoke' else output
                previous[stage] = run_stage(target, uid, packet, stage, backend, cfg, previous)
            write(output / 'progress' / f'{group}.json', {'completed': ids.index(uid)+1, 'planned': len(ids), 'last_question': uid})
            print('MIGRATION', group, ids.index(uid)+1, uid, {s: r['prediction'] for s, r in previous.items()}, flush=True)
    finally:
        backend.close()


def original_pipeline_checks(output):
    spec = read(output / 'experiment.json')
    checks = []
    for uid in spec['complete_pipeline_check_ids']:
        directory = output / 'smoke' / 'original_pipeline' / uid
        directory.mkdir(parents=True, exist_ok=True)
        common = ['--anno_path', output / 'smoke/tree_inputs' / f'{uid}.json', '--data_path', output / 'captions.json',
                  '--duration_path', Path(spec['data_root']) / 'duration.json', '--frame_feat_path', spec['feature_path'],
                  '--model', spec['model'], '--max_new_tokens', str(spec['max_new_tokens']),
                  '--output_base_path', directory, '--save_info', '--start_from_scratch', '--disable_eval']
        commands = [
            ['adaptive_breath_expansion.py', *common, '--output_filename', 'relevance_score.json', '--prompt_type', 'cap_score'],
            ['depth_expansion.py', '--relevance_path', directory/'relevance_score.json', '--width_path', directory/'width_res.json', '--output_path', directory/'depth_expansion_res.json'],
            ['main_qa.py', *common, '--output_filename', 'standard_qa.json', '--tree_node_idx', directory/'depth_expansion_res.json']]
        for args in commands:
            with (directory / 'pipeline.log').open('a') as log:
                subprocess.run([sys.executable, *map(str, args)], cwd=output/'source', stdout=log, stderr=subprocess.STDOUT, check=True)
        fresh = read(directory / 'standard_qa.json')
        fresh = fresh.get('data', fresh)[uid]
        packet = read(output / 'inputs' / f'{uid}.json')
        original = original_call(packet)
        indices = next(r['sorted_values'] for r in read(directory/'depth_expansion_res.json') if r['name'] == uid)
        check = {'question_id': uid, 'same_tree_indices': indices == packet['selected_seconds'],
                 'same_head': fresh['info']['message'][0]['content'] == original['head'],
                 'same_prompt': fresh['prompt'] == original['prompts'], 'prediction': str(fresh['pred'])}
        checks.append(check)
    write(output / 'smoke/original_pipeline_checks.json', checks)


def smoke_gate(output):
    # Labels and old predictions are only loaded after all smoke inference.
    evaluation = read(output / 'evaluation_only.json')
    failures = []
    for row in read(output / 'smoke/original_pipeline_checks.json'):
        if (not row['same_tree_indices'] or not row['same_head'] or not row['same_prompt']
                or row['prediction'] != evaluation[row['question_id']]['original_prediction']):
            failures.append({'id': row['question_id'], 'reason': 'Complete original pipeline reproduction mismatch'})
    for uid in read(output / 'smoke/ids.json'):
        for stage in STAGES:
            result = read(output / 'smoke/results' / uid / stage / 'result.json')
            if result['error'] or result['cost_summary']['budget_violation']:
                failures.append({'id': uid, 'stage': stage, 'reason': result['error'] or 'Replay budget violation'})
            if stage == 'v0' and (result['prediction'] != evaluation[uid]['original_prediction'] or result['input_tokens'] != evaluation[uid]['original_prompt_tokens']):
                failures.append({'id': uid, 'reason': 'Cached-tree original QA replay mismatch'})
    write(output / 'smoke/status.json', {'passed': not failures, 'failures': failures, 'questions': 8,
                                       'scope': 'Compatibility and runtime gate, not an accuracy estimate'})
    return not failures


def score(output):
    with (output / 'score.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        labels, cohort = read(output / 'evaluation_only.json'), read(output / 'cohort.json')
        groups, rows = {}, {}
        for stage in STAGES:
            rows[stage] = {}
            for uid in cohort:
                path = output / 'results' / uid / stage / 'result.json'
                try:
                    result = read(path) if path.exists() else None
                except json.JSONDecodeError:
                    result = None
                row = {'question_id': uid, 'completed': result is not None, 'truth': labels[uid]['truth'],
                       'prediction': (result or {}).get('prediction'), 'error': (result or {}).get('error'),
                       'correct': bool(result and not result['error'] and result['prediction'] == labels[uid]['truth']),
                       'replay_wall_seconds': (result or {}).get('replay_wall_seconds'),
                       'standalone_qa_seconds': (result or {}).get('standalone_qa_seconds'),
                       'historical_acquisition_seconds': (result or {}).get('historical_acquisition_seconds'),
                       'guard_accepted': (result or {}).get('accepted'),
                       'extension_error': (result or {}).get('extension_error'),
                       'requested_reads': (result or {}).get('requested_reads'),
                       'standalone_input_tokens': (result or {}).get('standalone_input_tokens'),
                       'caption_windows_omitted': (result or {}).get('caption_windows_omitted', 0),
                       'budget_violation': bool(((result or {}).get('cost_summary') or {}).get('budget_violation'))}
                if stage == 'v0' and result:
                    row['matches_historical_prediction'] = result['prediction'] == labels[uid]['original_prediction']
                rows[stage][uid] = row
            done = [r for r in rows[stage].values() if r['completed']]
            mean = lambda key: statistics.mean(r[key] for r in done if r[key] is not None) if any(r[key] is not None for r in done) else None
            groups[stage] = {'description': STAGES[stage], 'planned': len(cohort), 'completed': len(done),
                             'correct': sum(r['correct'] for r in done), 'errors': sum(bool(r['error']) for r in done),
                             'accuracy': sum(r['correct'] for r in done)/len(cohort) if len(done) == len(cohort) else None,
                             'mean_replay_wall_seconds': mean('replay_wall_seconds'), 'mean_standalone_qa_seconds': mean('standalone_qa_seconds'),
                             'mean_historical_acquisition_seconds': mean('historical_acquisition_seconds'),
                             'mean_standalone_input_tokens': mean('standalone_input_tokens'),
                             'questions_with_caption_omissions': sum(r['caption_windows_omitted'] > 0 for r in done),
                             'budget_violations': sum(r['budget_violation'] for r in done)}
            if stage == 'v0':
                groups[stage]['prediction_reproduction_mismatches'] = sum(not r['matches_historical_prediction'] for r in done)
            if stage == 'g7':
                groups[stage]['accepted_updates'] = sum(r['guard_accepted'] is True for r in done)
                groups[stage]['guard_failures'] = sum(bool(r['extension_error']) for r in done)
            if stage == 'c8':
                groups[stage]['questions_requesting_inspection'] = sum(bool(r['requested_reads']) for r in done)
        paired = {}
        for stage, parent in PARENTS.items():
            pairs = [(rows[parent][uid], rows[stage][uid]) for uid in cohort if rows[parent][uid]['completed'] and rows[stage][uid]['completed']]
            paired[f'{parent}_to_{stage}'] = {'completed_pairs': len(pairs),
                'fixed': sum(not a['correct'] and b['correct'] for a, b in pairs),
                'lost': sum(a['correct'] and not b['correct'] for a, b in pairs),
                'both_correct': sum(a['correct'] and b['correct'] for a, b in pairs),
                'both_wrong': sum(not a['correct'] and not b['correct'] for a, b in pairs)}
        pairs = [(rows['v0'][uid], rows['g7'][uid]) for uid in cohort if rows['v0'][uid]['completed'] and rows['g7'][uid]['completed']]
        paired['v0_to_g7'] = {'completed_pairs': len(pairs), 'fixed': sum(not a['correct'] and b['correct'] for a,b in pairs),
                               'lost': sum(a['correct'] and not b['correct'] for a,b in pairs)}
        for name, item in paired.items():
            before, after = name.split('_to_')
            item['fixed_ids'] = [uid for uid in cohort if rows[before][uid]['completed'] and rows[after][uid]['completed']
                                 and not rows[before][uid]['correct'] and rows[after][uid]['correct']]
            item['lost_ids'] = [uid for uid in cohort if rows[before][uid]['completed'] and rows[after][uid]['completed']
                                and rows[before][uid]['correct'] and not rows[after][uid]['correct']]
            item['net_correct'] = item['fixed'] - item['lost']
            item['delta_accuracy_pp'] = 100 * item['net_correct'] / len(cohort) if item['completed_pairs'] == len(cohort) else None
            n, k = item['fixed'] + item['lost'], min(item['fixed'], item['lost'])
            item['paired_exact_p_unadjusted'] = min(1., 2 * sum(math.comb(n, i) for i in range(k+1)) / 2**n) if n else 1.
        controls = {}
        for name, path in read(output / 'experiment.json').get('historical_controls', {}).items():
            observed = []
            for p in Path(path).glob('groups/[0-3]/runs/*/result.json'):
                try:
                    r, q = read(p), read(p.parent / 'query.json')
                except json.JSONDecodeError:
                    continue
                if q['id'] in labels:
                    observed.append((r, labels[q['id']]['truth']))
            controls[name] = {'completed': len(observed), 'planned': len(cohort),
                'correct': sum(not r.get('error') and (r.get('answer') or {}).get('selected_option') == truth for r, truth in observed),
                'errors': sum(bool(r.get('error')) for r, _ in observed),
                'scope': 'Existing full online pipeline, not a module-isolated QA replay', 'source': path}
            controls[name]['accuracy'] = controls[name]['correct'] / len(cohort) if len(observed) == len(cohort) else None
        report = {'complete': all(g['completed'] == len(cohort) for g in groups.values()), 'groups': groups,
                  'paired': paired, 'online_controls': controls,
                  'significance_note': 'Exploratory paired exact tests across several comparisons; p-values are unadjusted, not held-out evidence.',
                  'cost_scope': read(output/'experiment.json')['cost_notes'], 'rows': rows}
        write(output / 'comparison.json', report)
        return {k: v for k,v in report.items() if k != 'rows'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'group', 'score'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--repairs')
    parser.add_argument('--videotree')
    parser.add_argument('--stage', choices=['smoke', '0', '1'])
    parser.add_argument('--group')
    args = parser.parse_args()
    output = Path(args.out).resolve()
    if args.command == 'prepare':
        print(json.dumps(prepare(output, args.repairs, args.videotree), ensure_ascii=False))
        return 0
    verify(output)
    if args.command == 'score':
        print(json.dumps(score(output), ensure_ascii=False))
        return 0
    if args.command == 'group':
        run_group(output, args.group)
        return 0
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('Each job requires one visible GPU')
    if args.stage == 'smoke':
        original_pipeline_checks(output)
        run_group(output, 'smoke')
        return 0 if smoke_gate(output) else 2
    if not read(output / 'smoke/status.json')['passed']:
        raise ValueError('Compatibility smoke must pass first')
    groups = ['0', '2'] if args.stage == '0' else ['1', '3']
    def run(group):
        directory = output / 'shards' / group
        with (directory / 'run.log').open('a') as log:
            return subprocess.run([sys.executable, __file__, 'group', '--out', str(output), '--group', group],
                                  cwd=output/'source', stdout=log, stderr=subprocess.STDOUT).returncode
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(run, groups))
    print(json.dumps(score(output)), flush=True)
    return int(any(codes))


if __name__ == '__main__':
    raise SystemExit(main())
