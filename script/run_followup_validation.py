"""Frozen 300-question follow-ups, with four-GPU admission and paired reports."""
import argparse
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import statistics
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.run_adaptive_comparison import now, read, sha, verify as verify_prior
from script_core.state import write_json as write

ARMS = ('partial_direct', 'optimized_scheme4', 'adaptive_scheme4')


def direct_input(original):
    """Remove exactly the saved memory JSON, preserving captions/visual text."""
    result = deepcopy(original)
    if len(result['prompts']) != 1:
        raise ValueError('Expected one final QA prompt')
    prompt = result['prompts'][0]
    marker = '\n\nTemporal entity memory ('
    start = prompt.index(marker)
    json_start = prompt.index('):\n', start) + 3
    memory, end = json.JSONDecoder().raw_decode(prompt[json_start:])
    if not isinstance(memory, dict):
        raise ValueError('Missing saved memory object')
    suffix = prompt[json_start+end:]
    if suffix and not suffix.startswith('\n\nSupplemental sampled visual observations'):
        raise ValueError('Unexpected content after saved memory')
    result['prompts'][0] = prompt[:start] + suffix
    return result


def verify(out):
    for name, expected in read(out/'frozen_hashes.json').items():
        if sha(out/name) != expected:
            raise ValueError('Frozen input/source changed: ' + name)


def snapshot(source, target):
    for folder in ('script_core', 'script', 'tests', 'configs', 'kmeans_pytorch', 'data_extraction'):
        for p in (source/folder).rglob('*'):
            if p.is_file() and p.suffix in ('.py', '.yaml', '.json') and '__pycache__' not in p.parts:
                dest = target/p.relative_to(source)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
    for p in source.glob('*.py'):
        shutil.copy2(p, target/p.name)
    for name in ('data', 'checkpoints'):
        (target/name).symlink_to((ROOT/name).resolve(), target_is_directory=True)


