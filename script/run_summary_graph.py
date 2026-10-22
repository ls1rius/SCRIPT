"""Controlled coverage experiments with question-independent caption event graphs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script_core.config import load_config, digest
from script_core.summary_graph import source_records, extraction_inputs, assemble_graph, validate_graph, retrieve_graph

ARMS = {'b0': 'Fresh repaired coverage control; no caption graph',
        'b1': 'b0 fixed visual facts; graph-assisted final QA only',
        'b2': 'Coverage with graph-ranked follow-ups and graph-assisted final QA',
        'b3': 'Protect b0; accept b2 changes only after independent new-visual-fact audit'}


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(s) for s in Path(path).read_text().splitlines() if s.strip()] if Path(path).exists() else []


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(output, reference):
    from script_core.video import load_captions
    output, reference = Path(output).resolve(), Path(reference).resolve()
    if output.exists():
        raise ValueError('Use a new output directory')
    prior = read(reference/'comparison.json')
    cohort = read(reference/'cohort.json')
    if not prior['complete'] or len(cohort) != 300:
        raise ValueError('Require the completed fixed 300-question coverage reference')
    config = load_config(reference/'config.json')
    config['query'].update(reading_policy='coverage_first', caption_first=False)
    write(output/'config.json', config)
    load_config(output/'config.json')
    bases = read(reference/'base/videos.json')
    questions = {}
    for group in range(4):
        for question in rows(reference/f'groups/{group}/questions.jsonl'):
            questions[question['question_id']] = question
    if set(cohort) != set(questions) or len({q['video_id'] for q in questions.values()}) != 300:
        raise ValueError('Expected identical questions and unique videos')
    reuse = {str(reference/'comparison.json'): sha(reference/'comparison.json')}
    for uid in cohort:
        question = questions[uid]
        base = Path(bases[question['video_id']])
        manifest = read(base/'manifest.json')
        captions = source_records(load_captions(manifest)['data'], manifest['cutoff'])
        packet = {'question': question, 'base': str(base), 'captions': captions,
                  'manifest': {k: manifest[k] for k in ('video_id', 'caption_hash', 'caption_uri', 'cutoff', 'duration')}}
        write(output/'inputs'/f'{uid}.json', packet)
        for path in (base/'base_hash.json', base/'manifest.json', Path(manifest['caption_uri'])):
            reuse[str(path)] = sha(path)
    write(output/'cohort.json', cohort)
    write(output/'evaluation_only.json', {r['question_id']: {'truth': r['truth'],
        'historical_prediction': r['prediction'], 'historical_correct': r['correct']} for r in prior['rows']})
    for group in range(4):
        write(output/f'shards/{group}/ids.json', cohort[group::4])
    prefixes = ['45fd8bca', '8345d177', 'd30c1b6c', '0c17076e', '04c51dba']
    write(output/'smoke/ids.json', [next(uid for uid in cohort if uid.startswith(prefix)) for prefix in prefixes])
    write(output/'experiment.json', {'arms': ARMS, 'questions_per_arm': 300, 'reference': str(reference),
        'max_simultaneous_gpus_including_existing_jobs': 2, 'streams_per_gpu': 2,
        'graph_batch_spans': 30, 'max_events_per_batch': 10, 'graph_max_new_tokens': 2048,
        'qa_max_new_tokens': config['models']['generation']['max_new_tokens'],
        'graph_budget_seconds': 900, 'graph_call_cap_seconds': 90, 'online_cap_seconds': 600,
        'scope': ['Original caption text, times and M0 reused; fresh QA and online visual acquisition.',
            'Graph extraction receives captions only, no questions/options/labels or video frames.',
            'Text-label entities and timestamp edges never become confirmed pixel facts or identities.',
            'b2 keeps mandatory coverage checkpoints; graph routing changes ranked follow-ups only.',
            'b1 is fixed-evidence QA replay, not a second end-to-end acquisition.',
            'b3 uses b0+b2 and an audit; all dependency and graph build costs are reported separately.',
            'Graph failures/empty/irrelevant contexts retain ordinary coverage behavior.',
            'Existing development cohort and targeted runtime smoke, not held-out generalization.']})
    files = [ROOT/'script_qa.py', ROOT/'requirements.txt', ROOT/'script/run_summary_graph.py',
             ROOT/'script/summary_graph.pbs', *sorted((ROOT/'script_core').glob('*.py'))]
    hashes = {}
    for path in files:
        name = str(path.relative_to(ROOT))
        target = output/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        hashes[name] = sha(target)
    for name in ('data', 'checkpoints'):
        (output/'source'/name).symlink_to((ROOT/name).resolve(), target_is_directory=True)
    write(output/'source_hashes.json', hashes)
    write(output/'reuse_hashes.json', reuse)
    frozen = [output/'config.json', output/'cohort.json', output/'evaluation_only.json', output/'experiment.json',
              *sorted((output/'inputs').glob('*.json')), *sorted((output/'shards').glob('*/ids.json')), output/'smoke/ids.json']
    write(output/'input_hashes.json', {str(p.relative_to(output)): sha(p) for p in frozen})
    return {'prepared': len(cohort), 'arms': ARMS, 'output': str(output)}


def verify(output):
    for name, root in [('source_hashes.json', output/'source'), ('input_hashes.json', output)]:
        for path, expected in read(output/name).items():
            if sha(root/path) != expected:
                raise ValueError('Frozen source/input changed: '+path)
    for path, expected in read(output/'reuse_hashes.json').items():
        if sha(path) != expected:
            raise ValueError('Reference/caption/M0 changed: '+path)
    load_config(output/'config.json')


def build_graph(target, packet, backend, cfg):
    from script_core.budget import BudgetLedger, Meter, ToolFailure, ResourceExhausted
    path = target/'graphs'/f'{packet["manifest"]["video_id"]}.json'
    if path.exists():
        return validate_graph(read(path), packet['manifest'])
    directory = target/'graph_calls'/packet['manifest']['video_id']
    graph_cfg = deepcopy(cfg)
    graph_cfg['budget'].update(build_call_cap=90., tool_call_cap=90.)
    ledger = BudgetLedger(900., answer_reserve=0, calls_path=directory/'calls.jsonl')
    meter = Meter(backend, ledger, graph_cfg, directory/'raw_calls')
    reports, failures = [], []
    started = time.monotonic()
    try:
        for start in range(0, len(packet['captions']), 30):
            try:
                report, _ = meter.json('summary_graph', extraction_inputs(packet['captions'][start:start+30]), ('events',), build=True)
                reports.append(report)
            except (ToolFailure, ResourceExhausted) as exc:
                failures.append({'batch_start': start, 'error': str(exc)})
        graph = assemble_graph(packet['manifest']['video_id'], packet['manifest']['caption_hash'], packet['captions'], reports, failures)
        graph['cost_summary'] = ledger.summary()
        graph['wall_seconds'] = time.monotonic()-started
        graph['completed_batches'] = len(reports)
        validate_graph(graph, packet['manifest'])
        write(path, graph)
        return graph
    finally:
        meter.close()


def run_online(target, arm, packet, backend, cfg, graph=None):
    from script_core.build import load_base
    from script_core.controller import answer_query
    directory = target/arm/'runs'/digest(packet['question']['question_id'])[:20]
    if (directory/'result.json').exists():
        return read(directory/'result.json')
    started, cpu = time.monotonic(), time.process_time()
    base, metadata = load_base(packet['base'])
    metadata.update(base_restore_seconds=time.monotonic()-started, base_restore_cpu_seconds=time.process_time()-cpu)
    write(directory/'run_config.json', cfg)
    return answer_query(base, packet['question'], metadata, cfg, backend, directory,
                        summary_graph=graph, graph_routing=bool(graph))


def answer_inputs(target, arm, uid):
    directory = target/arm/'runs'/digest(uid)[:20]
    calls = rows(directory/'calls.jsonl')
    call = next((c for c in reversed(calls) if c['operation'] == 'answer' and not c.get('error')), None)
    return read(call['output_ref'])['inputs'] if call else None


def replay_answer(target, packet, graph, baseline, backend, cfg):
    from script_core.budget import BudgetLedger, Meter, ToolFailure, ResourceExhausted
    from script_core.answering import restore_answer_order
    uid = packet['question']['question_id']
    directory = target/'b1/runs'/digest(uid)[:20]
    if (directory/'result.json').exists():
        return read(directory/'result.json')
    inputs = answer_inputs(target, 'b0', uid)
    context = retrieve_graph(graph, dict(packet['question'], scope=packet['question'].get('scope', [0., packet['manifest']['cutoff']])))
    if not inputs or not context:
        result = {'answer': baseline.get('answer'), 'error': baseline.get('error'),
            'reused_baseline': True, 'reason': 'No final baseline input or no relevant graph events',
            'cost_summary': {'online': 0., 'budget_violation': False}}
        write(directory/'result.json', result)
        return result
    inputs = deepcopy(inputs)
    inputs['summary_graph'] = context
    ledger = BudgetLedger(600., calls_path=directory/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, directory/'raw_calls')
    result = {'answer': None, 'error': None, 'cost_scope': 'QA replay over b0 evidence; acquisition and graph costs separate'}
    try:
        report, _ = meter.json('answer', inputs, ('text', 'selected_option', 'claim_ids', 'evidence_ids', 'caption_event_ids'), final=True)
        if not set(report['caption_event_ids']) <= {e['id'] for e in context['events']}:
            raise ValueError('Unknown caption event citation')
        mapping = baseline['answer']['choice_presentation']['presented_to_original']
        result['answer'] = restore_answer_order(report, mapping)
    except (ToolFailure, ResourceExhausted, ValueError) as exc:
        result.update(answer=baseline.get('answer'), error=baseline.get('error'),
                      extension_error=str(exc), reused_baseline=True)
    finally:
        result['cost_summary'] = ledger.summary()
        meter.close()
    write(directory/'result.json', result)
    return result


def new_visual_claims(before, after):
    """Only additional supported visual observations can change protected output."""
    from script_core.summary_graph import words
    def similar(a, b):
        x, y = set(words(a['description'])), set(words(b['description']))
        overlap = min(a['interval'][1], b['interval'][1])-max(a['interval'][0], b['interval'][0])
        return overlap > 0 and len(x & y)/max(1, len(x | y)) >= .8
    known = (before or {}).get('claims', [])
    result = []
    for claim in (after or {}).get('claims', []):
        check = claim.get('verification', {})
        if (claim.get('source_type') != 'visual' or claim.get('status') != 'supported'
                or claim.get('fact_type') == 'inference' or check.get('whole_claim_supported') is not True
                or check.get('unresolved_conflicts') or any(similar(claim, old) for old in known)):
            continue
        # Fail closed for a state-labelled dynamic sentence, including unknown languages.
        if claim.get('fact_type') != 'action' or len(set(claim.get('source_frame_ids', []))) < 3 or not check.get('process_visible'):
            continue
        result.append(claim)
    return result


def protect(target, packet, baseline, candidate, backend, cfg):
    from script_core.budget import BudgetLedger, Meter, ToolFailure, ResourceExhausted
    uid = packet['question']['question_id']
    directory = target/'b3/runs'/digest(uid)[:20]
    if (directory/'result.json').exists():
        return read(directory/'result.json')
    old = (baseline.get('answer') or {}).get('selected_option')
    new = (candidate.get('answer') or {}).get('selected_option')
    accepted, report, extension_error = False, None, None
    ledger = BudgetLedger(600., calls_path=directory/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, directory/'raw_calls')
    claims = new_visual_claims(answer_inputs(target, 'b0', uid), answer_inputs(target, 'b2', uid))
    try:
        if old is None and new is not None and not candidate.get('error'):
            accepted = True
        elif old != new and new is not None and claims and not candidate.get('error'):
            inputs = {'question': packet['question']['question'], 'options': packet['question']['options'],
                      'baseline_option': old, 'candidate_option': new, 'claims': claims}
            report, _ = meter.json('summary_graph_guard', inputs, ('reason', 'claim_ids', 'resolves_distinction', 'accept'), final=True)
            accepted = (report['accept'] is True and report['resolves_distinction'] is True
                        and bool(report['claim_ids']) and set(report['claim_ids']) <= {c['id'] for c in claims})
    except (ToolFailure, ResourceExhausted, ValueError) as exc:
        extension_error = str(exc)
    finally:
        meter.close()
    source = candidate if accepted else baseline
    result = {'answer': source.get('answer'), 'error': source.get('error'), 'accepted_update': accepted and old != new,
        'baseline_prediction': old, 'candidate_prediction': new, 'guard': report, 'extension_error': extension_error,
        'new_visual_claim_ids': [c['id'] for c in claims], 'cost_summary': ledger.summary(),
        'cost_scope': 'Guard only; b0, b2 and graph extraction costs must also be included'}
    write(directory/'result.json', result)
    return result


def run_group(output, group):
    from script_core.backend import preflight
    from script_core.budget import ModelWorker
    target = output/'smoke' if group == 'smoke' else output
    ids = read(output/('smoke/ids.json' if group == 'smoke' else f'shards/{group}/ids.json'))
    started = time.monotonic()
    cfg, artifacts = preflight(load_config(output/'config.json'))
    write(target/'setup'/f'{group}-artifacts.json', artifacts)
    backend = ModelWorker(cfg)
    try:
        backend.call('prepare_vlm', {}, timeout=300.)
        write(target/'setup'/f'{group}.json', {'shared_setup_seconds': time.monotonic()-started})
        for index, uid in enumerate(ids):
            packet = read(output/'inputs'/f'{uid}.json')
            graph = build_graph(target, packet, backend, cfg)
            baseline = run_online(target, 'b0', packet, backend, cfg)
            replay_answer(target, packet, graph, baseline, backend, cfg)
            # Passing None gives exact ordinary coverage if extraction is empty.
            candidate = run_online(target, 'b2', packet, backend, cfg, graph if graph['events'] else None)
            protected = protect(target, packet, baseline, candidate, backend, cfg)
            write(target/'progress'/f'{group}.json', {'completed': index+1, 'planned': len(ids), 'last_question': uid})
            print('SUMMARY_GRAPH', group, index+1, uid, 'events', len(graph['events']),
                  'baseline', (baseline.get('answer') or {}).get('selected_option'),
                  'candidate', (candidate.get('answer') or {}).get('selected_option'),
                  'accepted', protected['accepted_update'], flush=True)
    finally:
        backend.close()


def smoke_gate(output):
    # Runtime/provenance gate only: no labels and no accuracy-based selection.
    target = output/'smoke'
    failures = []
    for uid in read(output/'smoke/ids.json'):
        packet = read(output/'inputs'/f'{uid}.json')
        graph = validate_graph(read(target/'graphs'/f'{packet["manifest"]["video_id"]}.json'), packet['manifest'])
        if not graph['events'] or graph['build_diagnostics']['failures'] or graph['cost_summary']['budget_violation']:
            failures.append({'question_id': uid, 'stage': 'graph', 'reason': 'Empty graph, failed batch or budget violation'})
        for arm in ARMS:
            result = read(target/arm/'runs'/digest(uid)[:20]/'result.json')
            if result.get('error') or result['cost_summary'].get('budget_violation') or result.get('extension_error'):
                failures.append({'question_id': uid, 'stage': arm, 'reason': result.get('error') or result.get('extension_error') or 'Budget violation'})
    write(target/'status.json', {'passed': not failures, 'failures': failures,
          'scope': 'Runtime and source grounding; no label/accuracy gate'})
    return not failures


def score(output):
    with (output/'score.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        labels, cohort = read(output/'evaluation_only.json'), read(output/'cohort.json')
        summaries, results = {}, {}
        for arm in ARMS:
            values = {}
            for uid in cohort:
                path = output/arm/'runs'/digest(uid)[:20]/'result.json'
                result = read(path) if path.exists() else None
                prediction = ((result or {}).get('answer') or {}).get('selected_option')
                values[uid] = {'completed': result is not None, 'prediction': prediction,
                    'correct': bool(result and not result.get('error') and prediction == labels[uid]['truth']),
                    'error': (result or {}).get('error'), 'extension_error': (result or {}).get('extension_error'),
                    'accepted_update': (result or {}).get('accepted_update', False),
                    'online_seconds': ((result or {}).get('cost_summary') or {}).get('online'),
                    'budget_violation': bool(((result or {}).get('cost_summary') or {}).get('budget_violation')),
                    'caption_event_ids': ((result or {}).get('answer') or {}).get('caption_event_ids', [])}
            results[arm] = values
            done = [r for r in values.values() if r['completed']]
            summaries[arm] = {'description': ARMS[arm], 'completed': len(done), 'planned': len(cohort),
                'correct': sum(r['correct'] for r in done), 'errors': sum(bool(r['error']) for r in done),
                'extension_errors': sum(bool(r['extension_error']) for r in done),
                'accuracy': sum(r['correct'] for r in done)/len(cohort) if len(done) == len(cohort) else None,
                'mean_stage_online_seconds': statistics.mean(r['online_seconds'] for r in done) if done else None,
                'budget_violations': sum(r['budget_violation'] for r in done),
                'with_event_citations': sum(bool(r['caption_event_ids']) for r in done),
                'accepted_updates': sum(r['accepted_update'] for r in done)}
        paired = {}
        for before, after in [('b0', 'b1'), ('b0', 'b2'), ('b0', 'b3'), ('b2', 'b3')]:
            ids = [uid for uid in cohort if results[before][uid]['completed'] and results[after][uid]['completed']]
            fixed = [uid for uid in ids if not results[before][uid]['correct'] and results[after][uid]['correct']]
            lost = [uid for uid in ids if results[before][uid]['correct'] and not results[after][uid]['correct']]
            paired[before+'_to_'+after] = {'completed_pairs': len(ids), 'fixed': len(fixed), 'lost': len(lost),
                'fixed_ids': fixed, 'lost_ids': lost, 'net_correct': len(fixed)-len(lost),
                'delta_pp': 100*(len(fixed)-len(lost))/len(cohort) if len(ids) == len(cohort) else None}
        graphs = [read(p) for p in (output/'graphs').glob('*.json')]
        graph_cost = {'completed': len(graphs), 'planned': len(cohort),
            'mean_extraction_seconds': statistics.mean(g['cost_summary']['online'] for g in graphs) if graphs else None,
            'mean_extraction_wall_seconds': statistics.mean(g['wall_seconds'] for g in graphs) if graphs else None,
            'graphs_with_failed_batches': sum(bool(g['build_diagnostics']['failures']) for g in graphs),
            'events': sum(len(g['events']) for g in graphs), 'text_label_entities': sum(len(g['entities']) for g in graphs)}
        graph_lookup = {g['video_id']: g['cost_summary']['online'] for g in graphs}
        for arm in ARMS:
            costs = []
            for uid in cohort:
                item = results[arm][uid]
                if not item['completed']:
                    continue
                graph_seconds = graph_lookup.get(read(output/'inputs'/f'{uid}.json')['question']['video_id'], 0.) if arm != 'b0' else 0.
                required = (item['online_seconds'] or 0.)+graph_seconds
                if arm in ('b1', 'b3'):
                    required += results['b0'][uid]['online_seconds'] or 0.
                if arm == 'b3':
                    required += results['b2'][uid]['online_seconds'] or 0.
                costs.append(required)
            summaries[arm]['mean_required_processing_seconds_excluding_shared_setup'] = statistics.mean(costs) if costs else None
        report = {'complete': all(v['completed'] == len(cohort) for v in summaries.values()), 'groups': summaries,
            'paired': paired, 'shared_graph_preprocessing': graph_cost,
            'historical_coverage_correct': sum(v['historical_correct'] for v in labels.values()),
            'cost_notes': read(output/'experiment.json')['scope'], 'rows': results}
        write(output/'comparison.json', report)
        return {k: v for k, v in report.items() if k != 'rows'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'group', 'score'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--reference')
    parser.add_argument('--stage', choices=['smoke', '0', '1'])
    parser.add_argument('--group')
    args = parser.parse_args()
    output = Path(args.out).resolve()
    if args.command == 'prepare':
        print(json.dumps(prepare(output, args.reference)))
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
        raise RuntimeError('Each job requires exactly one visible GPU')
    if args.stage == 'smoke':
        run_group(output, 'smoke')
        return 0 if smoke_gate(output) else 2
    if not read(output/'smoke/status.json')['passed']:
        raise ValueError('Graph smoke must pass first')
    def run(group):
        path = output/'shards'/group/'run.log'
        with path.open('a') as log:
            return subprocess.run([sys.executable, __file__, 'group', '--out', str(output), '--group', group],
                                  cwd=output/'source', stdout=log, stderr=subprocess.STDOUT).returncode
    groups = ['0', '2'] if args.stage == '0' else ['1', '3']
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(run, groups))
    print(json.dumps(score(output)), flush=True)
    return int(any(codes))


if __name__ == '__main__':
    raise SystemExit(main())
