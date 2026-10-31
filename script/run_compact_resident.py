"""Keep one final-QA backend resident while consuming sealed compact inputs."""
import argparse
import fcntl
import importlib
import itertools
import os
from pathlib import Path
import subprocess
import sys
import time


def process(pid):
    try:
        fields = Path('/proc', str(pid), 'stat').read_text().rsplit(') ', 1)[1].split()
        return dict(alive=fields[0] != 'Z', start_ticks=fields[19])
    except FileNotFoundError:
        return dict(alive=False)


class ResidentBackend:
    """Let the frozen per-range worker finish without unloading its backend."""

    def __init__(self, backend):
        self.backend = backend

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def close(self):
        pass


def drain(out, run, protocol, poll_seconds=5):
    """Use the original QA worker and retain its prepared model across ranges."""
    original_warm = protocol.warm_backend
    prepared = None

    def warm_once(folder, mode, phase):
        nonlocal prepared
        if prepared is None:
            cfg, backend, ledger, meter = original_warm(folder, mode, phase)
            prepared = cfg, ResidentBackend(backend), ledger, meter
        return prepared

    protocol.warm_backend = warm_once
    try:
        while True:
            run.prepare_ready(out)
            if run.report(out)['complete']:
                return 0
            span = run.next_range(out)
            run.io.write(out/'resident_state.json', dict(
                state='running' if span else 'waiting_for_parent_inputs',
                model_resident=prepared is not None, worker_pid=os.getpid(),
                ready_range=list(span) if span else None, updated_at=run.io.now()))
            if span is None:
                time.sleep(poll_seconds)
                continue
            code = run.worker(out, *span, 'resident_qa')
            if code:
                return code
    finally:
        protocol.warm_backend = original_warm
        if prepared is not None:
            prepared[1].backend.close()


def worker(out, run):
    protocol = importlib.import_module('compact_protocol')
    protocol.io.ARMS = run.ARMS
    protocol.io.ORDERS = tuple(itertools.permutations(run.ARMS))
    return drain(out, run, protocol)


def drain_many(tasks, protocol, poll_seconds=5):
    """Answer multiple compatible experiments with one prepared model."""
    for name in ['qa_config.json','qa_expected_artifacts.json','control/compact_protocol.py']:
        hashes={run.io.sha(out/name) for out,run in tasks}
        if len(hashes)!=1:
            raise ValueError('Shared QA experiments differ: '+name)
    original_warm=protocol.warm_backend
    prepared=None

    def publish(out,run,state,**details):
        run.io.write(out/'queue_status.json',dict(state=state,worker_pid=os.getpid(),
            controller_pid=os.getppid(),gpu_index='3',model_resident=prepared is not None,
            resource_policy='shared_resident_C_D',updated_at=run.io.now(),**details))

    def warm_once(out,mode,phase):
        nonlocal prepared
        if prepared is None:
            cfg,backend,ledger,meter=original_warm(out,mode,phase)
            prepared=cfg,ResidentBackend(backend),ledger,meter
            for root,run in tasks:
                state_path=root/'queue_status.json'
                details=run.io.read(state_path) if state_path.exists() else {}
                run.io.write(state_path,dict(details,model_resident=True,updated_at=run.io.now()))
        return prepared

    protocol.warm_backend=warm_once
    try:
        while True:
            unfinished=False
            worked=False
            for out,run in tasks:
                run.prepare_ready(out)
                if run.report(out)['complete']:
                    publish(out,run,'complete')
                    continue
                unfinished=True
                span=run.next_range(out)
                if span is None:
                    publish(out,run,'waiting_for_parent_inputs')
                    continue
                publish(out,run,'running',phase='shared_resident_qa',ready_range=list(span))
                code=run.worker(out,*span,'shared_resident_qa')
                if code:
                    return code
                worked=True
            if not unfinished:
                return 0
            if not worked:
                time.sleep(poll_seconds)
    finally:
        protocol.warm_backend=original_warm
        if prepared is not None:
            prepared[1].backend.close()


