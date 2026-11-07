"""Watch GPU headroom and add bounded Q4 workers without changing frozen calls.

Adopt the original four workers; new workers may use two slots on each of GPUs
2/3 when >=32GiB effective headroom is stable. Original model-facing runtime,
batch sizes, question claims, source/Q4 budgets and successful results stay intact.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO=Path(__file__).resolve().parents[1]
_local_config=Path(__file__).resolve().parent/'config.json'
if _local_config.exists():
    sys.path.insert(0,str(Path(json.loads(_local_config.read_text())['out'])/'runtime'))
else:
    sys.path.insert(0,str(REPO))
from script.scale_live_parallel_pool import read,write,sha,identity,now,handoff_supervisor,verify_manifest


class IdlePoolDrain(Exception):
    """No owned in-flight question and every question has already been claimed."""


def frozen(out):
    sys.path.insert(0,str(out/'runtime'))
    return importlib.import_module('script.extend_q4_500')


def alive(row):
    return row.get('identity') is not None and identity(row['pid'])==row['identity']


def work_remaining(out):
    # Atomic claims snapshots are sufficient: claims are never removed/retried.
    claimed=read(out/'claims.json')
    return sum(u not in claimed for u in read(out/'cohort.json'))


def can_drain(out,current):
    claimed=read(out/'claims.json')
    uid=current.get('question_id')
    if uid and claimed.get(uid,{}).get('state')=='running':
        return False
    return all(u in claimed for u in read(out/'cohort.json'))


def generated_worker(original):
    edits=[
      ("resource=dict(preference=['0','1'],",'resource=dict(preference=SCALE_GPU_INDICES,'),
      ("   if (out/'failure.json').exists():raise RuntimeError('Pool halted after failure')\n",
       "   if (out/'failure.json').exists():raise RuntimeError('Pool halted after failure')\n   if can_drain(out,current):raise IdlePoolDrain('No unclaimed work and no owned in-flight case')\n"),
      ('  while True:\n   item=None\n','  while True:\n   if not work_remaining(out):break\n   item=None\n'),
      (' except BaseException as e:\n'," except IdlePoolDrain:\n  status('complete',reason='idle_after_all_questions_claimed')\n except BaseException as e:\n")]
    result=original
    for before,after in edits:
        if result.count(before)!=1:raise ValueError('Frozen worker layout changed; refuse ambiguous adaptation')
        result=result.replace(before,after,1)
    return result,edits


def prepare(out,scale):
    if scale.exists():raise ValueError('Scale path already exists')
    ext=frozen(out);ext.verify(out)
    q=read(out/'queue_status.json');old=read(out/'launch.json')
    if q['state']!='running' or q['controller_pid']!=old['controller_pid'] or (out/'failure.json').exists():
        raise ValueError('Require the healthy original500 scheduler')
    if identity(old['controller_pid'])!=old['identity']:raise ValueError('Controller identity mismatch')
    rows=old['workers']
    if len(rows)!=4 or {str(r['id']) for r in rows}!=set(q['workers']):raise ValueError('Unexpected existing worker inventory')
    for row in rows:
        if identity(row['pid'])!=row['identity']:raise ValueError('Original worker not alive')
    # Preserve the exact adopted controller/worker commands and birth identities.
    scale.mkdir(parents=True)
    original=inspect.getsource(ext.worker);adapted,edits=generated_worker(original)
    (scale/'original_worker.py').write_text(original)
    (scale/'worker.py').write_text(adapted)
    write(scale/'scheduling_edits.json',[dict(before=a,after=b) for a,b in edits])
    shutil.copy2(__file__,scale/'runner.py')
    # The frozen runner imports script.scale_live_parallel_pool from its runtime;
    # it therefore needs no mutable working-tree code at launch.
    write(scale/'config.json',dict(out=str(out),scale=str(scale),old_controller=dict(pid=old['controller_pid'],identity=old['identity']),
        adopted_workers=rows,maximum_workers=8,new_worker_ids=[4,5,6,7],new_gpu_indices=['2','3'],allowed_gpu_indices=['0','1','2','3'],
        original_worker_gpu_indices=['0','1'],maximum_new_workers_per_gpu=2,
        launch_minimum_effective_free_mib=32768,launch_stable_observations=3,poll_seconds=10,
        Q4_admission_mib=24576,Q4_allocator_mib=22528,N_admission_mib=32768,N_allocator_mib=30720,
        frozen_original_workers=4,frozen_runtime_manifest_sha256=sha(out/'frozen_hashes.json'),
        model_scope='Exact original worker calls, gates, model/config/schema/batch/input/budgets and shared claims; only GPU preference and idle-drain scheduling adapted',
        user_request='监听空间，有空间多线程/多卡加速',at=now()))
    paths=list((out/'runs').glob('*/result.json'))
    with ThreadPoolExecutor(max_workers=4) as pool:hashes=dict(pool.map(lambda p:(str(p),sha(p)),paths))
    write(scale/'published_before.json',hashes)
    write(scale/'queue_before.json',q)
    write(scale/'frozen_hashes.json',{p.name:sha(p) for p in scale.iterdir() if p.is_file()})
    verify_manifest(scale)
    print('AUTOSCALE_PREPARED',scale,'completed',len(hashes),flush=True)


def worker(scale,wid,gpu):
    verify_manifest(scale);cfg=read(scale/'config.json');out=Path(cfg['out']);ext=frozen(out)
    if wid not in cfg['new_worker_ids'] or gpu not in cfg['new_gpu_indices']:raise ValueError('Unapproved worker/GPU slot')
    scope=dict(vars(ext),IdlePoolDrain=IdlePoolDrain,SCALE_GPU_INDICES=[gpu],can_drain=can_drain,work_remaining=work_remaining)
    exec(compile((scale/'worker.py').read_text(),str(scale/'worker.py'),'exec'),scope)
    scope['worker'](out,wid)


def gpu_snapshot():
    from script.gpu_queue import inventory
    from script.gpu_shared_queue import locked_registry,effective_free,process_snapshot,allocations
    with locked_registry() as leases:
        devices=inventory();free,active=effective_free(devices,leases,process_snapshot(),allocations())
        leases.clear();leases.update(active)
    return devices,free,active


def launch_candidate(cfg,rows,statuses,devices,free,leases,stable,remaining,failed=False):
    """Pure bounded admission decision; the worker also acquires an atomic lease."""
    if failed or not remaining or len(rows)>=cfg['maximum_workers']:return None
    candidates=[]
    for gpu in cfg['new_gpu_indices']:
        assigned=[r for r in rows if not r.get('adopted',False) and r.get('assigned_gpu')==gpu]
        # IDs are never reused; a finished extra slot is not silently retried.
        if len(assigned)>=cfg['maximum_new_workers_per_gpu']:continue
        effective=free.get(gpu,0)
        for row in assigned:
            if statuses.get(str(row['id']),{}).get('stage') in ['complete','failed']:continue
            if not any(l['pid']==row['pid'] for l in leases.values()):
                effective-=cfg['launch_minimum_effective_free_mib']
        stable[gpu]=stable.get(gpu,0)+1 if effective>=cfg['launch_minimum_effective_free_mib'] else 0
        if stable[gpu]>=cfg['launch_stable_observations']:candidates.append((effective,gpu))
    if not candidates:return None
    gpu=max(candidates)[1];stable[gpu]=0
    return gpu


def queue(out,scale,cfg,rows,statuses,stage='running',**extra):
    write(out/'queue_status.json',dict(state=stage,controller_pid=os.getpid(),workers=statuses,
        effective_worker_count=len(rows),configured_maximum_worker_count=cfg['maximum_workers'],
        allowed_gpu_indices=cfg['allowed_gpu_indices'],worker_gpu_policies={str(r['id']):
            cfg['original_worker_gpu_indices'] if r.get('adopted') else [r['assigned_gpu']] for r in rows},
        autoscaler=str(scale),updated_at=now(),error=read(out/'failure.json') if (out/'failure.json').exists() else None,**extra))


def supervise(scale):
    verify_manifest(scale);cfg=read(scale/'config.json');out=Path(cfg['out']);ext=frozen(out);ext.verify(out)
    rows=[dict(r,adopted=True) for r in cfg['adopted_workers']];children={};stable={};last_observation={}
    env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
        CUDA_DEVICE_ORDER='PCI_BUS_ID',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
    with (scale/'controller.lock').open('a') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (scale/'launch.json').exists():raise ValueError('Autoscale controller already launched')
        q=read(out/'queue_status.json')
        if q['controller_pid']!=cfg['old_controller']['pid'] or (out/'failure.json').exists():raise ValueError('Scheduler changed before handoff')
        for row in rows:
            if identity(row['pid'])!=row['identity']:raise ValueError('Original worker identity changed before handoff')
        write(scale/'launch.json',dict(controller_pid=os.getpid(),identity=identity(os.getpid()),phase='handoff',at=now()))
        # Publish the live successor first: alert/training watchers keep a live PID
        # across ownership transfer. Only the old supervisor PID receives SIGTERM.
        queue(out,scale,cfg,rows,q['workers'],handoff=True)
        handoff_supervisor(cfg['old_controller'],rows)
        with (out/'controller.lock').open('a') as ownership:
            fcntl.flock(ownership,fcntl.LOCK_EX|fcntl.LOCK_NB)
            write(scale/'launch.json',dict(controller_pid=os.getpid(),identity=identity(os.getpid()),phase='watching',
                adopted_workers=rows,at=now()))
            while True:
                statuses={str(r['id']):read(out/'workers'/str(r['id'])/'status.json') for r in rows
                          if (out/'workers'/str(r['id'])/'status.json').exists()}
                for row in rows:
                    if row['pid'] in children:children[row['pid']].poll()
                    status=statuses.get(str(row['id']),{})
                    worker_alive=alive(row)
                    if (not worker_alive and status.get('stage') not in ['complete','failed']) or status.get('stage')=='failed':
                        ext.fail(out,row['id'],RuntimeError(status.get('error','Worker exited without terminal status')))
                remaining=work_remaining(out)
                try:
                    devices,free,leases=gpu_snapshot()
                    selected=launch_candidate(cfg,rows,statuses,devices,free,leases,stable,remaining,(out/'failure.json').exists())
                    last_observation=dict(devices=devices,effective_free_mib=free,stable=dict(stable),at=now())
                except (OSError,ValueError,subprocess.SubprocessError) as e:
                    # A failed inventory read never authorizes a new worker.
                    selected=None;stable.clear();last_observation=dict(inventory_error=str(e),at=now())
                if selected is not None:
                    wid=next(w for w in cfg['new_worker_ids'] if w not in {r['id'] for r in rows})
                    if (out/'workers'/str(wid)).exists():raise ValueError('Extra worker path already attempted')
                    command=[sys.executable,'-u',str(scale/'runner.py'),'worker','--scale',str(scale),'--worker-id',str(wid),'--gpu',selected]
                    with (scale/f'worker-{wid}.log').open('x') as log:
                        child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True)
                    row=dict(id=wid,pid=child.pid,identity=identity(child.pid),assigned_gpu=selected,adopted=False,command=command,at=now())
                    rows.append(row);children[child.pid]=child
                    write(scale/'workers_launch.json',rows)
                    print('AUTOSCALE_WORKER_STARTED',wid,child.pid,selected,flush=True)
                with ext.locked(out):
                    report=ext.build_report(out)
                    report.update(parallel_worker_count=len(rows),configured_maximum_worker_count=cfg['maximum_workers'],
                        autoscaler=str(scale),allowed_gpu_indices=cfg['allowed_gpu_indices'])
                    ext.write(out/'report.json',report)
                live=any(alive(r) for r in rows)
                state='running' if live else 'complete' if report['complete'] else 'failed'
                queue(out,scale,cfg,rows,statuses,state,unclaimed_questions=remaining,capacity_watch=last_observation)
                write(scale/'status.json',dict(state=state,controller_pid=os.getpid(),worker_count=len(rows),maximum_workers=cfg['maximum_workers'],
                    unclaimed_questions=remaining,capacity_watch=last_observation,updated_at=now()))
                if not live:
                    write(scale/'completed.json',dict(state=state,completed=report['completed'],errors=report['errors'],at=now()))
                    return
                time.sleep(cfg['poll_seconds'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','worker','supervise']);p.add_argument('--out',type=Path)
    p.add_argument('--scale',type=Path,required=True);p.add_argument('--worker-id',type=int);p.add_argument('--gpu');a=p.parse_args();scale=a.scale.resolve()
    if a.action=='prepare':prepare(a.out.resolve(),scale)
    elif a.action=='worker':worker(scale,a.worker_id,a.gpu)
    else:supervise(scale)
