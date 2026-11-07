"""Adopt resident Q6 workers and admit additional actors on GPU0/2 only."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def validate_config(cfg):
    if cfg['allowed_gpu_indices'] != ['0', '2']:
        raise ValueError('This extension allows GPU0 and GPU2 only')
    if cfg['additional_actor_ids'] != [3, 4, 5]:
        raise ValueError('Expected three bounded additional actors')
    if [row['id'] for row in cfg['active_workers']] != [0, 1, 2]:
        raise ValueError('Expected original resident learner and two actors')
    return cfg


def actor_policy(original, cfg):
    validate_config(cfg)
    if original['minimum_free_mib'] != 24576 or original['admission_mode'] != 'memory_shared':
        raise ValueError('Actor admission requirements changed')
    result = dict(original, preference=list(cfg['allowed_gpu_indices']))
    return result


def configure(control):
    cfg = validate_config(json.loads((control / 'config.json').read_text()))
    sys.path.insert(0, cfg['runtime'])
    return cfg


def actor(control, wid):
    cfg = configure(control)
    if wid not in cfg['additional_actor_ids']:
        raise ValueError('Only new actor IDs may be launched')
    from script import gpu_queue
    original = gpu_queue.acquire
    gpu_queue.acquire = lambda policy, on_wait: original(actor_policy(policy, cfg), on_wait)
    from script.q6_parallel_rl import worker
    worker(Path(cfg['out']), wid)


def supervise(control):
    cfg = configure(control)
    from script.scale_live_parallel_pool import read, write, now, identity, verify_manifest, worker_health
    from script.train_q6_rl import evaluation_report
    out = Path(cfg['out'])
    verify_manifest(out)
    if os.environ.get('VIDEOAGENT_PHYSICAL_CUDA_POOL') != '1':
        raise ValueError('Physical CUDA pools required')
    rows = list(cfg['active_workers'])
    children = {}
    with (out / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (control / 'launch.json').exists():
            raise ValueError('Extension already launched')
        for row in rows:
            if identity(row['pid']) != row['identity']:
                raise ValueError('Resident worker identity changed')
        write(control / 'launch.json', dict(pid=os.getpid(), identity=identity(os.getpid()), at=now()))
        for wid in cfg['additional_actor_ids']:
            command = [sys.executable, '-u', str(control / 'runner.py'), 'actor', '--control', str(control), '--worker-id', str(wid)]
            env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                       OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', CUDA_DEVICE_ORDER='PCI_BUS_ID')
            with (out / f'worker-{wid}.log').open('x') as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
            children[wid] = child
            rows.append(dict(id=wid, pid=child.pid, identity=identity(child.pid)))
            write(control / 'workers_launch.json', rows)
        while True:
            states = {}
            failure = read(out / 'failure.json') if (out / 'failure.json').exists() else None
            for row in rows:
                path = out / 'workers' / str(row['id']) / 'status.json'
                status = read(path) if path.exists() else dict(stage='starting', pid=row['pid'])
                states[str(row['id'])] = status
                error = worker_health(row, status)
                if error and not failure:
                    failure = dict(worker_id=row['id'], message=error, at=now())
                    write(out / 'failure.json', failure)
            for child in children.values():
                child.poll()
            trained = (out / 'training/result.json').exists()
            evaluation = evaluation_report(out) if trained else None
            live = any(identity(row['pid']) == row['identity'] for row in rows)
            complete = bool(evaluation and evaluation['complete'] and not live and not failure)
            if not live and not complete and not failure:
                failure = dict(message='Extended Q6 workers exited before completion', at=now())
                write(out / 'failure.json', failure)
            stage = 'failed' if failure else 'complete' if complete else 'validation300' if trained else 'parallel_RL'
            epochs = read(out / 'training_config.json')['epochs']
            write(out / 'queue_status.json', dict(state='failed' if failure else 'complete' if complete else 'running',
                stage=stage, controller_pid=os.getpid(), workers=states, allowed_gpu_indices=cfg['allowed_gpu_indices'],
                user_reserved_gpu_indices=['1', '3'], maximum_model_workers=len(rows),
                resource_controller=str(control), primary_arm='Qplus', updated_at=now()))
            write(out / 'report.json', dict(stage=stage, complete=complete, primary_arm='Qplus', epochs=epochs,
                training_progress=read(out / 'training/status.json') if (out / 'training/status.json').exists() else None,
                training=read(out / 'training/result.json') if trained else None, evaluation=evaluation,
                failure=failure, resource_controller=str(control), updated_at=now()))
            if not live:
                return
            time.sleep(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['supervise', 'actor'])
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--worker-id', type=int)
    args = parser.parse_args()
    if args.action == 'supervise':
        supervise(args.control.resolve())
    else:
        actor(args.control.resolve(), args.worker_id)