def prepare(prior, out, repairs_only=False, minimum_free_mib=48*1024):
    if out.exists():
        raise ValueError('Use a fresh experiment directory')
    verify_prior(prior)
    ids = read(prior/'cohort.json')
    if len(ids) != 300 or len(set(ids)) != 300:
        raise ValueError('Require the complete distinct 300-question cohort')
    out.mkdir(parents=True)
    snapshot(ROOT, out/'source')
    snapshot(prior/'source', out/'source_original')
    shutil.copy2(Path(__file__), out/'source_original/script'/Path(__file__).name)
    for name in ('cohort.json', 'questions.json', 'captions.json', 'evaluation_only.json',
                 'config.json', 'settings.json', 'input_hashes.json'):
        shutil.copy2(prior/name, out/name)
    shutil.copytree(prior/'inputs', out/'inputs')
    controls = {a: {} for a in ('adaptive_partial', 'adaptive_scheme4', 'optimized_scheme4', 'summary_deep')}
    for uid in ids:
        shutil.copy2(prior/'formal/directories'/uid/'directory.json',
                     _parent(out/'directories'/uid/'directory.json'))
        for arm in controls:
            path = prior/'formal/arms'/arm/'runs'/uid/'result.json'
            r = read(path)
            controls[arm][uid] = {k: r.get(k) for k in ('answer', 'error', 'incomplete',
                'coverage_check', 'total_measured_seconds', 'selected_seconds', 'stop_reason')}
            if r.get('answer'):
                controls[arm][uid]['answer'] = dict(selected_option=r['answer']['selected_option'])
            controls[arm][uid]['source_sha256'] = sha(path)
        if repairs_only:
            continue
        target = prior/'formal/arms/adaptive_partial/runs'/uid
        calls = [json.loads(line) for line in (target/'calls.jsonl').read_text().splitlines()]
        qa = [c for c in calls if c['operation'] == 'videotree_qa']
        if len(qa) != 1 or qa[0]['error']:
            raise ValueError('Partial ablation requires one saved successful final QA')
        inp = direct_input(qa[0]['parameters'])
        write(out/'ablation_inputs'/f'{uid}.json', dict(inputs=inp,
            original_input_hash=qa[0]['input_hash'], source_call_id=qa[0]['call_id'],
            selected_seconds=controls['adaptive_partial'][uid]['selected_seconds'],
            policy='Only saved memory JSON removed; all original description and visual supplement bytes retained'))
    write(out/'controls.json', controls)
    settings = read(out/'settings.json')
    if not repairs_only:
        settings.update(adaptive_bounded_policy=True, adaptive_initial_frames=6,
                        adaptive_expand_frames=8, adaptive_max_steps=8, adaptive_max_read_streak=2)
    write(out/'settings.json', settings)
    cfg = read(out/'config.json')
    cfg['cache']['persistent_directory'] = str(out/'cache/text_tools')
    cfg['compatibility']['cache_directory'] = ''
    write(out/'config.json', cfg)
    jobs = []
    # Regression prefixes are selected by failure/status only, never correctness.
    arms = ('adaptive_scheme4', 'optimized_scheme4') if repairs_only else ARMS
    for arm in arms:
        gate = [] if arm == 'partial_direct' else [u for u in ids if controls[arm][u]['error']]
        if arm != 'partial_direct':
            gate += [u for u in ids if u not in gate][:5]
            jobs.append(dict(id=arm+'_gate', arm=arm, ids=gate, gate=True, depends=[]))
        remaining = [u for u in ids if u not in gate]
        for i in range(4):
            jobs.append(dict(id=f'{arm}_{i}', arm=arm, ids=remaining[i::4], gate=False,
                             depends=[arm+'_gate'] if gate else []))
    write(out/'jobs.json', jobs)
    write(out/'experiment.json', dict(created_at=now(), prior=str(prior), examples=300, arms=arms,
        repairs_only=repairs_only,
        navigation_policy='Unchanged from prior settings' if repairs_only else 'Bounded adaptive navigation',
        comparison='paired development cohort; no held-out generalization claim',
        partial_direct='Frozen original inference code; only memory removed from saved final prompt',
        source_reuse='Existing immutable descriptions, frame features and directories only; no cross-arm extraction cache',
        regression='Failure cases plus first five previously successful cases; pass requires answers, no errors or budget violation',
        formal='Regression prefix and remaining questions each executed once under the same frozen new version; all 300 count',
        timing='Directory source cost attributed to each full arm; direct ablation is incremental QA cost only',
        scheduling=dict(allowed_gpus=['0','1','2','3'], minimum_free_mib=minimum_free_mib,
            stable_observations=3, poll_seconds=30, max_workers=4, no_automatic_failed_job_retries=True)))
    paths = [p for p in out.rglob('*') if p.is_file() and not any(
        x in ('data', 'checkpoints', '__pycache__') for x in p.relative_to(out).parts)]
    write(out/'frozen_hashes.json', {str(p.relative_to(out)): sha(p) for p in paths})
    verify(out)
    write(out/'queue_status.json', dict(state='prepared', updated_at=now()))


