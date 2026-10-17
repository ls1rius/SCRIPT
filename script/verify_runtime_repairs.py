"""Rerun exactly the failed development questions using frozen source and M0."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def read(path):
    return json.loads(Path(path).read_text())


def write(path, data):
    from script_core.state import write_json
    write_json(path, data)


def now():
    return datetime.now(timezone.utc).isoformat()


def prepare(output, previous, shards=4, streams_per_gpu=2, cohort_size=None, failure_code=None, question_ids=None, reading_policy=None):
    from script_core.config import digest
    output, previous = Path(output).resolve(), Path(previous).resolve()
    if output.exists():
        raise ValueError('Choose a new verification directory')
    if shards < 1 or streams_per_gpu < 1 or shards % streams_per_gpu:
        raise ValueError('Shards must divide evenly among GPU jobs')
    if reading_policy not in (None, 'coverage_first', 'script_guided'):
        raise ValueError('Unknown reading policy')
    selected = set(question_ids) if question_ids is not None else None
    if selected is not None and (not selected or len(selected) != len(question_ids) or failure_code is not None):
        raise ValueError('Explicit regression IDs must be distinct, nonempty, and used without a failure filter')
    cohort = set(read(previous/'cohorts.json')[str(cohort_size)]) if cohort_size else None
    questions, bases, failures = [], {}, []
    for shard in sorted((previous/'shards').iterdir()):
        index = {}
        paths = [shard/'script_base/videos.json', *sorted(shard.glob('stages/*/script_base/videos.json'))]
        for path in paths:
            if path.exists():
                index.update(read(path))
        for line in (shard/'questions.jsonl').read_text().splitlines():
            q = json.loads(line)
            if selected is not None and q['question_id'] not in selected:
                continue
            if cohort is not None and q['question_id'] not in cohort:
                continue
            source = shard/'script_runs'/digest(q['question_id'])[:20]/'result.json'
            if not source.exists():
                continue
            result = read(source)
            error = result.get('error') or {}
            if selected is not None or (error and (failure_code is None or error.get('code') == failure_code)):
                if q['video_id'] not in index:
                    # Scaling cohorts symlink the original 100 query runs; their
                    # immutable M0 index belongs to the original shard instead.
                    origin_shard = source.resolve().parent.parent.parent
                    reused_index = origin_shard/'script_base/videos.json'
                    if reused_index.exists():
                        index.update(read(reused_index))
                if q['video_id'] not in index:
                    raise ValueError('Missing frozen M0 for selected regression: '+q['video_id'])
                questions.append(q)
                bases[q['video_id']] = index[q['video_id']]
                failures.append({'question_id': q['question_id'], 'error': result.get('error'), 'source': str(source),
                                 'prediction': (result.get('answer') or {}).get('selected_option'),
                                 'online_seconds': result.get('cost_summary', {}).get('online')})
    if selected is not None and selected != {q['question_id'] for q in questions}:
        raise ValueError('Selected regression questions are missing or outside the requested cohort')
    if not questions:
        raise ValueError('No failed questions to verify')
    if len(questions) < shards:
        raise ValueError('Do not prepare empty verification shards')
    output.mkdir(parents=True)
    shutil.copyfile(previous/'config.yaml', output/'config.yaml')
    if reading_policy is not None:
        import yaml
        config = yaml.safe_load((output/'config.yaml').read_text())
        config.setdefault('query', {}).update(reading_policy=reading_policy, max_script_reads=3, script_context_windows=12)
        (output/'config.yaml').write_text(yaml.safe_dump(config, sort_keys=False))
    outcomes = 'original_outcomes.json' if selected is not None else 'original_failures.json'
    write(output/outcomes, failures)
    for i in range(shards):
        directory = output/'shards'/str(i)
        directory.mkdir(parents=True)
        subset = questions[i::shards]
        (directory/'questions.jsonl').write_text(''.join(json.dumps(q, ensure_ascii=False)+'\n' for q in subset))
        write(directory/'base/videos.json', {q['video_id']: bases[q['video_id']] for q in subset})
    snapshot = output/'source'
    names = list(read(previous/'source_hashes.json'))
    names += ['script/verify_runtime_repairs.py', 'script/runtime_reliability.pbs', 'script/parse_recheck.pbs',
              'script/case_recheck.pbs']
    names += [str(p.relative_to(ROOT)) for p in (ROOT/'script_core').glob('*.py')]
    hashes = {}
    for name in dict.fromkeys(names):
        target = snapshot/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, target)
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    for name in ('checkpoints', 'data'):
        (snapshot/name).symlink_to((ROOT/name).resolve(), target_is_directory=True)
    write(output/'source_hashes.json', hashes)
    input_hashes = {}
    for path in [output/'config.yaml', output/outcomes,
                 *output.glob('shards/*/questions.jsonl'), *output.glob('shards/*/base/videos.json'),
                 *(Path(base)/'base_hash.json' for base in bases.values()),
                 *(Path(base)/'state.sqlite3' for base in bases.values()),
                 *(Path(failure['source']) for failure in failures)]:
        input_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(output/'input_hashes.json', input_hashes)
    write(output/'verification_spec.json', {'created_at': now(), 'previous': str(previous),
        'planned': len(questions), 'question_ids': [q['question_id'] for q in questions],
        'shards': shards, 'gpu_jobs': shards//streams_per_gpu, 'streams_per_gpu': streams_per_gpu,
        'source_cohort_size': cohort_size, 'failure_code': failure_code,
        'selection_mode': 'explicit_cases' if selected is not None else 'original_failures',
        'reading_policy': reading_policy,
        'purpose': ('Targeted end-to-end development-case regression; not a population accuracy estimate.'
                    if selected is not None else 'Runtime regression validation on the original failures; not an accuracy estimate.'),
        'constraints': {'online_cap_seconds': 600, 'global_task_limit': 8, 'max_new_tokens': 1024,
                        'model': 'Qwen2.5-VL-7B-Instruct'}})
    if (previous/'evaluation_only.json').exists():
        selected = {q['question_id'] for q in questions}
        labels = read(previous/'evaluation_only.json')['questions']
        write(output/'evaluation_only.json', {'questions': [q for q in labels if q['question_id'] in selected]})
        input_hashes[str(output/'evaluation_only.json')] = hashlib.sha256((output/'evaluation_only.json').read_bytes()).hexdigest()
        write(output/'input_hashes.json', input_hashes)
    return str(output)


def verify_snapshot(output):
    for name, expected in read(output/'source_hashes.json').items():
        if hashlib.sha256((output/'source'/name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Source changed: {name}')
    for path, expected in read(output/'input_hashes.json').items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Input changed: {path}')


def prepare_build_probe(output, count=2):
    """Select a few costly M0 sources for a separate, freshly built diagnostic."""
    if type(count) is not int or count < 1:
        raise ValueError('Build probe count must be positive')
    bases = {}
    for path in output.glob('shards/*/base/videos.json'):
        bases.update(read(path))
    choices = []
    for uid, directory in bases.items():
        directory = Path(directory)
        summaries = [json.loads(line) for line in (directory/'summaries.jsonl').read_text().splitlines()]
        cost = read(directory/'build_cost.json')
        operations = cost['by_operation']
        choices.append({'video_id': uid, 'base': str(directory), 'old_cost': cost,
            'old_unreadable_summaries': sum(s['status'] != 'current' for s in summaries),
            'summary_processing_seconds': operations.get('summarize', 0.)+operations.get('repair', 0.)})
    choices.sort(key=lambda r: (-r['old_unreadable_summaries'], -r['summary_processing_seconds'], r['video_id']))
    choices = choices[:count]
    manifest = []
    hashes = read(output/'input_hashes.json')
    for row in choices:
        source = Path(row['base'])
        m = read(source/'manifest.json')
        manifest.append({key: m[key] for key in ('video_id', 'uri', 'cutoff', 'caption_uri', 'subtitle_uri')
                         if m.get(key) is not None})
        for name in ('manifest.json', 'summaries.jsonl', 'build_cost.json'):
            path = source/name
            hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(output/'build_probe/manifest.json', manifest)
    write(output/'build_probe/spec.json', {'selected': choices,
        'limitations': ['Selected costly development cases; not an estimate of mean M0 cost.',
                        'GPU sharing, model warmup and filesystem cache can differ from the historical run.',
                        'These new bases are separate from the M0 used by the eight-query reading ablation.']})
    for name in ('manifest.json', 'spec.json'):
        path = output/'build_probe'/name
        hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(output/'input_hashes.json', hashes)


def run_build_probe(output):
    verify_snapshot(output)
    probe = output/'build_probe'
    args = [sys.executable, output/'source/script_qa.py', 'build', '--manifest', probe/'manifest.json',
            '--config', output/'config.yaml', '--out', probe/'base']
    with (probe/'build.log').open('w') as log:
        process = subprocess.run(list(map(str, args)), cwd=output/'source', stdout=log, stderr=subprocess.STDOUT)
    from script_core.config import digest
    rows = []
    for old in read(probe/'spec.json')['selected']:
        base = probe/'base'/digest(old['video_id'])[:20]
        cost = read(base/'build_cost.json') if (base/'build_cost.json').exists() else None
        summaries = ([json.loads(line) for line in (base/'summaries.jsonl').read_text().splitlines()]
                     if (base/'summaries.jsonl').exists() else [])
        rows.append({**old, 'new_cost': cost, 'new_summary_count': len(summaries),
                     'new_unreadable_summaries': sum(s['status'] != 'current' for s in summaries),
                     'complete': (base/'base_hash.json').exists()})
    passed = process.returncode == 0 and all(r['complete'] and r['new_summary_count'] and not r['new_unreadable_summaries'] for r in rows)
    report = {'returncode': process.returncode, 'passed': passed, 'rows': rows,
              'limitations': read(probe/'spec.json')['limitations'], 'ended_at': now()}
    write(probe/'summary.json', report)
    return report


def inspect_results(output, shards):
    from script_core.config import digest
    results, errors, violations, missing = [], [], [], []
    for i in shards:
        directory = output/'shards'/str(i)
        for line in (directory/'questions.jsonl').read_text().splitlines():
            q = json.loads(line)
            key = digest(q['question_id'])[:20]
            path = directory/'runs'/key/'result.json'
            failure = directory/'runs/query_failures'/(key+'.json')
            if failure.exists():
                errors.append({'question_id': q['question_id'], 'error': read(failure)['error']})
                continue
            if not path.exists():
                missing.append(q['question_id'])
                continue
            result = read(path)
            results.append(q['question_id'])
            answer = result.get('answer') or {}
            if result.get('error') or answer.get('selected_option') not in [str(n) for n in range(len(q['options']))] or not answer.get('text'):
                errors.append({'question_id': q['question_id'], 'error': result.get('error') or 'Invalid/empty answer'})
            if result['cost_summary'].get('budget_violation') or result['cost_summary']['online'] > 600.05:
                violations.append(q['question_id'])
    return {'checked_at': now(), 'results': len(results), 'errors': errors, 'missing': missing,
            'budget_violations': violations, 'passed': not errors and not missing and not violations}


def run_job(output, index):
    verify_snapshot(output)
    spec = read(output/'verification_spec.json')
    if not 0 <= index < spec['gpu_jobs']:
        raise ValueError('Job index outside prepared verification')
    shards = list(range(index, spec['shards'], spec['gpu_jobs']))
    state = {'job_id': os.environ.get('PBS_JOBID'), 'started_at': now(), 'shards': shards, 'status': 'running'}
    write(output/'jobs'/f'job-{index}-status.json', state)
    processes = {}
    with ExitStack() as stack:
        for i in shards:
            directory = output/'shards'/str(i)
            log = stack.enter_context((directory/'worker.log').open('a', buffering=1))
            args = [sys.executable, output/'source/script_qa.py', 'query', '--base', directory/'base',
                    '--questions', directory/'questions.jsonl', '--config', output/'config.yaml',
                    '--out', directory/'runs', '--resume']
            processes[i] = subprocess.Popen(list(map(str, args)), cwd=output/'source', stdout=log, stderr=subprocess.STDOUT)
        codes = {i: process.wait() for i, process in processes.items()}
    report = inspect_results(output, shards)
    state.update(ended_at=now(), status='finished', returncodes=codes, **report)
    write(output/'jobs'/f'job-{index}-status.json', state)
    print(json.dumps(state), flush=True)
    return int(any(codes.values()) or not report['passed'])


def summarize(output):
    """Audit execution and compare labels only after the selected queries finish."""
    from check_run_integrity import audit
    from script_core.evaluation import evaluate
    verify_snapshot(output)
    spec = read(output/'verification_spec.json')
    report = inspect_results(output, range(spec['shards']))
    audits = {}
    for i in range(spec['shards']):
        folder = output/'shards'/str(i)
        verdict = audit(folder/'runs')
        expected = len((folder/'questions.jsonl').read_text().splitlines())
        verdict.update(expected_queries=expected, passed=verdict['passed'] and verdict['queries'] == expected)
        write(folder/'integrity_report.json', verdict)
        audits[str(i)] = verdict
    evaluation = evaluate(output/'shards', output/'evaluation_only.json', output/'evaluation')
    passed = report['passed'] and all(a['passed'] for a in audits.values())
    report.update(status='complete' if passed else 'complete_with_errors', ended_at=now(),
                  integrity=audits, evaluation=evaluation['groups'], passed=passed, scope=spec['purpose'])
    outcomes = output/'original_outcomes.json'
    if outcomes.exists():
        from script_core.config import digest
        labels = {q['question_id']: q['selected_option'] for q in read(output/'evaluation_only.json')['questions']}
        old = {r['question_id']: r for r in read(outcomes)}
        paired = []
        for i in range(spec['shards']):
            for line in (output/'shards'/str(i)/'questions.jsonl').read_text().splitlines():
                q = json.loads(line)
                uid = q['question_id']
                path = output/'shards'/str(i)/'runs'/digest(uid)[:20]/'result.json'
                result = read(path) if path.exists() else {}
                prediction = (result.get('answer') or {}).get('selected_option')
                paired.append({'question_id': uid, 'truth': labels[uid],
                    'old_prediction': old[uid]['prediction'], 'new_prediction': prediction,
                    'old_correct': old[uid]['prediction'] == labels[uid] and not old[uid]['error'],
                    'new_correct': prediction == labels[uid] and bool(result) and not result.get('error'),
                    'old_online_seconds': old[uid]['online_seconds'],
                    'new_online_seconds': result.get('cost_summary', {}).get('online'),
                    'reading': result.get('diagnostics', {}).get('reading')})
        write(output/'paired_cases.json', paired)
        report['paired_cases'] = {'questions': len(paired),
            'source_correct': sum(r['old_correct'] for r in paired),
            'new_correct': sum(r['new_correct'] for r in paired),
            'fixed_wrong_answers': sum(not r['old_correct'] and r['new_correct'] for r in paired),
            'lost_correct_answers': sum(r['old_correct'] and not r['new_correct'] for r in paired)}
        for prefix in ('old', 'new'):
            values = [r[prefix+'_online_seconds'] for r in paired if r[prefix+'_online_seconds'] is not None]
            report['paired_cases']['mean_'+prefix+'_online_seconds'] = sum(values)/len(values) if values else None
    write(output/'run_summary.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--out', required=True)
    prep.add_argument('--previous', required=True)
    prep.add_argument('--shards', type=int, default=4)
    prep.add_argument('--streams-per-gpu', type=int, default=2)
    prep.add_argument('--cohort-size', type=int)
    prep.add_argument('--failure-code')
    prep.add_argument('--question-ids-file')
    prep.add_argument('--reading-policy', choices=['coverage_first', 'script_guided'])
    prep.add_argument('--build-probe-count', type=int, default=0)
    run = commands.add_parser('run-job')
    run.add_argument('--out', required=True)
    run.add_argument('--index', type=int, choices=(0, 1), required=True)
    check = commands.add_parser('status')
    check.add_argument('--out', required=True)
    summary = commands.add_parser('summarize')
    summary.add_argument('--out', required=True)
    probe = commands.add_parser('probe-build')
    probe.add_argument('--out', required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        ids = read(args.question_ids_file) if args.question_ids_file else None
        output = prepare(args.out, args.previous, args.shards, args.streams_per_gpu, args.cohort_size, args.failure_code, ids, args.reading_policy)
        if args.build_probe_count:
            prepare_build_probe(Path(output), args.build_probe_count)
        print(output)
    elif args.command == 'run-job':
        raise SystemExit(run_job(Path(args.out).resolve(), args.index))
    elif args.command == 'summarize':
        report = summarize(Path(args.out).resolve())
        print(json.dumps(report), flush=True)
        raise SystemExit(0 if report['passed'] else 2)
    elif args.command == 'probe-build':
        report = run_build_probe(Path(args.out).resolve())
        print(json.dumps(report), flush=True)
        raise SystemExit(0 if report['passed'] else 2)
    else:
        root = Path(args.out).resolve()
        print(json.dumps(inspect_results(root, range(read(root/'verification_spec.json')['shards'])), indent=2))
