"""Explicit O continuation after an upstream stop, with identical model runtime."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import controlled_memory_io as io
from script.parallel_artifacts import tree_hashes, copy_tree_verified


def completed_prefix(out):
    ids = io.read(out/'experiment.json')['case_ids']
    complete = []
    for uid in ids:
        path = out/'runs'/uid/'result.json'
        if not path.exists():
            break
        row = io.read(path)
        if not row.get('complete') or row.get('error'):
            raise ValueError('Only complete O/O+ pairs may be inherited')
        complete.append(uid)
    actual = {p.name for p in (out/'runs').iterdir()}
    if actual != set(complete):
        raise ValueError('Partial or non-prefix outputs require separate accounting')
    if list((out/'runs').rglob('*.pending.json')):
        raise ValueError('Pending calls require explicit accounting')
    return complete


def prepare(old, out, upstream_control):
    from script.run_o_sentence_experiment import verify
    from script.o_composition_context_eval import report
    verify(old)
    state = io.read(old/'queue_status.json')
    if out.exists() or state['state'] != 'failed':
        raise ValueError('Fresh output and stopped failed prior required')
    for pid in [state.get('controller_pid'), io.read(old/'launch.json').get('worker_pid')]:
        if pid and Path('/proc', str(pid)).exists():
            raise ValueError('Prior process remains alive')
    complete = completed_prefix(old)
    for name in ['source_bound_functional.json', 'batch_functional.json']:
        if not io.read(old/'validation'/name)['passed']:
            raise ValueError('Identical runtime must have a passed technical gate')
    before = tree_hashes(old)
    out.mkdir(parents=True)
    started = time.monotonic()
    names = ['runtime', 'questions', 'cases', 'runs', 'validation']
    def copy(name):
        snapshot = {k[len(name)+1:]:v for k,v in before.items() if k.startswith(name+'/')}
        return copy_tree_verified(old/name, out/name, snapshot=snapshot, workers=1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(copy, names))
    for name in ['qa_config.json', 'qa_expected_artifacts.json', 'evaluation_only.json', 'cohort.json']:
        shutil.copy2(old/name, out/name)
    policy = io.read(old/'experiment.json')
    policy.update(previous_run=str(old), created_at=io.now(),
                  upstream_repair_control=str(upstream_control),
                  continuation_reason='Explicit user-requested continuation after N stopped',
                  inherited_complete_pairs=len(complete))
    io.write(out/'experiment.json', policy)
    archive = out/'archived_attempt'
    archive.mkdir()
    for name in ['queue_status.json', 'report.json', 'setup.json', 'failure.json', 'reloads.json']:
        if (old/name).exists():
            shutil.copy2(old/name, archive/name)
    if tree_hashes(old) != before:
        raise ValueError('Prior O changed during copy')
    shutil.copy2(Path(__file__), out/'runtime/script/resume_o_upstream.py')
    shutil.copy2(ROOT/'script/parallel_artifacts.py', out/'runtime/script/parallel_artifacts.py')
    io.write(out/'continuation.json', dict(prior_hashes=before, successful_ids=complete,
        prior_unchanged=True, model_runtime_unchanged=True, cpu_seconds=time.monotonic()-started,
        preparation_workers=4, technical_gates='Inherited identical-runtime gates, not new calls',
        accounting='Copied successful calls counted once; prior setup/reload/gate costs archived, not free retries',
        prior_setup=io.read(old/'setup.json'),
        prior_reloads=io.read(old/'reloads.json') if (old/'reloads.json').exists() else []))
    io.write(out/'frozen_hashes.json', tree_hashes(out))
    verify(out)
    report(out)
    io.write(out/'queue_status.json', dict(state='prepared', updated_at=io.now()))


def upstream_ready(out):
    policy = io.read(out/'experiment.json')
    parent = Path(policy['parent_N'])
    control = Path(policy['upstream_repair_control'])
    if not (control/'launch.json').exists():
        return False, 'waiting_for_N_repair'
    state = io.read(parent/'queue_status.json')
    if state.get('scheduler') != str(control):
        return False, 'waiting_for_N_repair'
    if state['state'] == 'failed':
        raise RuntimeError('Repaired N failed; no automatic O restart')
    done = completed_prefix(out)
    uid = policy['case_ids'][len(done)]
    result = parent/'runs'/uid/'result.json'
    if not result.exists():
        return False, 'waiting_for_N_source'
    row = io.read(result)
    if row.get('error') or not row.get('answer'):
        raise ValueError('Invalid next N source')
    return True, uid


def queue(out):
    from script.run_o_sentence_experiment import verify
    from script.gpu_queue import acquire
    verify(out)
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (out/'launch.json').exists():
            raise ValueError('No automatic retry')
        def status(state, **extra):
            io.write(out/'queue_status.json', dict(state=state, controller_pid=os.getpid(), updated_at=io.now(), **extra))
        try:
            while True:
                ready, reason = upstream_ready(out)
                if ready:
                    break
                status(reason, model_loaded=False)
                time.sleep(10)
            with acquire(io.read(out/'experiment.json')['resource_policy'], lambda d: status('waiting_for_gpu', **d)) as (gpu, _):
                env = dict(os.environ, VIDEOAGENT_GPU_LEASE=gpu['reservation_id'], CUDA_VISIBLE_DEVICES=gpu['uuid'],
                    CUDA_DEVICE_ORDER='PCI_BUS_ID', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                    TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='4', MKL_NUM_THREADS='4')
                cmd = [sys.executable, '-u', str(out/'runtime/script/resume_o_upstream.py'), 'worker', '--out', str(out)]
                with (out/'worker.log').open('a') as log:
                    child = subprocess.Popen(cmd, env=env, cwd=out/'runtime', stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                io.write(out/'launch.json', dict(worker_pid=child.pid, gpu=gpu, command=cmd, at=io.now()))
                while child.poll() is None:
                    status('running', worker_pid=child.pid, gpu=gpu['index'])
                    time.sleep(5)
                if child.returncode != 0 or not io.read(out/'report.json')['complete']:
                    raise RuntimeError('O continuation stopped; retained without automatic retry')
                status('complete')
        except Exception as exc:
            status('failed', error=str(exc))
            raise


def worker(out):
    from script.run_o_sentence_experiment import verify
    from script.run_evidence_planner_experiment import verify_model
    from script.run_o_full_experiment import run_live
    from script.o_composition_context_eval import report
    from script_core.budget import ModelWorker
    verify(out)
    verify_model(out)
    if (out/'setup.json').exists():
        raise ValueError('Already attempted')
    cfg = io.read(out/'qa_config.json')
    backend = ModelWorker(cfg)
    try:
        start = time.monotonic()
        io.write(out/'setup.json', dict(state='loading', started_at=io.now()))
        details = backend.call('prepare_vlm', {}, 600)
        io.write(out/'setup.json', dict(state='ready', seconds=time.monotonic()-start, details=details,
            technical_gates='Inherited unchanged-runtime checks; no new model gate claim'))
        run_live(out, backend, cfg)
    except Exception as exc:
        io.write(out/'failure.json', dict(type=type(exc).__name__, message=str(exc), at=io.now()))
        raise
    finally:
        backend.close(graceful=True)
        report(out)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'queue', 'worker'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--old', type=Path)
    parser.add_argument('--upstream-control', type=Path)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args.old.resolve(), args.out.resolve(), args.upstream_control.resolve())
    else:
        globals()[args.mode](args.out.resolve())