def _parent(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def resource_error(result):
    error = result.get('error')
    message = str(error or '').lower()
    return bool(error) and ('cuda out of memory' in message or 'outofmemoryerror' in message)


def stop_on_resource_error(out, job, result, completed):
    if resource_error(result):
        write(out/'workers'/job['id']/'resource_stop.json', dict(updated_at=now(),
            reason='CUDA memory exhausted; stop this worker before attempting another question',
            completed=completed, question_id=result['question_id'], error=result['error'],
            result_retained=True, automatic_retry=False))
        raise RuntimeError('GPU memory exhausted; failed result retained, remaining questions not attempted')


def summarize(out):
    ids, truth, control = read(out/'cohort.json'), read(out/'evaluation_only.json'), read(out/'controls.json')
    def correct(r, uid):
        return not r.get('error') and str((r.get('answer') or {}).get('selected_option')) == str(truth[uid])
    report = {}
    for arm in read(out/'experiment.json').get('arms', ARMS):
        rows = {u: r for u in ids if (p := out/'formal/arms'/arm/'runs'/u/'result.json').exists()
                and 'total_measured_seconds' in (r := read(p))}
        base = control['adaptive_partial' if arm == 'partial_direct' else arm]
        report[arm] = dict(completed=len(rows), planned=300,
            correct=sum(correct(r,u) for u,r in rows.items()),
            errors=sum(bool(r.get('error')) for r in rows.values()),
            incomplete=sum(bool(r.get('incomplete')) for r in rows.values()),
            mean_seconds=statistics.mean(r['total_measured_seconds'] for r in rows.values()) if rows else None,
            mean_plans=statistics.mean(r.get('planning_calls',len(r.get('reviews',[]))) for r in rows.values()) if rows else None,
            coverage_unmet=sum(r.get('coverage_check',{}).get('met') is False for r in rows.values()),
            prior_correct_same_ids=sum(correct(base[u],u) for u in rows),
            prior_errors_same_ids=sum(bool(base[u].get('error')) for u in rows),
            prior_mean_seconds_same_ids=statistics.mean(base[u]['total_measured_seconds'] for u in rows) if rows else None,
            median_seconds=statistics.median(r['total_measured_seconds'] for r in rows.values()) if rows else None,
            corrected=[u for u in rows if correct(rows[u],u) and not correct(base[u],u)],
            regressed=[u for u in rows if correct(base[u],u) and not correct(rows[u],u)])
    write(out/'report.json', dict(updated_at=now(), arms=report,
        denominator='300 per arm, errors count as incorrect; pending shown separately',
        direct_ablation_timing='Incremental QA only; full arms include attributed directory cost'))


def worker(out, job_id):
    verify(out)
    from script_core.backend import preflight
    from script_core.budget import BudgetLedger, Meter, ModelWorker
    from script_core.config import load_config
    from script.run_temporal_entity import checked_packet
    job = next(j for j in read(out/'jobs.json') if j['id'] == job_id)
    cfg, artifacts = preflight(load_config(out/'config.json'))
    prior_artifacts = read(Path(read(out/'experiment.json')['prior'])/'artifacts.json')
    for kind in ('visual_encoder','detector','vlm','text_embedding'):
        if artifacts[kind]['hash'] != prior_artifacts[kind]['hash']:
            raise ValueError('Model files changed: '+kind)
    if artifacts['software'] != prior_artifacts['software']:
        raise ValueError('Model software changed since control run')
    write(out/'workers'/job_id/'artifacts.json', artifacts)
    backend = ModelWorker(cfg)
    try:
        setup = out/'workers'/job_id/'setup'
        setup_ledger = BudgetLedger(600, answer_reserve=0, calls_path=setup/'calls.jsonl')
        setup_meter = Meter(backend, setup_ledger, cfg, setup/'raw_calls')
        try:
            setup_meter.call('prepare_vlm', {}, build=True)
            if job['arm'] != 'partial_direct':
                setup_meter.call('clip_text', dict(texts=['video']), build=True)
        finally:
            write(setup/'cost_summary.json', setup_ledger.summary())
            setup_meter.close()
        settings = read(out/'settings.json')
        for i, uid in enumerate(job['ids']):
            target = out/'formal/arms'/job['arm']/'runs'/uid
            if target.exists():
                raise ValueError('Attempt already exists; no automatic replay of spent work')
            write(out/'workers'/job_id/'progress.json', dict(question_id=uid, completed=i,
                planned=len(job['ids']), arm=job['arm'], updated_at=now()))
            if job['arm'] == 'partial_direct':
                saved = read(out/'ablation_inputs'/f'{uid}.json')
                ledger = BudgetLedger(120, answer_reserve=30, calls_path=target/'calls.jsonl')
                meter = Meter(backend, ledger, cfg, target/'raw_calls')
                result = dict(answer=None, error=None, selected_seconds=saved['selected_seconds'],
                              original_input_hash=saved['original_input_hash'], evaluation_only=True)
                try:
                    meter.call('temporal_qa_check', saved['inputs'], final=True)
                    answer, _ = meter.call('videotree_qa', saved['inputs'], final=True)
                    result['answer'] = answer['data']
                except Exception as exc:
                    result['error'] = dict(type=type(exc).__name__, message=str(exc))
                finally:
                    result.update(cost_summary=ledger.summary(), total_measured_seconds=ledger.summary()['total'])
                    meter.close()
            else:
                from script_core import adaptive_entity, optimized_entity
                packet = checked_packet(out, uid, check_images=True)
                directory = read(out/'directories'/uid/'directory.json')
                fn = adaptive_entity.run if job['arm'] == 'adaptive_scheme4' else optimized_entity.run
                result = fn(packet, directory, packet['frames'], backend, cfg, target, settings,
                    memory_path=out/'memory'/job['arm']/f'{uid}.json')
                result['directory_cost_summary'] = directory['cost_summary']
                result['total_measured_seconds'] = directory['cost_summary'].get('budgeted_total', directory['cost_summary']['total']) + result['cost_summary'].get('budgeted_total',result['cost_summary']['total'])
            result.update(question_id=uid, arm=job['arm'], job_id=job_id)
            write(target/'result.json', result)
            stop_on_resource_error(out, job, result, i+1)
        passed = all(not (r := read(out/'formal/arms'/job['arm']/'runs'/uid/'result.json')).get('error')
            and r.get('answer') and not r['cost_summary']['budget_violation'] for uid in job['ids'])
        write(out/'workers'/job_id/'completion.json', dict(completed=len(job['ids']), gate=job['gate'],
            passed=bool(passed), updated_at=now()))
        return 0 if not job['gate'] or passed else 2
    finally:
        backend.close()


def eligible_jobs(jobs, states, repair_first=False):
    pending = [j for j in jobs if j['id'] not in states and all(states.get(d,{}).get('state') == 'complete'
        for d in j['depends'])]
    if repair_first:
        # Check repaired methods before spending another device admission on
        # the direct-QA ablation. Stable sorting retains each arm's cohort order.
        pending.sort(key=lambda j: (not j['gate'], j['arm'] == 'partial_direct'))
    return pending


def resume_terminal_states(out, jobs):
    """Resume only an idle controller, preserving every completed/failed attempt."""
    states = read(out/'scheduler_state.json')
    expected = {j['id']: j for j in jobs}
    for jid, state in states.items():
        if jid not in expected or state['state'] not in ('complete', 'failed'):
            raise ValueError('Recovery requires terminal known jobs; inspect active attempts first')
        if state['state'] == 'complete':
            job = expected[jid]
            completion = read(out/'workers'/jid/'completion.json')
            if completion['completed'] != len(job['ids']) or (job['gate'] and not completion['passed']):
                raise ValueError('Completed job lacks matching completion evidence: '+jid)
            for uid in job['ids']:
                result = read(out/'formal/arms'/job['arm']/'runs'/uid/'result.json')
                if result.get('job_id') != jid or 'total_measured_seconds' not in result:
                    raise ValueError('Completed job has missing or mismatched result: '+jid)
    # A worker can create an attempt before the controller persists its launch.
    # Such work must be inspected, never silently replayed on recovery.
    for job in jobs:
        if job['id'] not in states and any((out/'formal/arms'/job['arm']/'runs'/u).exists() for u in job['ids']):
            raise ValueError('Untracked attempt exists; inspect before recovery: '+job['id'])
    return states


def queue(out, resume=False, repair_first=False):
    from script.run_optimized_comparison import inventory
    verify(out)
    jobs = read(out/'jobs.json')
    policy = read(out/'experiment.json')['scheduling']
    minimum_free_mib = policy['minimum_free_mib']
    allowed_gpus = policy['allowed_gpus']
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (out/'scheduler_state.json').exists() and not resume:
            raise ValueError('Scheduler already attempted; inspect retained jobs before recovery')
        states = resume_terminal_states(out, jobs) if resume else {}
        running, stable = {}, {}
        while True:
            for jid, (proc, lease, index) in list(running.items()):
                if proc.poll() is not None:
                    states[jid].update(state='complete' if proc.returncode == 0 else 'failed',
                        exit_code=proc.returncode, finished_at=now())
                    lease.close()
                    del running[jid]
            gpus = {i:r for i,r in inventory().items() if i in allowed_gpus}
            busy = {v[2] for v in running.values()}
            stable = {i:stable.get(i,0)+1 if r['free_mib'] >= minimum_free_mib and i not in busy else 0 for i,r in gpus.items()}
            for index in sorted(gpus, key=lambda i:-gpus[i]['free_mib']):
                pending = eligible_jobs(jobs, states, repair_first)
                if not pending or len(running) >= policy['max_workers'] or stable[index] < policy['stable_observations']:
                    continue
                row = gpus[index]
                lease = (Path('/tmp')/f'videoagent-{os.getuid()}-{row["uuid"]}.lock').open('a')
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lease.close()
                    continue
                fresh = inventory().get(index)
                if not fresh or fresh['uuid'] != row['uuid'] or fresh['free_mib'] < minimum_free_mib:
                    lease.close()
                    continue
                job = pending[0]
                source = out/('source_original' if job['arm'] == 'partial_direct' else 'source')
                env = dict(os.environ, CUDA_VISIBLE_DEVICES=row['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HUB_OFFLINE='1', HF_HOME=str(ROOT/'checkpoints/.cache/huggingface'),
                    HF_MODULES_CACHE=str(ROOT/'checkpoints/.cache/modules'), TOKENIZERS_PARALLELISM='false',
                    OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
                with _parent(out/'workers'/job['id']/'worker.log').open('a') as log:
                    proc = subprocess.Popen([sys.executable,'-u',str(source/'script/run_followup_validation.py'),
                        'worker','--out',str(out),'--job',job['id']], cwd=source, env=env,
                        stdout=log, stderr=subprocess.STDOUT, pass_fds=(lease.fileno(),))
                states[job['id']] = dict(state='running', pid=proc.pid, gpu_index=index,
                    gpu_uuid=row['uuid'], started_at=now())
                running[job['id']] = (proc, lease, index)
                stable[index] = 0
            pending = eligible_jobs(jobs, states, repair_first)
            terminal = not running and not pending
            state = ('complete' if len(states)==len(jobs) and all(s['state']=='complete' for s in states.values())
                else 'failed') if terminal else ('running' if running else 'waiting_for_gpu_memory')
            write(out/'scheduler_state.json', states)
            summarize(out)
            write(out/'queue_status.json', dict(state=state, updated_at=now(), pid=os.getpid(),
                allowed_gpu_indices=allowed_gpus, minimum_free_mib=minimum_free_mib, gpus=gpus,
                scheduling_priority='repair_first' if repair_first else 'cohort_order',
                stable_observations=stable, jobs=states, planned_jobs=len(jobs), ready_jobs=len(pending)))
            if terminal:
                return 0 if state == 'complete' else 1
            time.sleep(policy['poll_seconds'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare','queue','worker','report'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--prior', type=Path)
    parser.add_argument('--job')
    parser.add_argument('--detach', action='store_true')
    parser.add_argument('--resume', action='store_true', help='Recover an idle controller with only terminal tracked jobs')
    parser.add_argument('--repair-first', action='store_true', help='Prioritize repaired-method regression gates')
    parser.add_argument('--repairs-only', action='store_true', help='Evaluate repaired methods with unchanged prior navigation settings')
    parser.add_argument('--minimum-free-gib', type=int, default=48, help='Device admission threshold; recorded in the frozen experiment')
    args = parser.parse_args()
    out = args.out.resolve()
    if args.mode == 'prepare':
        if args.minimum_free_gib < 40:
            parser.error('Device admission must allow at least 40 GiB for this model stack')
        prepare(args.prior.resolve(), out, repairs_only=args.repairs_only,
                minimum_free_mib=args.minimum_free_gib*1024)
        if args.detach:
            command = [sys.executable,'-u',str(out/'source/script/run_followup_validation.py'),'queue','--out',str(out)]
            start = out/'start_queue.sh'
            start.write_text('#!/bin/bash\nset -euo pipefail\nexec '+shlex.join(command)+' >> '+shlex.quote(str(out/'controller.log'))+' 2>&1\n')
            session = 'script-followup-'+str(int(time.time()))
            subprocess.run(['tmux','new-session','-d','-s',session,'bash '+shlex.quote(str(start))],check=True)
            write(out/'launch.json', dict(session=session, command=command, created_at=now()))
        return 0
    try:
        if args.mode == 'report':
            summarize(out)
            return 0
        return queue(out, resume=args.resume, repair_first=args.repair_first) if args.mode == 'queue' else worker(out,args.job)
    except Exception:
        target = out/'controller_failure.json' if args.mode != 'worker' else out/'workers'/args.job/'failure.json'
        write(target, dict(updated_at=now(), traceback=traceback.format_exc()))
        raise


if __name__ == '__main__':
    raise SystemExit(main())
