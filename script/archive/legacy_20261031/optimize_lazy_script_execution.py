"""Activate F microbatching at an idle call boundary, retaining the original experiment."""
from pathlib import Path
import sys
_PROJECT_ROOT = next(p for p in Path(__file__).resolve().parents if (p/'script_core').is_dir())
sys.path[:0] = [str(_PROJECT_ROOT), str(_PROJECT_ROOT/'script')]

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(_PROJECT_ROOT/'script'))
import controlled_memory_io as io


def process(pid):
    try:
        path = Path('/proc', str(pid))
        stat = (path / 'stat').read_text().rsplit(') ', 1)[1].split()
        return dict(alive=stat[0] != 'Z', state=stat[0], start_ticks=stat[19],
            command=(path / 'cmdline').read_bytes().replace(b'\0', b' ').decode())
    except FileNotFoundError:
        return dict(alive=False)


def verify(out, record):
    for manifest, root in [(out / 'frozen_hashes.json', out), (record / 'hashes.json', record)]:
        for name, expected in io.read(manifest).items():
            if io.sha(root / name) != expected:
                raise ValueError('Changed artifact: ' + str(root / name))


def prepare(out):
    record = out / 'control/execution_optimization'
    if record.exists():
        raise ValueError('Execution optimization already prepared; inspect existing handoff')
    for name, expected in io.read(out / 'frozen_hashes.json').items():
        if io.sha(out / name) != expected:
            raise ValueError('Original artifact changed: ' + name)
    record.mkdir()
    root = _PROJECT_ROOT
    for source in [root / 'script/run_lazy_script_experiment.py', root / 'script/lazy_script_execution.py',
            root / 'script_core/batch_extraction.py', Path(__file__), out / 'control/controlled_memory_io.py']:
        shutil.copy2(source, record / source.name)
    io.write(record / 'request.json', dict(created_at=io.now(), batch_size=2,
        reason='User requested direct execution optimization instead of a separate benchmark or algorithm arm.',
        original_runtime_preserved=True, same_database=True, completed_answers_reused=True,
        accounting='One charged call per microbatch; source request count is reported separately.',
        continuation='Stop only between model calls; keep committed fields and restore all paid call budget.'))
    io.write(record / 'hashes.json', {p.name: io.sha(p) for p in record.glob('*.py')})
    verify(out, record)
    return record


def suspend_at_boundary(out, worker, seconds=60):
    """Never cancel an active model call or interrupt a final QA generation."""
    began = time.monotonic()
    while time.monotonic() - began < seconds:
        current = process(worker['pid'])
        if not current['alive'] or current['start_ticks'] != worker['start_ticks']:
            raise RuntimeError('Original worker changed before handoff')
        progress = io.read(out / 'progress.json')
        uid = progress.get('question_id')
        folder = out / 'materialized' / str(uid)
        if progress.get('stage') != 'missing_field_extraction' or list((folder / 'raw_calls').glob('*.pending.json')):
            time.sleep(.01)
            continue
        os.kill(worker['pid'], signal.SIGSTOP)
        while process(worker['pid']).get('state') not in ('T', 't'):
            time.sleep(.001)
        # The parent can cross a boundary between our first observation and
        # SIGSTOP. Recheck its actual pending files while it cannot issue calls.
        progress = io.read(out / 'progress.json')
        uid = progress.get('question_id')
        folder = out / 'materialized' / str(uid)
        qa = out / 'runs/F_lazy_script' / str(uid)
        safe = (progress.get('stage') == 'missing_field_extraction'
            and (folder / 'plan.json').exists()
            and not list((folder / 'raw_calls').glob('*.pending.json'))
            and not (qa.exists() and not (qa / 'result.json').exists()))
        if safe:
            return progress
        os.kill(worker['pid'], signal.SIGCONT)
        time.sleep(.01)
    raise TimeoutError('No idle model-call boundary observed; original F was left running')


