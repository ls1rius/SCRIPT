"""One explicit resource recovery; original attempts and inference code stay frozen."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.run_complete_memory_comparison import summarize
from script.run_adaptive_comparison import now, read
from script.run_followup_validation import verify
from script.run_optimized_comparison import inventory
from script_core.state import write_json as write


def prepare(original, shard, attempt='resource-r2', allowed_gpus=None):
    verify(original)
    selected = read(original / 'cohort.json')[shard::2]
    attempted = [read(original/'runs/original_replay'/uid/'result.json') for uid in selected
        if (original/'runs/original_replay'/uid/'result.json').exists()]
    if len(attempted) != 1 or 'out of memory' not in str(attempted[0].get('error')).lower():
        raise ValueError('Expected one failed OOM control attempt before materialization')
    if any((original/'materialized'/uid).exists() for uid in selected):
        raise ValueError('Recovery must not rerun completed or partially spent materialization')
    if not attempt.replace('-', '').isalnum(): raise ValueError('Invalid attempt name')
    recovery = original / 'recovery' / f'shard-{shard}-{attempt}'
    recovery.mkdir(parents=True, exist_ok=False)
    roots = {Path(k).parts[0] for k in read(original / 'frozen_hashes.json')}
    for name in sorted(roots | {'frozen_hashes.json'}):
        (recovery / name).symlink_to(original / name)
    verify(recovery)
    request = dict(created_at=now(), shard=shard, original=str(original), recovery=str(recovery),
        reason='First original-replay QA failed with CUDA OOM after shared GPU memory fell.',
        original_attempts_preserved=True, inference_source='Original frozen source unchanged',
        automatic_retries=0, allowed_gpus=allowed_gpus or ['0', '1', '2', '3'], minimum_free_mib=48*1024,
        stable_observations=6, poll_seconds=10,
        report=str(original / 'current_report.json'))
    write(recovery / 'recovery_request.json', request)
    write(original / 'resource_recovery.json', request)
    return recovery


def publish(original, recovery):
    """An explicit overlay report, with both failed and recovered costs visible."""
    combined = recovery / 'combined'
    combined.mkdir(exist_ok=True)
    for name in ('cohort.json', 'evaluation_only.json', 'historical.json'):
        link = combined / name
        if not link.exists(): link.symlink_to(original / name)
    for prefix in ('runs/original_replay', 'runs/complete_with_quotes',
                   'runs/complete_structured', 'materialized'):
        target = combined / prefix
        target.mkdir(parents=True, exist_ok=True)
        for uid in read(original / 'cohort.json'):
            recovered = recovery / prefix / uid
            chosen = recovered if (recovered / ('coverage.json' if prefix == 'materialized' else 'result.json')).exists() else original / prefix / uid
            link = target / uid
            if link.is_symlink() and link.readlink() != chosen: link.unlink()
            if not link.is_symlink(): link.symlink_to(chosen)
    report = summarize(combined)
    costs = {}
    attempts = [('initial_attempt', original)] + [
        ('resource_recovery:'+folder.name, folder) for folder in sorted(recovery.parent.iterdir()) if folder.is_dir()]
    for name, folder in attempts:
        measured = 0.
        # Every ledger appears only once, including failed setup and QA attempts.
        for directory in ('setup', 'runs', 'materialized'):
            for file in (folder / directory).rglob('calls.jsonl'):
                measured += sum(float(json.loads(line).get('actual_cost') or 0)
                    for line in file.read_text().splitlines())
        costs[name] = measured
    report['resource_recovery'] = dict(request=str(recovery / 'recovery_request.json'),
        original_failed_attempts=[str(p) for p in (original / 'verification').glob('baseline_mismatch*.json')],
        measured_seconds_by_attempt=costs, measured_seconds_all_attempts=sum(costs.values()),
        status=read(recovery / 'queue_status.json') if (recovery / 'queue_status.json').exists() else {'state':'prepared'},
        policy='Recovered results take precedence only when completed; original attempts remain on disk and costed.')
    write(original / 'current_report.json', report)
    return report


def queue(original, recovery):
    policy = read(recovery / 'recovery_request.json')
    stable = {}; proc = None; lease = None
    with (recovery / 'controller.lock').open('a') as controller:
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (recovery / 'launch.json').exists(): raise ValueError('Recovery already attempted')
        while True:
            if proc is None:
                rows = {i:r for i,r in inventory().items() if i in policy['allowed_gpus']}
                stable = {i:stable.get(i,0)+1 if r['free_mib'] >= policy['minimum_free_mib'] else 0 for i,r in rows.items()}
                for index in sorted(rows, key=lambda i:-rows[i]['free_mib']):
                    if stable[index] < policy['stable_observations']: continue
                    row = rows[index]
                    candidate = (Path('/tmp') / f'videoagent-finalqa-{os.getuid()}-{row["uuid"]}.lock').open('a')
                    try: fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError: candidate.close(); continue
                    fresh = inventory().get(index)
                    if not fresh or fresh['uuid'] != row['uuid'] or fresh['free_mib'] < policy['minimum_free_mib']:
                        candidate.close(); stable[index] = 0; continue
                    env = dict(os.environ, CUDA_VISIBLE_DEVICES=row['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                        OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
                    command = [sys.executable, '-u', str(recovery/'source/script/run_complete_memory_comparison.py'),
                        'worker', '--out', str(recovery), '--shard', str(policy['shard'])]
                    with (recovery / 'worker.log').open('a') as log:
                        proc = subprocess.Popen(command, cwd=recovery/'source', env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, pass_fds=(candidate.fileno(),))
                    lease = candidate
                    write(recovery / 'launch.json', dict(worker_pid=proc.pid, gpu_index=index, gpu_uuid=row['uuid'],
                        started_at=now(), command=command))
                    break
            code = proc.poll() if proc else None
            if code is not None and lease:
                lease.close(); lease = None
            state = ('complete' if code == 0 else 'failed') if code is not None else 'running' if proc else 'waiting_for_stable_gpu_memory'
            write(recovery / 'queue_status.json', dict(state=state, updated_at=now(),
                worker_pid=proc.pid if proc else None, returncode=code, stable_observations=stable))
            report = publish(original, recovery)
            # Keep the combined report current while the unaffected shard finishes.
            main = read(original / 'queue_status.json')
            if code is not None and not main.get('active_shards'):
                return 0 if code == 0 and report['complete'] else 2
            time.sleep(policy['poll_seconds'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['prepare', 'queue'])
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--shard', type=int, choices=[0,1], default=1)
    p.add_argument('--recovery', type=Path)
    p.add_argument('--attempt', default='resource-r2')
    p.add_argument('--gpus', default='0,1,2,3')
    args = p.parse_args(); original = args.out.resolve()
    if args.mode == 'queue': return queue(original, args.recovery.resolve())
    allowed = args.gpus.split(',')
    if any(i not in ('0', '1', '2', '3') for i in allowed): raise ValueError('Invalid GPU index')
    recovery = prepare(original, args.shard, args.attempt, allowed)
    publish(original, recovery)
    with (recovery/'controller.log').open('a') as log:
        proc = subprocess.Popen([sys.executable, '-u', __file__, 'queue', '--out', str(original),
            '--recovery', str(recovery)], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
    write(recovery/'controller.json', dict(pid=proc.pid, started_at=now()))
    print(json.dumps(dict(recovery=str(recovery), controller_pid=proc.pid)))
    return 0


if __name__ == '__main__': raise SystemExit(main())
