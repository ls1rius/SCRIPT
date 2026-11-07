"""Freeze I presentation now, then run its H prerequisite and I in order."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import controlled_memory_io as io


def verify_manifest(root):
    for name, expected in io.read(root/'frozen_hashes.json').items():
        if io.sha(root/name) != expected:
            raise ValueError('Frozen artifact changed: '+str(root/name))


def prepare(queue_root, parent, after, output, gpu_index='3'):
    if queue_root.exists() or output.exists():
        raise ValueError('Use fresh queue and I output directories')
    verify_manifest(parent)
    if io.read(parent/'experiment.json').get('arm') != 'H_generalized_lazy':
        raise ValueError('I requires H parent evidence')
    if after is not None and not (after/'queue_status.json').exists():
        raise ValueError('Preceding task must have an existing queue')
    root = Path(__file__).resolve().parents[1]
    control = queue_root/'control'
    control.mkdir(parents=True)
    for source in [Path(__file__), root/'script/run_merged_lazy_experiment.py',
                   root/'script/controlled_memory_io.py', root/'script/gpu_queue.py',
                   root/'script_core/merged_lazy_memory.py', root/'script_core/qa_evidence_view.py']:
        shutil.copy2(source, control/source.name)
    # Read the sealed local copy; subsequent workspace edits cannot change this run.
    io.write(queue_root/'plan.json', dict(created_at=io.now(), parent_H=str(parent),
        after=str(after) if after is not None else None, output_I=str(output), gpu_index=str(gpu_index), planned=len(io.read(parent/'cohort.json')),
        parent_manifest_sha256=io.sha(parent/'frozen_hashes.json'),
        render_version='merged-lazy-memory-I-v2-H-adjacent-v1',
        scope='H prerequisite followed by I exact-assertion and adjacent-interval merging; unchanged QA/parser.',
        failures='Stop on dependency or child failure; no automatic retries.'))
    io.write(queue_root/'frozen_hashes.json', {str(p.relative_to(queue_root)):io.sha(p)
        for p in queue_root.rglob('*') if p.is_file()})
    io.write(queue_root/'queue_status.json', dict(state='queued', phase='waiting_for_predecessor',
        dependency=str(after) if after is not None else str(parent), updated_at=io.now()))


def dependency_complete(root):
    state = io.read(root/'queue_status.json')
    if state.get('state') == 'failed':
        raise RuntimeError('Dependency failed: '+str(root))
    failure = root/'failures/queue.json'
    if failure.exists():
        raise RuntimeError('Dependency queue recorded a failure: '+str(failure))
    return state.get('state') == 'complete'


def run(queue_root):
    verify_manifest(queue_root)
    plan = io.read(queue_root/'plan.json')
    parent, output = (Path(plan[k]) for k in ('parent_H','output_I'))
    after = Path(plan['after']) if plan.get('after') else None
    if io.sha(parent/'frozen_hashes.json') != plan['parent_manifest_sha256']:
        raise ValueError('H snapshot manifest changed')
    verify_manifest(parent)

    def status(state, phase, **details):
        io.write(queue_root/'queue_status.json', dict(state=state, phase=phase,
            controller_pid=os.getpid(), updated_at=io.now(), **details))

    def child(phase, command):
        launch = queue_root/'launches'/f'{phase}.json'
        if launch.exists():
            raise ValueError('Stage already attempted: '+phase)
        (queue_root/'logs').mkdir(exist_ok=True)
        with (queue_root/'logs'/f'{phase}.log').open('a') as log:
            proc = subprocess.Popen(command, cwd=queue_root, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT)
        io.write(launch, dict(pid=proc.pid, command=command, started_at=io.now()))
        while proc.poll() is None:
            status('running', phase, child_pid=proc.pid)
            time.sleep(10)
        if proc.returncode:
            raise RuntimeError(f'{phase} failed with exit code {proc.returncode}')

    with (queue_root/'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            while after is not None and not dependency_complete(after):
                status('waiting_for_predecessor', 'before_H', dependency=str(after))
                time.sleep(30)
            if not dependency_complete(parent):
                parent_state = io.read(parent/'queue_status.json')
                if parent_state.get('state') != 'prepared' or (parent/'launch.json').exists():
                    # Another controller owns H; wait without duplicating its work.
                    while not dependency_complete(parent):
                        status('waiting_for_H', 'H', dependency=str(parent))
                        time.sleep(30)
                else:
                    child('H_queue', [sys.executable, '-u',
                        str(parent/'control/run_generalized_lazy_experiment.py'), 'queue', '--out', str(parent)])
            if not dependency_complete(parent):
                raise RuntimeError('H exited without completing its cohort')
            entry = queue_root/'control/run_merged_lazy_experiment.py'
            child('I_prepare', [sys.executable, '-u', str(entry), 'prepare', '--reference', str(parent),
                '--out', str(output), '--gpu-index', plan['gpu_index']])
            child('I_queue', [sys.executable, '-u', str(output/'control/run_merged_lazy_experiment.py'),
                'queue', '--out', str(output)])
            if not dependency_complete(output):
                raise RuntimeError('I exited without completing its cohort')
            status('complete', 'complete', output_I=str(output))
            return 0
        except Exception as exc:
            status('failed', 'stopped', error=str(exc))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare','run'])
    parser.add_argument('--queue-root', type=Path, required=True)
    parser.add_argument('--parent-H', type=Path)
    parser.add_argument('--after', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--gpu-index', default='3')
    args = parser.parse_args()
    queue_root = args.queue_root.resolve()
    if args.mode == 'prepare':
        if any(x is None for x in (args.parent_H, args.out)):
            parser.error('prepare requires --parent-H, --out')
        prepare(queue_root, args.parent_H.resolve(), args.after.resolve() if args.after else None, args.out.resolve(), args.gpu_index)
        return 0
    return run(queue_root)


if __name__ == '__main__':
    raise SystemExit(main())
