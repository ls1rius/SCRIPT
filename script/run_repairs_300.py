"""Matched 300-question repair comparison with one fresh M0 shared by two arms."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'script'))
import run_pilot_comparison as pilot
from check_run_integrity import audit
from script_core.config import base_config, digest, load_config

ARMS = ('coverage_first', 'script_guided')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(output, previous, cohort_size=300, reuse_m0_from=None):
    import yaml
    output, previous = Path(output).resolve(), Path(previous).resolve()
    if output.exists():
        raise ValueError('Choose a new experiment directory')
    ids = pilot.read(previous/'cohorts.json')[str(cohort_size)]
    old_spec = pilot.read(previous/'pilot_spec.json')
    reference = pilot.read(previous/f'comparison_{cohort_size}.json')
    if len(ids) != cohort_size or len(set(ids)) != cohort_size or not reference['complete']:
        raise ValueError('Expected the complete, distinct fixed cohort')
    if {r['question_id'] for r in reference['rows']} != set(ids):
        raise ValueError('Reference predictions differ from the fixed cohort')
    spec = pilot.prepare(output, count=cohort_size, shards=4, streams_per_gpu=2,
        seed=old_spec['seed'], data_root=old_spec['data_root'], video_ids=ids,
        feature_batch_size=old_spec['feature_batch_size'], embedding_batch_size=old_spec['embedding_batch_size'])
    config = yaml.safe_load((output/'config.yaml').read_text())
    for arm in ARMS:
        arm_config = json.loads(json.dumps(config))
        arm_config['query'] = {'reading_policy': arm, 'max_script_reads': 3, 'script_context_windows': 12}
        directory = output/'arms'/arm
        directory.mkdir(parents=True)
        (directory/'config.yaml').write_text(yaml.safe_dump(arm_config, sort_keys=False))
        assert base_config(load_config(directory/'config.yaml')) == base_config(load_config(output/'config.yaml'))
    pilot.write(output/'evaluation_reference.json', reference)
    pilot.write(output/'cohort.json', ids)
    reused_hashes = {}
    if reuse_m0_from:
        reused = Path(reuse_m0_from).resolve()
        if pilot.read(reused/'cohort.json') != ids:
            raise ValueError('M0 reuse requires the exact same cohort and ordering')
        if base_config(load_config(reused/'config.yaml')) != base_config(load_config(output/'config.yaml')):
            raise ValueError('M0 reuse requires identical models and base configuration')
        for i in range(spec['shards']):
            old, new = reused/'shards'/str(i), output/'shards'/str(i)
            if (old/'questions.jsonl').read_bytes() != (new/'questions.jsonl').read_bytes():
                raise ValueError('M0 reuse requires identical per-shard questions')
            index = pilot.read(old/'script_base'/'videos.json')
            expected = {json.loads(line)['video_id'] for line in (new/'questions.jsonl').read_text().splitlines()}
            if set(index) != expected:
                raise ValueError('M0 reuse requires a complete base for every planned video')
            for path in [old/'script_base'/'videos.json', *[Path(p)/'base_hash.json' for p in index.values()]]:
                reused_hashes[str(path)] = sha(path)
            (new/'script_base').symlink_to((old/'script_base').resolve(), target_is_directory=True)
        previous_results = pilot.read(reused/'comparison.json')
        if not previous_results['complete'] or any(len(previous_results['rows'][a]) != cohort_size for a in ARMS):
            raise ValueError('Expected complete matched previous results')
        pilot.write(output/'previous_repair_reference.json', previous_results)
        pilot.write(output/'reuse_hashes.json', reused_hashes)
    # Extend the standard source snapshot with this orchestration, before submission.
    hashes = pilot.read(output/'source_hashes.json')
    for name in ('script/run_repairs_300.py', 'script/repairs_300.pbs'):
        target = output/'source'/name
        shutil.copyfile(ROOT/name, target)
        hashes[name] = sha(target)
    pilot.write(output/'source_hashes.json', hashes)
    spec.update(mode='matched_repair_reading_comparison', arms=list(ARMS),
        reference_experiment=str(previous), fresh_m0_videos=cohort_size, reused_m0_videos=0,
        shared_m0_across_arms=True, questions_per_arm=cohort_size,
        stage_order=['fresh_m0', *ARMS],
        constraints={'online_cap_seconds':600, 'global_task_limit':8, 'guided_max_reads':3,
                     'max_new_tokens':1024, 'maximum_concurrent_gpus':2},
        limitations=['Fixed development cohort; not held-out accuracy.',
                     'Both arms share newly built M0. Historical V7c uses its historical M0.',
                     'Original VideoTree predictions/times are reused; VideoTree is not rerun.',
                     'M0 is built once; do not sum its cost across both arms.'])
    if reuse_m0_from:
        spec.update(reuse_m0_from=str(reused), fresh_m0_videos=0, reused_m0_videos=cohort_size,
                    stage_order=list(ARMS))
        spec['limitations'][1] = 'Both arms reuse the same M0 as the previous matched run. Historical V7c uses its historical M0.'
        spec['limitations'].append('Reuse the same complete M0 as the previous matched run; build costs are historical, not remeasured.')
    spec['query_repairs'] = {name: load_config(output/'arms'/ARMS[0]/'config.yaml')['query'][name]
                              for name in ('answer_strategy', 'answer_order', 'caption_policy')}
    pilot.write(output/'pilot_spec.json', spec)
    inputs = ['config.yaml', 'pilot_spec.json', 'cohort.json', 'evaluation_only.json', 'evaluation_reference.json']
    inputs += [f'arms/{arm}/config.yaml' for arm in ARMS]
    inputs += [str(p.relative_to(output)) for p in output.glob('captions/*.json')]
    inputs += [f'shards/{i}/{name}' for i in range(4) for name in ('manifest.json', 'questions.jsonl')]
    if reuse_m0_from:
        inputs += ['previous_repair_reference.json', 'reuse_hashes.json']
    pilot.write(output/'input_hashes.json', {name:sha(output/name) for name in inputs})
    score(output)
    return spec


def verify(output):
    pilot.verify_snapshot(output)
    for name, expected in pilot.read(output/'input_hashes.json').items():
        if sha(output/name) != expected:
            raise ValueError('Frozen experiment input changed: '+name)


def mean(values):
    values = [v for v in values if isinstance(v, (float, int)) and not isinstance(v, bool)]
    return statistics.mean(values) if values else None


def summarize_rows(rows):
    done = [r for r in rows if r['completed']]
    return {'planned':len(rows), 'completed':len(done), 'correct':sum(r['correct'] for r in done),
        'errors':sum(bool(r['error']) for r in done),
        'accuracy_on_completed':sum(r['correct'] for r in done)/len(done) if done else None,
        'accuracy':sum(r['correct'] for r in done)/len(rows) if len(done)==len(rows) else None,
        'mean_online_seconds':mean([r.get('online_seconds') for r in done]),
        'mean_wall_seconds':mean([r.get('wall_seconds') for r in done]),
        'mean_total_cost_seconds':mean([r.get('total_cost_seconds') for r in done]),
        'mean_visual_reads':mean([r.get('visual_reads') for r in done]),
        'budget_violations':sum(r.get('budget_violation', False) for r in done)}


def score(output):
    output = Path(output)
    with (output/'score.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        spec = pilot.read(output/'pilot_spec.json')
        reference = pilot.read(output/'evaluation_reference.json')
        old = {r['question_id']:r for r in reference['rows']}
        rows = {arm:[] for arm in ARMS}
        builds = []
        for i in range(spec['shards']):
            directory = output/'shards'/str(i)
            errors = pilot.read(directory/'script_build_errors.json') if (directory/'script_build_errors.json').exists() else {}
            for line in (directory/'questions.jsonl').read_text().splitlines():
                q = json.loads(line)
                uid, key = q['question_id'], digest(q['question_id'])[:20]
                base = directory/'script_base'/digest(q['video_id'])[:20]
                if (base/'base_hash.json').exists():
                    cost = pilot.read(base/'build_cost.json')
                    summaries = [json.loads(s) for s in (base/'summaries.jsonl').read_text().splitlines()]
                    builds.append({'video_id':q['video_id'], 'completed':True, 'error':None,
                        'seconds':cost.get('total', cost.get('online')), 'by_operation':cost.get('by_operation', {}),
                        'summary_count':len(summaries), 'unreadable_summaries':sum(s['status']!='current' for s in summaries)})
                elif q['video_id'] in errors:
                    builds.append({'video_id':q['video_id'], 'completed':True, 'error':errors[q['video_id']]})
                else:
                    builds.append({'video_id':q['video_id'], 'completed':False, 'error':None})
                truth = str(old[uid]['truth'])
                for arm in ARMS:
                    runs = output/'arms'/arm/'shards'/str(i)/'runs'
                    path = runs/key/'result.json'
                    failure = runs/'query_failures'/(key+'.json')
                    result = pilot.read(failure if failure.exists() else path) if failure.exists() or path.exists() else None
                    error = result.get('error') if result is not None else errors.get(q['video_id'])
                    answer = (result.get('answer') or {}) if result else {}
                    costs = result.get('cost_summary', {}) if result else {}
                    reading = (result.get('diagnostics', {}).get('reading') or {}) if result else {}
                    rows[arm].append({'question_id':uid, 'truth':truth, 'completed':result is not None or bool(error),
                        'prediction':answer.get('selected_option'), 'error':error,
                        'correct':answer.get('selected_option')==truth and not error,
                        'online_seconds':costs.get('online'), 'wall_seconds':result.get('wall_seconds') if result else None,
                        'total_cost_seconds':costs.get('total'), 'visual_reads':reading.get('visual_reads'),
                        'guided_active':reading.get('script_guided_active'),
                        'budget_violation':costs.get('budget_violation', False),
                        'historical_script_correct':old[uid]['SCRIPT']['correct'],
                        'videotree_correct':old[uid]['VideoTree']['correct']})
        groups = {arm:summarize_rows(values) for arm, values in rows.items()}
        inference_complete = all(g['completed']==g['planned'] for g in groups.values())
        integrity = {f'{arm}/{i}':pilot.read(path) for arm in ARMS for i in range(spec['shards'])
                     if (path := output/'arms'/arm/'shards'/str(i)/'integrity_report.json').exists()}
        complete = (inference_complete and all(b['completed'] for b in builds)
                    and len(integrity)==len(ARMS)*spec['shards'])
        clean = (not any(g['errors'] or g['budget_violations'] for g in groups.values())
                 and all(a['passed'] for a in integrity.values()))
        for arm, values in rows.items():
            done = [r for r in values if r['completed']]
            groups[arm].update(fixed_wrong_answers=sum(not r['historical_script_correct'] and r['correct'] for r in done),
                lost_correct_answers=sum(r['historical_script_correct'] and not r['correct'] for r in done))
        pairs = [(a,b) for a,b in zip(rows[ARMS[0]], rows[ARMS[1]]) if a['completed'] and b['completed']]
        build_summary = {'planned':len(builds), 'completed':sum(b['completed'] for b in builds),
            'built':sum(b['completed'] and not b['error'] for b in builds),
            'errors':sum(bool(b['error']) for b in builds), 'mean_seconds':mean([b.get('seconds') for b in builds]),
            'summary_count':sum(b.get('summary_count', 0) for b in builds),
            'unreadable_summaries':sum(b.get('unreadable_summaries', 0) for b in builds)}
        ops = {key for b in builds for key in b.get('by_operation', {})}
        built = [b for b in builds if 'seconds' in b]
        build_summary['operation_seconds_per_built_video'] = {key:mean([b['by_operation'].get(key,0.) for b in built]) for key in sorted(ops)}
        clean = clean and not build_summary['errors'] and not build_summary['unreadable_summaries']
        report = {'updated_at':pilot.now(), 'complete':complete, 'inference_complete':inference_complete,
            'status':('complete' if clean else 'complete_with_errors') if complete else 'running_or_queued',
            'integrity':integrity,
            'groups':groups, 'fresh_m0':build_summary, 'historical_baseline':reference['groups'],
            'paired_guided_vs_coverage':{'completed_pairs':len(pairs),
                'gained':sum(not a['correct'] and b['correct'] for a,b in pairs),
                'lost':sum(a['correct'] and not b['correct'] for a,b in pairs)},
            'limitations':spec['limitations']}
        report['m0_cost_remeasured'] = not bool(spec.get('reuse_m0_from'))
        report['fresh_m0'].update(newly_built=0 if spec.get('reuse_m0_from') else build_summary['built'],
                                  reused=build_summary['built'] if spec.get('reuse_m0_from') else 0)
        if (output/'previous_repair_reference.json').exists():
            prior = pilot.read(output/'previous_repair_reference.json')
            report['paired_vs_previous_repair'] = {}
            for arm, values in rows.items():
                old_arm = {r['question_id']:r for r in prior['rows'][arm]}
                done = [r for r in values if r['completed']]
                report['paired_vs_previous_repair'][arm] = {
                    'completed_pairs':len(done),
                    'fixed_wrong_answers':sum(not old_arm[r['question_id']]['correct'] and r['correct'] for r in done),
                    'lost_correct_answers':sum(old_arm[r['question_id']]['correct'] and not r['correct'] for r in done),
                    'previous_correct':prior['groups'][arm]['correct'],
                    'previous_errors':prior['groups'][arm]['errors']}
        pilot.write(output/'comparison.json', dict(report, rows=rows))
        pilot.write(output/'run_summary.json', report)
        return report


def command(output, directory, phase, arguments):
    state = {'phase':phase, 'started_at':pilot.now(), 'command':list(map(str, arguments))}
    pilot.write(directory/'phase_status.json', state)
    with (directory/(phase+'.log')).open('a') as log:
        process = subprocess.run(list(map(str, arguments)), cwd=output/'source', stdout=log, stderr=subprocess.STDOUT)
    state.update(returncode=process.returncode, ended_at=pilot.now())
    pilot.write(directory/(phase+'-status.json'), state)
    return process.returncode


def run_shard(output, shard):
    output = Path(output).resolve()
    verify(output)
    spec = pilot.read(output/'pilot_spec.json')
    if not 0 <= shard < spec['shards']:
        raise ValueError('Unknown shard')
    directory = output/'shards'/str(shard)
    with (directory/'pipeline.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        index = (pilot.read(directory/'script_base'/'videos.json') if spec.get('reuse_m0_from')
                 else pilot.build_script(output, directory, spec))
        questions = [json.loads(line) for line in (directory/'questions.jsonl').read_text().splitlines()]
        accepted = [q for q in questions if q['video_id'] in index]
        path = directory/'built_questions.jsonl'
        path.write_text(''.join(json.dumps(q,ensure_ascii=False)+'\n' for q in accepted))
        score(output)
        codes, audits = {}, {}
        for arm in ARMS:
            runs = output/'arms'/arm/'shards'/str(shard)/'runs'
            codes[arm] = command(output, directory, arm, [sys.executable, output/'source/script_qa.py', 'query',
                '--base',directory/'script_base', '--questions',path, '--config',output/'arms'/arm/'config.yaml',
                '--out',runs, '--resume']) if accepted else 0
            verdict = audit(runs)
            verdict.update(expected_queries=len(accepted), passed=verdict['passed'] and verdict['queries']==len(accepted))
            audits[arm] = verdict['passed']
            pilot.write(runs.parent/'integrity_report.json', verdict)
            score(output)
        pilot.write(directory/'pipeline_status.json', {'status':'finished', 'ended_at':pilot.now(), 'returncodes':codes,
            'build_errors':len(questions)-len(accepted)})
        return int(any(codes.values()) or not all(audits.values()) or len(accepted)!=len(questions))


def run_job(output, index):
    output = Path(output).resolve()
    verify(output)
    spec = pilot.read(output/'pilot_spec.json')
    if not 0 <= index < spec['gpu_jobs']:
        raise ValueError('Unknown GPU job')
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count()!=1:
        raise RuntimeError('Each GPU job requires exactly one visible CUDA device')
    shards = list(range(index, spec['shards'], spec['gpu_jobs']))
    state = {'job_id':os.environ.get('PBS_JOBID'), 'shards':shards, 'status':'running', 'started_at':pilot.now()}
    pilot.write(output/'jobs'/f'job-{index}-status.json', state)
    with ExitStack() as stack:
        processes = {}
        for shard in shards:
            log = stack.enter_context((output/'shards'/str(shard)/'pipeline.log').open('a'))
            processes[shard] = subprocess.Popen([sys.executable, str(output/'source/script/run_repairs_300.py'),
                'run-shard', '--out',str(output), '--shard',str(shard)], cwd=output/'source', stdout=log, stderr=subprocess.STDOUT)
        codes = {shard:process.wait() for shard,process in processes.items()}
    state.update(status='finished', ended_at=pilot.now(), returncodes=codes)
    pilot.write(output/'jobs'/f'job-{index}-status.json', state)
    score(output)
    return int(any(codes.values()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--out', required=True)
    prep.add_argument('--previous', required=True)
    prep.add_argument('--reuse-m0-from', help='Reuse the identical complete M0 of a matched repair run')
    run = commands.add_parser('run-shard')
    run.add_argument('--out', required=True)
    run.add_argument('--shard', type=int, required=True)
    job = commands.add_parser('run-job')
    job.add_argument('--out', required=True)
    job.add_argument('--index', type=int, required=True)
    scoring = commands.add_parser('score')
    scoring.add_argument('--out', required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        result = prepare(args.out, args.previous, reuse_m0_from=args.reuse_m0_from)
        print(json.dumps({'questions_per_arm':result['questions_per_arm'], 'gpu_jobs':result['gpu_jobs']}))
    elif args.command == 'run-shard':
        raise SystemExit(run_shard(args.out,args.shard))
    elif args.command == 'run-job':
        raise SystemExit(run_job(args.out,args.index))
    else:
        print(json.dumps(score(args.out)))