def controller(out, record, run):
    io = run.io
    for name, expected in io.read(record/'hashes.json').items():
        if io.sha(record/name) != expected:
            raise ValueError('Resident controller artifact changed: '+name)
    run.verify(out)
    request = io.read(record/'request.json')
    handoff = io.read(record/'handoff.json')
    if not io.read(out/'validation/baseline_reproduction.json')['passed']:
        raise ValueError('Baseline reproduction must pass before resident continuation')
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        io.write(out/'controller.json', dict(pid=os.getpid(), control=str(record), started_at=io.now()))
        adopted = handoff.get('worker')
        if adopted:
            while True:
                info = process(adopted['worker_pid'])
                if not info['alive']:
                    break
                if info['start_ticks'] != adopted['start_ticks']:
                    raise ValueError('Adopted worker identity changed')
                run.report(out)
                io.write(out/'queue_status.json', dict(adopted,
                    state='running_finishing_existing_batch', controller_pid=os.getpid(),
                    continuation='resident_model', updated_at=io.now()))
                time.sleep(5)
            for uid in io.read(out/'cohort.json')[adopted['start']:adopted['end']]:
                for arm in run.ARMS:
                    result = out/'runs'/arm/uid/'result.json'
                    if not result.exists():
                        raise ValueError('Adopted worker stopped incompletely: '+str(result))
                    error = io.read(result).get('error')
                    protocol = importlib.import_module('compact_protocol')
                    if error and protocol.is_infrastructure(error):
                        raise ValueError('Adopted worker failed: '+str(error))
        stable = 0
        while True:
            run.prepare_ready(out)
            if run.report(out)['complete']:
                io.write(out/'queue_status.json', dict(state='complete', updated_at=io.now()))
                return 0
            row = run.inventory()['3']
            if row['uuid'] != request['gpu_uuid']:
                raise ValueError('Physical GPU 3 identity changed')
            span = run.next_range(out)
            stable = stable+1 if span and row['free_mib'] >= request['minimum_free_mib'] else 0
            io.write(out/'queue_status.json', dict(
                state='waiting_for_initial_gpu_memory' if span else 'waiting_for_parent_inputs',
                controller_pid=os.getpid(), continuation='resident_model',
                gpu_index='3', gpu=row, minimum_free_mib=request['minimum_free_mib'],
                stable_observations=stable, updated_at=io.now()))
            if stable < request['stable_observations']:
                time.sleep(request['poll_seconds'])
                continue
            break
        with (Path('/tmp')/f'videoagent-compact-shared-{os.getuid()}-{row["uuid"]}.lock').open('a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=row['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
            command = [sys.executable, '-u', str(Path(__file__).resolve()), 'worker',
                       '--out', str(out), '--record', str(record)]
            (out/'logs').mkdir(exist_ok=True)
            with (out/'logs/resident_qa.log').open('a') as log:
                proc = subprocess.Popen(command, cwd=out, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, pass_fds=(lease.fileno(),))
            launch = dict(gpu_index='3', gpu_uuid=row['uuid'], worker_pid=proc.pid,
                controller_pid=os.getpid(), phase='resident_qa', started_at=io.now(),
                resource_policy='shared_gpu_resident_model', lifecycle='Keep one backend until all inputs are answered.')
            io.write(record/'launch.json', dict(launch, command=command))
            io.write(out/'workers/resident_qa.json', launch)
            while proc.poll() is None:
                run.report(out)
                state_path = out/'resident_state.json'
                details = io.read(state_path) if state_path.exists() else dict(state='loading_model', model_resident=False)
                io.write(out/'queue_status.json', dict(launch, **{k:v for k,v in details.items()
                    if k not in launch and k != 'updated_at'}, updated_at=io.now()))
                time.sleep(5)
            summary = run.report(out)
            state = 'complete' if proc.returncode == 0 and summary['complete'] else 'failed'
            io.write(out/'queue_status.json', dict(launch, state=state,
                returncode=proc.returncode, updated_at=io.now()))
            return proc.returncode or (0 if summary['complete'] else 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['controller', 'worker'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--record', type=Path, required=True)
    args = parser.parse_args()
    out, record = args.out.resolve(), args.record.resolve()
    sys.path.insert(0, str(out/'control'))
    run = importlib.import_module('run_compact_evidence_experiment')
    try:
        return controller(out, record, run) if args.mode == 'controller' else worker(out, run)
    except Exception as exc:
        import traceback
        run.io.write(record/f'{args.mode}_failure.json', dict(type=type(exc).__name__,
            message=str(exc), traceback=traceback.format_exc(), updated_at=run.io.now()))
        raise


if __name__ == '__main__':
    raise SystemExit(main())
