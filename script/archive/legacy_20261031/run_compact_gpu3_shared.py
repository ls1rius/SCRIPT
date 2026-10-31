"""User-authorized GPU 3 sharing for the frozen compact-input experiment."""
from pathlib import Path
import sys
_PROJECT_ROOT = next(p for p in Path(__file__).resolve().parents if (p/'script_core').is_dir())
sys.path[:0] = [str(_PROJECT_ROOT), str(_PROJECT_ROOT/'script')]

import argparse
import fcntl
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main(out):
    sys.path.insert(0, str(out/'control'))
    run = importlib.import_module('run_compact_evidence_experiment')
    io = run.io
    run.verify(out)
    record = out/'control/gpu3_shared'
    for name, expected in io.read(record/'hashes.json').items():
        if io.sha(record/name) != expected:
            raise ValueError('GPU 3 controller seal changed')
    request = io.read(record/'request.json')
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (out/'launch.json').exists():
            raise ValueError('Prior GPU attempt exists; inspect retained state')
        serial = 0
        while True:
            run.prepare_ready(out)
            if run.report(out)['complete']:
                io.write(out/'queue_status.json', dict(state='complete', updated_at=io.now()))
                return 0
            span = run.next_range(out)
            stable = 0
            while True:
                row = run.inventory()['3']
                if row['uuid'] != request['gpu_uuid']:
                    raise ValueError('Physical GPU 3 identity changed')
                stable = stable+1 if span and row['free_mib'] >= request['minimum_free_mib'] else 0
                io.write(out/'queue_status.json', dict(state='waiting_for_gpu_memory' if span else 'waiting_for_parent_inputs',
                    controller_pid=os.getpid(), gpu_index='3', gpu_uuid=row['uuid'],
                    resource_policy='shared_gpu_explicitly_authorized_by_user',
                    minimum_free_mib=request['minimum_free_mib'], stable_observations=stable,
                    gpu=row, updated_at=io.now()))
                if span and stable >= request['stable_observations']:
                    break
                time.sleep(request['poll_seconds'])
                if span is None:
                    run.prepare_ready(out)
                    span = run.next_range(out)
            # The old global exclusive lease belongs to another task. Sharing was explicitly
            # requested; acquire a separate lease for this compact experiment only.
            with (Path('/tmp')/f'videoagent-compact-shared-{os.getuid()}-{row["uuid"]}.lock').open('a') as lease:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(request['poll_seconds'])
                    continue
                fresh = run.inventory()['3']
                if fresh['uuid'] != row['uuid'] or fresh['free_mib'] < request['minimum_free_mib']:
                    continue
                start, end = span
                if not (out/'validation/baseline_reproduction.json').exists():
                    start, end, phase = 0, 20, 'baseline_smoke'
                else:
                    phase = f'gpu3_compact_{start}_{end}_{serial}'
                env = dict(os.environ, CUDA_VISIBLE_DEVICES=row['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                    OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
                command = [sys.executable, '-u', str(out/'control/run_compact_evidence_experiment.py'),
                    'worker', '--out', str(out), '--start', str(start), '--end', str(end), '--phase', phase]
                (out/'logs').mkdir(exist_ok=True)
                with (out/'logs'/f'{phase}.log').open('a') as log:
                    proc = subprocess.Popen(command, cwd=out, env=env, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, pass_fds=(lease.fileno(),))
                launch = dict(gpu_index='3', gpu_uuid=row['uuid'], worker_pid=proc.pid,
                    controller_pid=os.getpid(), phase=phase, start=start, end=end, started_at=io.now(),
                    resource_policy='shared_gpu_explicitly_authorized_by_user')
                io.write(out/'workers'/f'{phase}.json', launch)
                if not (out/'launch.json').exists():
                    io.write(out/'launch.json', launch)
                while proc.poll() is None:
                    run.report(out)
                    io.write(out/'queue_status.json', dict(launch, state='running', updated_at=io.now()))
                    time.sleep(5)
                run.report(out)
                if proc.returncode:
                    io.write(out/'queue_status.json', dict(launch, state='failed', returncode=proc.returncode,
                        updated_at=io.now()))
                    return proc.returncode
            serial += 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.out.resolve()))
    except Exception as exc:
        import traceback
        p = args.out.resolve()/'control/gpu3_shared/failure.json'
        p.write_text(json.dumps(dict(type=type(exc).__name__, message=str(exc),
            traceback=traceback.format_exc()), ensure_ascii=False, indent=2))
        raise
