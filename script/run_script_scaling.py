"""Extend the frozen V7c 100 questions to the existing VideoTree 300/500 cohorts."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'script'))
import run_pilot_comparison as pilot
from check_run_integrity import audit


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(output, previous, baseline):
    output, previous, baseline = map(lambda p: Path(p).resolve(), (output, previous, baseline))
    cohorts = pilot.read(baseline/'cohorts.json')
    old = pilot.read(previous/'pilot_spec.json')
    if ([len(cohorts[str(n)]) for n in (100, 300, 500)] != [100, 300, 500]
            or cohorts['100'] != old['video_ids'] or cohorts['500'][:300] != cohorts['300']
            or cohorts['300'][:100] != cohorts['100']):
        raise ValueError('Expected the unchanged nested 100/300/500 cohorts')
    pilot.verify_snapshot(previous)
    old_hashes = pilot.read(previous/'source_hashes.json')
    for name in ['script_qa.py', *[str(p.relative_to(ROOT)) for p in (ROOT/'script_core').glob('*.py')]]:
        if sha(ROOT/name) != old_hashes.get(name):
            raise ValueError('SCRIPT inference differs from completed 100: '+name)
    baseline_spec = pilot.read(baseline/'scaling_spec.json')
    if baseline_spec['model'] != old['model'] or sha(Path(old['data_root'])/'lavila_subset.json') != old['shared_caption_sha256']:
        raise ValueError('Baseline weights or captions differ')
    original = {p.parent.name: p.parent for p in previous.glob('shards/*/script_runs/*/result.json')}
    from script_qa import safe_directory_id
    reuse_hashes = {}
    for uid in cohorts['100']:
        directory = original[safe_directory_id(uid)]
        result = pilot.read(directory/'result.json')
        if result.get('error') or not result.get('answer'):
            raise ValueError('Incomplete original SCRIPT answer: '+uid)
        # Freeze predictions and semantic provenance. Pixel caches remain in the
        # original immutable run; avoid rereading them in every GPU worker.
        for path in directory.rglob('*'):
            if path.is_file() and path.suffix in ('.json', '.jsonl'):
                reuse_hashes[str(path.resolve())] = sha(path)
    for uid in cohorts['500']:
        directory = baseline/'videotree_runs'/uid
        artifacts = [directory/n for n in ('summary.json', 'failure.json') if (directory/n).exists()]
        if not artifacts:
            raise ValueError('Missing baseline outcome: '+uid)
        for path in artifacts:
            reuse_hashes[str(path.resolve())] = sha(path)
    spec = pilot.prepare(output, count=500, shards=4, seed=old['seed'], data_root=old['data_root'],
                         feature_batch_size=old['feature_batch_size'], embedding_batch_size=old['embedding_batch_size'],
                         streams_per_gpu=2, video_ids=cohorts['500'])
    if (output/'config.yaml').read_bytes() != (previous/'config.yaml').read_bytes():
        raise ValueError('Scaling configuration differs from the completed 100')
    pilot.write(output/'cohorts.json', cohorts)
    spec.update(reused_script_questions=100, new_script_questions=400, previous_script_from=str(previous),
                reuse_videotree_from=str(baseline), cohort_source=str(baseline/'cohorts.json'),
                stage_order=[300, 500])
    spec['cost_limitations'] += ['Original V7c 100 SCRIPT results and all existing VideoTree outcomes are reused with their historical times.',
                               'VideoTree failures remain failures and count as incorrect; previously debugged IDs present in the public subset are included and recorded.',
                               'The nested cohorts overlap and include the development 100; they are not independent held-out trials.']
    pilot.write(output/'pilot_spec.json', spec)
    for shard in range(4):
        directory = output/'shards'/str(shard)
        (directory/'script_runs').mkdir()
        (directory/'videotree_runs').mkdir()
        for row in pilot.read(directory/'manifest.json'):
            uid = row['video_id']
            (directory/'videotree_runs'/uid).symlink_to((baseline/'videotree_runs'/uid).resolve(), target_is_directory=True)
            if uid in cohorts['100']:
                (directory/'script_runs'/safe_directory_id(uid)).symlink_to(original[safe_directory_id(uid)].resolve(), target_is_directory=True)
    pilot.write(output/'reuse_hashes.json', reuse_hashes)
    names = ['config.yaml', 'pilot_spec.json', 'cohorts.json', 'evaluation_only.json']
    names += [str(p.relative_to(output)) for p in (output/'captions').glob('*.json')]
    names += [f'shards/{i}/{name}' for i in range(4) for name in ('manifest.json', 'questions.jsonl', 'videotree_input.json')]
    pilot.write(output/'input_hashes.json', {name: sha(output/name) for name in names})
    score(output)
    return spec


def verify(output):
    pilot.verify_snapshot(output)
    for name, expected in pilot.read(output/'input_hashes.json').items():
        if sha(output/name) != expected:
            raise ValueError('Frozen scaling input changed: '+name)


def metrics(rows, method):
    finished = [r[method] for r in rows if r[method] is not None]
    seconds = [r['wall_seconds'] for r in finished if isinstance(r.get('wall_seconds'), (int, float))]
    correct = sum(r['correct'] for r in finished)
    return dict(planned_questions=len(rows), completed_questions=len(finished), correct=correct,
                errors=sum(bool(r.get('error')) for r in finished),
                accuracy_on_completed=correct/len(finished) if finished else None,
                final_accuracy=correct/len(rows) if len(finished) == len(rows) else None,
                mean_wall_seconds=statistics.mean(seconds) if seconds else None,
                median_wall_seconds=statistics.median(seconds) if seconds else None,
                **{key: sum(r.get('diagnostics', {}).get(key, 0) for r in finished)
                   for key in ('internal_tool_errors', 'hard_timeouts', 'soft_deadline_stops')},
                budget_violations=sum(bool(r.get('cost', {}).get('budget_violation')) for r in finished))


def score(output):
    output = Path(output)
    # Serialize the full snapshot and derived cohort reports together.
    with (output/'scaling_summary.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        comparison = pilot.score(output)
        cohorts = pilot.read(output/'cohorts.json')
        groups = {}
        from script_core.evaluation import paired_cluster_bootstrap
        for size, ids in cohorts.items():
            selected = set(ids)
            rows = [r for r in comparison['rows'] if r['question_id'] in selected]
            group = {method: metrics(rows, method) for method in ('SCRIPT', 'VideoTree')}
            paired = [{'question_id': r['question_id'], 'cluster_id': r['question_id'],
                       'method': method, 'correct': r[method]['correct']}
                      for r in rows if all(r[m] is not None for m in group) for method in group]
            report = dict(updated_at=pilot.now(), complete=all(g['final_accuracy'] is not None for g in group.values()),
                          groups=group, paired_accuracy_bootstrap=paired_cluster_bootstrap(paired, 'SCRIPT', 'VideoTree'),
                          rows=rows, cost_limitations=comparison['cost_limitations'])
            pilot.write(output/f'comparison_{size}.json', report)
            groups[size] = {k: v for k, v in report.items() if k != 'rows'}
        audits = {str(i): pilot.read(p) for i in range(4)
                  if (p := output/'shards'/str(i)/'integrity_report.json').exists()}
        audited = len(audits) == 4 and all(a['passed'] for a in audits.values())
        status = 'complete' if comparison['complete'] and audited else 'complete_integrity_failed' if comparison['complete'] and len(audits) == 4 else 'running'
        summary = dict(updated_at=pilot.now(), status=status, inference_complete=comparison['complete'],
                       groups=groups, integrity=audits, reused_script_questions=100, new_script_questions=400)
        pilot.write(output/'run_summary.json', summary)
        return summary


def run_shard(output, shard):
    output = Path(output).resolve()
    verify(output)
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('One allocated GPU is required')
    spec, cohorts = pilot.read(output/'pilot_spec.json'), pilot.read(output/'cohorts.json')
    if not 0 <= shard < spec['shards']:
        raise ValueError('Invalid shard')
    directory = output/'shards'/str(shard)
    questions = [json.loads(s) for s in (directory/'questions.jsonl').read_text().splitlines()]
    manifest = pilot.read(directory/'manifest.json')
    failed = False
    errors = {}
    with (directory/'run.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for size, already in (('300', '100'), ('500', '300')):
            ids = set(cohorts[size])-set(cohorts[already])
            stage = directory/'stages'/size
            stage.mkdir(parents=True, exist_ok=True)
            pilot.write(directory/'status.json', {'phase': 'build', 'cohort': size, 'updated_at': pilot.now()})
            pilot.write(stage/'manifest.json', [r for r in manifest if r['video_id'] in ids])
            index = pilot.build_script(output, stage, spec)
            errors.update(pilot.read(stage/'script_build_errors.json'))
            pilot.write(directory/'script_build_errors.json', errors)
            accepted = [q for q in questions if q['video_id'] in index]
            query_path = stage/'questions.jsonl'
            query_path.write_text(''.join(json.dumps(q, ensure_ascii=False)+'\n' for q in accepted))
            pilot.write(directory/'status.json', {'phase': 'query', 'cohort': size, 'questions': len(accepted), 'updated_at': pilot.now()})
            rc = pilot.command(stage, 'script_query', [ROOT/'script_qa.py', 'query', '--base', stage/'script_base',
                '--questions', query_path, '--config', output/'config.yaml', '--out', directory/'script_runs', '--resume']) if accepted else 0
            failed |= bool(rc or errors)
            score(output)
        report = audit(directory/'script_runs')
        report['expected_queries'] = len(questions)
        if report['queries'] != len(questions):
            report['passed'] = False
            report['failures'].append({'reason': 'Missing result artifacts', 'expected': len(questions), 'actual': report['queries']})
        pilot.write(directory/'integrity_report.json', report)
        pilot.write(directory/'status.json', {'phase': 'finished', 'ended_at': pilot.now(), 'failed': failed or not report['passed']})
        score(output)
    return int(failed or not report['passed'])


def run_job(output, index):
    output = Path(output).resolve()
    verify(output)
    import torch
    if not 0 <= index < 2 or not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('Two jobs, each with one allocated GPU, are required')
    if torch.cuda.get_device_properties(0).total_memory < 120*1024**3:
        raise RuntimeError('Two concurrent workers require at least 120 GiB GPU memory')
    state = dict(job_id=os.environ.get('PBS_JOBID'), job_index=index, node=socket.gethostname(),
                 gpu=torch.cuda.get_device_name(0), shards=list(range(index, 4, 2)), status='running', started_at=pilot.now())
    pilot.write(output/'jobs'/f'job-{index}-status.json', state)
    with ExitStack() as stack:
        processes = []
        for shard in state['shards']:
            log = stack.enter_context((output/'shards'/str(shard)/'worker.log').open('a', buffering=1))
            processes.append((shard, subprocess.Popen([sys.executable, str(ROOT/'script/run_script_scaling.py'),
                'run', '--out', str(output), '--index', str(shard)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)))
        for shard, process in processes:
            state.setdefault('returncodes', {})[str(shard)] = process.wait()
            pilot.write(output/'jobs'/f'job-{index}-status.json', state)
    state.update(status='finished', ended_at=pilot.now())
    pilot.write(output/'jobs'/f'job-{index}-status.json', state)
    score(output)
    return int(any(state['returncodes'].values()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run', 'run-job', 'score'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--previous')
    parser.add_argument('--baseline')
    parser.add_argument('--index', type=int, default=0)
    args = parser.parse_args()
    if args.action == 'prepare':
        print(json.dumps(prepare(args.out, args.previous, args.baseline), indent=2))
    elif args.action == 'score':
        print(json.dumps(score(args.out), indent=2))
    else:
        sys.exit(run_job(args.out, args.index) if args.action == 'run-job' else run_shard(args.out, args.index))