def activate(out, record):
    verify(out, record)
    launch = io.read(out / 'launch.json')
    prior = io.read(out / 'queue_status.json')
    worker = dict(process(launch['worker_pid']), pid=launch['worker_pid'])
    controller = dict(process(launch['controller_pid']), pid=launch['controller_pid'])
    for proc, mode in [(worker, 'worker'), (controller, 'queue')]:
        if (not proc['alive'] or 'run_lazy_script_experiment.py ' + mode not in proc['command']
                or str(out) not in proc['command']):
            raise ValueError('Original F process identity mismatch')
    progress = suspend_at_boundary(out, worker)
    try:
        io.write(record / 'prior_launch.json', launch)
        io.write(record / 'prior_queue_status.json', prior)
        io.write(record / 'prior_report.json', io.read(out / 'report.json'))
        io.write(record / 'prior_progress.json', progress)
        # This queue owns no model itself; retire it before the worker exits so
        # its final status write cannot overwrite the new controller's state.
        os.kill(controller['pid'], signal.SIGTERM)
        os.kill(worker['pid'], signal.SIGINT)
    finally:
        os.kill(worker['pid'], signal.SIGCONT)
    began = time.monotonic()
    while process(worker['pid'])['alive'] or process(controller['pid'])['alive']:
        if time.monotonic() - began > 30:
            raise RuntimeError('Original F has not exited; new model was not launched')
        time.sleep(.1)
    ids = io.read(out / 'cohort.json')
    done = {uid: io.sha(p) for uid in ids if (p := out / 'runs/F_lazy_script' / uid / 'result.json').exists()}
    partial = [uid for uid in ids if uid not in done and (out / 'materialized' / uid).exists()]
    if len(partial) != 1:
        raise ValueError('Expected exactly one partial extraction at handoff')
    uid = partial[0]
    if list((out / 'materialized' / uid / 'raw_calls').glob('*.pending.json')):
        raise ValueError('Handoff unexpectedly left a pending model call')
    database = out / io.read(out / 'experiment.json')['database']
    with sqlite3.connect(database) as source, sqlite3.connect(record / 'checkpoint.sqlite3') as target:
        source.backup(target)
        versions = [row[0] for row in source.execute('SELECT DISTINCT version FROM field_cache')]
        fields = source.execute('SELECT COUNT(*) FROM field_cache').fetchone()[0]
    io.write(record / 'checkpoint.json', dict(created_at=io.now(), partial_question_id=uid,
        completed_result_hashes=done, completed_questions=len(done), field_cache_versions=versions,
        committed_fields=fields, stopped_between_calls=True, interrupted_calls=0,
        interrupted_unlogged_wall_estimate_seconds=0, original_processes=dict(worker=worker, controller=controller)))
    verify(out, record)
    command = [sys.executable, '-u', str(record / Path(__file__).name), 'controller', '--out', str(out)]
    with (record / 'controller.log').open('a') as log:
        proc = subprocess.Popen(command, cwd=out, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    io.write(record / 'controller.json', dict(pid=proc.pid, command=command, started_at=io.now()))
    return dict(controller_pid=proc.pid, completed_preserved=len(done), partial_question_id=uid,
        committed_fields=fields, batch_size=2, interrupted_calls=0)


def controller(out, record):
    verify(out, record)
    checkpoint = io.read(record / 'checkpoint.json')
    for uid, expected in checkpoint['completed_result_hashes'].items():
        if io.sha(out / 'runs/F_lazy_script' / uid / 'result.json') != expected:
            raise ValueError('Previously completed answer changed')
    old = io.read(record / 'prior_launch.json')
    with (out / 'controller.lock').open('a') as lock, \
            (Path('/tmp') / f"videoagent-F-{os.getuid()}-{old['gpu_uuid']}.lock").open('a') as lease:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Reuse F's released ~18 GiB model slot, leaving room for the second
        # source's KV cache. Other processes on the shared card are untouched.
        minimum_free = 19456
        while True:
            rows = subprocess.check_output(['nvidia-smi', '--query-gpu=uuid,memory.free',
                '--format=csv,noheader,nounits'], text=True)
            free = {a.strip(): int(b.strip()) for line in rows.splitlines() for a, b in [line.split(',')]}
            if free[old['gpu_uuid']] >= minimum_free:
                break
            io.write(out / 'queue_status.json', dict(state='waiting_for_execution_resume_memory',
                controller_pid=os.getpid(), gpu_index=old['gpu_index'],
                free_mib=free[old['gpu_uuid']], minimum_free_mib=minimum_free, updated_at=io.now()))
            time.sleep(10)
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=old['gpu_uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
            HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
            OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
        command = [sys.executable, '-u', str(record / 'run_lazy_script_experiment.py'), 'worker', '--out', str(out)]
        with (record / 'worker.log').open('a') as log:
            proc = subprocess.Popen(command, cwd=out, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lease.fileno(),))
        state = dict(controller_pid=os.getpid(), worker_pid=proc.pid, gpu_index=old['gpu_index'],
            gpu_uuid=old['gpu_uuid'], command=command, started_at=io.now(), execution_batch_size=2,
            execution_record=str(record), state='running')
        io.write(record / 'launch.json', state)
        while proc.poll() is None:
            io.write(out / 'queue_status.json', dict(state, updated_at=io.now()))
            time.sleep(5)
        state.update(state='complete' if proc.returncode == 0 else 'failed', returncode=proc.returncode)
        io.write(out / 'queue_status.json', dict(state, updated_at=io.now()))
        return proc.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['activate', 'controller'])
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    if args.mode == 'activate':
        record = prepare(out)
        print(json.dumps(activate(out, record), ensure_ascii=False), flush=True)
        return 0
    return controller(out, out / 'control/execution_optimization')


if __name__ == '__main__':
    raise SystemExit(main())
