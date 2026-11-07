"""Adopt live question workers and add capacity without interrupting model calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def identity(pid):
    try:
        fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except FileNotFoundError:
        return None


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_manifest(root):
    entries = read(root / 'frozen_hashes.json')
    def check(item):
        name, expected = item
        if sha(root / name) != expected:
            raise ValueError('Frozen artifact changed: ' + str(root / name))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(check, entries.items()))


def worker_health(row, status, process_identity=identity):
    alive = process_identity(row['pid']) == row['identity']
    if not alive and status.get('stage') != 'complete':
        return 'Worker exited without complete status: ' + str(row['id'])
    if status.get('stage') == 'failed':
        return status.get('error', 'Worker failed: ' + str(row['id']))
    return None


def handoff_supervisor(old, rows, process_identity=identity, send_signal=os.kill):
    if process_identity(old['pid']) != old['identity']:
        raise ValueError('Original controller identity changed')
    for row in rows:
        if process_identity(row['pid']) != row['identity']:
            raise ValueError('Adopted worker identity changed')
    # Signal exactly the supervisor PID, never its group or model workers.
    send_signal(old['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while process_identity(old['pid']) is not None:
        if time.monotonic() > deadline:
            raise RuntimeError('Old supervisor did not release ownership')
        time.sleep(.1)


def queue_snapshot(out, control, rows, stage='running'):
    workers = {}
    for row in rows:
        f = control / 'workers' / str(row['id']) / 'status.json'
        if f.exists():
            workers[str(row['id'])] = read(f)
    return dict(state=stage, controller_pid=os.getpid(), worker_pids=[r['pid'] for r in rows],
                worker_pid=rows[0]['pid'] if rows else None, workers=workers,
                scheduler=str(control), effective_worker_count=len(rows), updated_at=now())


def pool_policy(control):
    if (control/'config.json').exists():
        policy=read(control/'config.json')
        if policy.get('scheme') not in ('M','O','P'):
            raise ValueError('Unsupported generic pool scheme')
        return 'generic',policy
    return 'mn',read(control/'policy.json')


def worker_command(control,out,wid,kind,python=None):
    if kind=='generic':
        return [python or sys.executable,'-u',str(control/'runner.py'),'worker',
                '--control',str(control),'--worker-id',str(wid)]
    return [python or sys.executable,'-u',str(control/'runtime/script/run_parallel_scene_scheduler.py'),
            'worker','--out',str(out),'--control',str(control),'--worker-id',str(wid)]


def refresh_scaled_report(out,control,module,cfg,count):
    if cfg.get('pool_kind','mn')=='generic':
        # The generic reporter already owns claims.lock internally.
        module.report(out,control,read(control/'config.json'))
        with module.locked(control):
            report=read(out/'report.json')
            report.update(parallel_worker_count=count,parallel_live_extension=cfg['scale_path'],
                          parallel_original_worker_count=cfg['frozen_policy_workers'])
            write(out/'report.json',report)
    else:
        with module.locked(control):
            report=module.refresh_report(out,control)
            report.update(parallel_worker_count=count,parallel_live_extension=cfg['scale_path'],
                          parallel_original_worker_count=cfg['frozen_policy_workers'])
            write(out/'report.json',report)
    return report


def write_queue(out,control,snapshot,kind):
    write(out/'queue_status.json',snapshot)
    if kind=='generic':
        # Existing monitoring follows this control directory, not only out/.
        write(control/'queue_status.json',snapshot)
        write(control/'status.json',snapshot)


def prepare(out, control, scale, count):
    if count < 1 or count > 4:
        raise ValueError('Add one through four bounded workers')
    status = read(out / 'queue_status.json')
    if status['state'] != 'running' or Path(status['scheduler']).resolve() != control:
        raise ValueError('Require the active parallel scheduler')
    if (control / 'failure.json').exists():
        raise ValueError('Pool already failed; scaling must not retry it')
    verify_manifest(out)
    verify_manifest(control)
    old_pid = status['controller_pid']
    if identity(old_pid) is None:
        raise ValueError('Original controller is not alive')
    cmd = Path('/proc', str(old_pid), 'cmdline').read_bytes().split(b'\0')
    if b'supervise' not in cmd or str(control).encode() not in cmd:
        raise ValueError('Original controller command does not match')
    adopted = []
    for wid, info in sorted(status['workers'].items(), key=lambda item: int(item[0])):
        pid = info['pid']
        token = identity(pid)
        if token is None:
            raise ValueError('Existing worker is not alive')
        adopted.append(dict(id=int(wid), pid=pid, identity=token, adopted=True))
    if {r['pid'] for r in adopted} != set(status['worker_pids']):
        raise ValueError('Existing worker inventory differs')
    next_id = max(r['id'] for r in adopted) + 1
    added = list(range(next_id, next_id + count))
    if any((control / 'workers' / str(i)).exists() for i in added):
        raise ValueError('An additional worker was already attempted')
    scale.mkdir()
    shutil.copy2(__file__, scale / 'runner.py')
    shutil.copy2(Path(__file__).with_name('preserve_parallel_call_paths.py'), scale / 'alias_helper.py')
    published = list((out / 'runs').glob('*/result.json'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        hashes = dict(pool.map(lambda f: (str(f), sha(f)), published))
    write(scale / 'published_before.json', hashes)
    kind,policy = pool_policy(control)
    write(scale / 'config.json', dict(out=str(out), control=str(control),
        old_controller=dict(pid=old_pid, identity=identity(old_pid)), adopted_workers=adopted,
        new_worker_ids=added, effective_workers=len(adopted)+count,
        pool_kind=kind,scale_path=str(scale),
        resource_policy=policy['resource_policy'], frozen_policy_workers=policy['workers'],
        user_request='继续多线程抽取；显存够就增加并行', at=now(),
        method_scope='Same frozen model, prompts, schemas, batch4, sources and per-question budgets',
        handoff_scope='Terminate only the old supervisor PID; adopt its live workers without signalling them',
        failure_policy='One shared failure marker; stop new claims, drain in-flight work, no retry'))
    write(scale / 'preflight.json', dict(passed=True, published_results=len(hashes),
        frozen_manifests_verified=True, original_workers_alive=True, new_workers=count, at=now()))
    write(scale / 'frozen_hashes.json', {f.name: sha(f) for f in scale.iterdir() if f.is_file()})
    return scale


def supervise(scale):
    verify_manifest(scale)
    cfg = read(scale / 'config.json')
    out, control = Path(cfg['out']), Path(cfg['control'])
    # Import the running experiment's exact frozen scheduling/report code.
    kind=cfg.get('pool_kind','mn')
    cfg.setdefault('scale_path',str(scale))
    import importlib.util
    if kind=='generic':
        spec=importlib.util.spec_from_file_location('scaled_generic_pool',control/'runner.py')
        frozen=importlib.util.module_from_spec(spec);spec.loader.exec_module(frozen)
        frozen.frozen_modules(out)
        aliases=None
    else:
        sys.path.insert(0, str(control / 'runtime'))
        from script import run_parallel_scene_scheduler as frozen
        spec = importlib.util.spec_from_file_location('scale_aliases', scale / 'alias_helper.py')
        aliases = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(aliases)
    rows = list(cfg['adopted_workers'])
    children = []
    with (scale / 'controller.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (scale / 'launch.json').exists():
            raise ValueError('Scale controller already attempted')
        if kind=='generic':verify_manifest(control)
        else:frozen.verify(control)
        if (control / 'failure.json').exists():
            raise ValueError('Pool failed before handoff')
        old = cfg['old_controller']
        if identity(old['pid']) != old['identity']:
            raise ValueError('Original controller identity changed')
        for row in rows:
            if identity(row['pid']) != row['identity']:
                raise ValueError('Adopted worker identity changed')
        # No process-group signal: original model/worker processes keep running.
        write(scale / 'launch.json', dict(pid=os.getpid(), phase='handoff', at=now()))
        handoff_supervisor(old, rows)
        with (out / 'controller.lock').open('a') as ownership:
            fcntl.flock(ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            write_queue(out,control,queue_snapshot(out,control,rows),kind)
            try:
                helper=None
                if aliases is not None:
                    previous = read(control / 'origin_aliases_launch.json')
                    deadline = time.monotonic() + 10
                    while identity(previous['pid']) is not None:
                        if time.monotonic() > deadline:
                            raise RuntimeError('Previous alias helper did not exit')
                        time.sleep(.1)
                    shutil.move(control / 'origin_aliases_launch.json', scale / 'origin_aliases_previous_launch.json')
                    if (control / 'origin_aliases_status.json').exists():
                        shutil.move(control / 'origin_aliases_status.json', scale / 'origin_aliases_previous_status.json')
                    helper = aliases.launch(out, control, os.getpid(), helper=scale / 'alias_helper.py')
                    write(scale / 'alias_launch.json', helper)
                env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                    TOKENIZERS_PARALLELISM='false', CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    OMP_NUM_THREADS='4', MKL_NUM_THREADS='4')
                for wid in cfg['new_worker_ids']:
                    folder = control / 'workers' / str(wid)
                    folder.mkdir()
                    command = worker_command(control,out,wid,kind)
                    with (folder / 'worker.log').open('a') as log:
                        child = subprocess.Popen(command, cwd=control if kind=='generic' else control/'runtime', env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                    children.append(child)
                    token = identity(child.pid)
                    if token is None:
                        raise RuntimeError('New worker exited immediately')
                    rows.append(dict(id=wid, pid=child.pid, identity=token, adopted=False, command=command))
                    write(folder / 'status.json', dict(stage='starting', pid=child.pid, at=now()))
                write(scale / 'launch.json', dict(pid=os.getpid(), identity=identity(os.getpid()),
                    phase='running', workers=rows, alias_helper=helper['pid'] if helper else None, at=now()))
                write(scale / 'ready.json', dict(ready=True, workers=rows, at=now(),
                    adopted_workers_uninterrupted=True, method_runtime_unchanged=True))
                while True:
                    # Reap own children, while adopted children use PID/starttime checks.
                    for child in children:
                        child.poll()
                    snapshot = queue_snapshot(out, control, rows)
                    for row in rows:
                        state = snapshot['workers'].get(str(row['id']), {})
                        reason = worker_health(row, state)
                        if reason and not (control / 'failure.json').exists():
                            with frozen.locked(control):
                                if not (control / 'failure.json').exists():
                                    write(control / 'failure.json', dict(worker_id=row['id'], message=reason, at=now()))
                    if helper and identity(helper['pid']) != helper['process_identity']:
                        raise RuntimeError('Alias helper exited unexpectedly')
                    alive = any(identity(r['pid']) == r['identity'] for r in rows)
                    report=refresh_scaled_report(out,control,frozen,cfg,len(rows))
                    if not alive:
                        snapshot['state'] = 'complete' if report['complete'] and not (control / 'failure.json').exists() else 'failed'
                    write_queue(out,control,snapshot,kind)
                    active = [v for v in snapshot['workers'].values() if v.get('stage') == 'query']
                    if active:
                        first = min(active, key=lambda v: v['index'])
                        write(out / 'progress.json', dict(stage='parallel_queries', index=first['index'],
                            question_id=first['question_id'], active_queries=active, updated_at=now()))
                    write(scale / 'status.json', dict(state=snapshot['state'], pid=os.getpid(),
                        effective_workers=len(rows), workers=snapshot['workers'], updated_at=now()))
                    if not alive:
                        if aliases:aliases.preserve(out,control)
                        return
                    time.sleep(5)
            except BaseException as exc:
                with frozen.locked(control):
                    if not (control / 'failure.json').exists():
                        write(control / 'failure.json', dict(message=str(exc), type=type(exc).__name__, at=now()))
                write_queue(out,control,dict(queue_snapshot(out,control,rows,'failed'),error=str(exc)),kind)
                write(scale / 'failure.json', dict(message=str(exc), type=type(exc).__name__, at=now()))
                raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'supervise'])
    parser.add_argument('--out', type=Path)
    parser.add_argument('--control', type=Path)
    parser.add_argument('--scale', type=Path, required=True)
    parser.add_argument('--add-workers', type=int, default=2)
    args = parser.parse_args()
    if args.mode == 'prepare':
        print(prepare(args.out.resolve(), args.control.resolve(), args.scale.resolve(), args.add_workers))
    else:
        supervise(args.scale.resolve())
