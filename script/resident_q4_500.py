"""Keep a Q4 model and its GPU lease for the entire remaining question queue.

The frozen source/Q4 routines, inputs and budgets are used verbatim. Only the
worker lifecycle and an explicit, validated stage-configuration IPC are changed.
"""
import argparse
import copy
from contextlib import ExitStack, contextmanager
import fcntl
import importlib
import inspect
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
if (HERE/'config.json').exists():
    import json
    sys.path.insert(0, str(Path(json.loads((HERE/'config.json').read_text())['out'])/'runtime'))
else:
    sys.path.insert(0, str(HERE.parent))
from script.scale_live_parallel_pool import read, write, sha, identity, now, verify_manifest

CONTROL = '__resident_stage_config__'


class QueueDrained(Exception):
    pass


def compatible(a, b):
    """Only the already frozen allocator and extraction batch may differ."""
    a, b = copy.deepcopy(a), copy.deepcopy(b)
    for cfg in (a, b):
        cfg['models'].pop('cuda_allocator_limit_mib', None)
        cfg['query'].pop('field_extraction_batch_size', None)
    if a != b:
        raise ValueError('Resident configuration changed model semantics')


def process_code(original):
    before = '        operation, inputs, deadline = message\n'
    after = before + '''        if operation == '__resident_stage_config__':
            from resident_q4_500 import compatible
            compatible(cfg, inputs['config'])
            cfg = inputs['config']
            backend.cfg = cfg
            import torch
            cap = int(cfg['models']['cuda_allocator_limit_mib'])
            total = torch.cuda.get_device_properties(0).total_memory
            torch.cuda.set_per_process_memory_fraction(cap * 1024**2 / total, 0)
            allocator_limit_applied = True
            connection.send((True, dict(configured=True, pid=__import__('os').getpid(),
                allocator_mib=cap, loaded=sorted(backend.loaded))))
            continue
'''
    if original.count(before) != 1:
        raise ValueError('Unexpected frozen model-process layout')
    return original.replace(before, after, 1)


def resident_process(connection, cfg):
    from script_core import budget
    # The runner directory is explicitly importable in multiprocessing children.
    sys.path.insert(0, str(HERE))
    scope = dict(vars(budget))
    exec(compile((HERE/'model_process.py').read_text(), str(HERE/'model_process.py'), 'exec'), scope)
    scope['_worker'](connection, cfg)


class ResidentSession:
    def __init__(self, out, folder, status, acquire=None, factory=None):
        from script.gpu_queue import acquire as gpu_acquire
        from script_core.budget import ModelWorker
        self.out, self.folder, self.status = out, folder, status
        self.acquire, self.factory = acquire or gpu_acquire, factory or ModelWorker
        self.stack = ExitStack()
        self.backend = self.gpu = self.kind = None
        self.loads = 0

    def close(self):
        try:
            if self.backend is not None:
                self.backend.close(graceful=True)
        finally:
            self.stack.close()
            os.environ.pop('VIDEOAGENT_GPU_LEASE', None)

    @contextmanager
    def stage(self, kind):
        cfg = read(self.out/('qa_config.json' if kind == 'q4' else 'source_config.json'))
        if self.backend is None:
            def waiting(d):
                if (self.out/'failure.json').exists():
                    raise RuntimeError('Pool halted after failure')
                if not remaining(self.out) and not owned(self.out, self.folder.name):
                    raise QueueDrained()
                self.status('waiting_for_gpu', kind=kind, resident=True, **d)
            policy = dict(preference=['0','1','2','3'], minimum_free_mib=32768,
                          stable_observations=3, admission_mode='memory_shared', maximum_utilization=100)
            self.gpu, _ = self.stack.enter_context(self.acquire(policy, waiting))
            os.environ['CUDA_VISIBLE_DEVICES'] = self.gpu['uuid']
            os.environ['VIDEOAGENT_GPU_LEASE'] = self.gpu['reservation_id']
            self.backend = self.factory(cfg)
            self.status('loading', kind=kind, gpu=self.gpu['index'], resident=True)
            started = time.monotonic()
            details = self.backend.call('prepare_vlm', {}, 600)
            self.loads += 1
            write(self.folder/'loads'/f'{self.loads:04}.json', dict(kind=kind, seconds=time.monotonic()-started,
                  gpu=self.gpu, details=details, resident=True))
        elif self.kind != kind:
            compatible(self.backend.cfg, cfg)
            started = time.monotonic()
            details = self.backend.call(CONTROL, dict(config=cfg), 30)
            self.backend.cfg = cfg
            write(self.folder/'stage_switches'/f'{len(list((self.folder/"stage_switches").glob("*.json")))+1:05}.json',
                  dict(kind=kind, seconds=time.monotonic()-started, gpu=self.gpu, details=details, at=now()))
        self.kind = kind
        self.status('query', kind=kind, gpu=self.gpu['index'], resident=True,
                    model_pid=self.backend.process.pid)
        # Closing the context intentionally keeps both the process and lease.
        yield self.backend, cfg, self.gpu


def remaining(out):
    claims = read(out/'claims.json')
    return sum(uid not in claims for uid in read(out/'cohort.json'))


def owned(out, wid):
    return any(c.get('state') == 'running' and str(c['worker_id']) == str(wid)
               for c in read(out/'claims.json').values())


def generated_worker(original):
    start = original.index(' @contextmanager\n def stage_model(kind):')
    end = original.index('\n try:\n  # Original real Q4 gates', start)
    result = original[:start] + ' session=ResidentSession(out,folder,status)\n stage_model=session.stage\n' + original[end:]
    edits = [
        ('  while True:\n   item=None\n', '  while True:\n   if not work_remaining(out) and not owned(out,wid):break\n   item=None\n'),
        ('    item=claim(out,wid)\n', '    item=resident_claim(out,wid)\n'),
    ]
    # Resume a completed source without recomputing any model call.
    result = result.replace('    import torch\n', '    if not (out/\'source_runs\'/uid).exists():\n     import torch\n', 1)
    a = result.index('     import torch\n') + len('     import torch\n')
    b = result.index('\n   if item is None:break', a)
    result = result[:a] + '\n'.join(' '+l for l in result[a:b].split('\n')) + result[b:]
    for before, after in edits:
        if result.count(before) != 1:
            raise ValueError('Unexpected frozen question-worker layout')
        result = result.replace(before, after, 1)
    before = '     source_validated=True\n'
    after = '''     # Prove Q4 still passes after N with the SAME resident process.
     with stage_model('q4') as (probe_backend,probe_cfg,probe_gpu):
      status('validation',kind='resident_round_trip',gpu=probe_gpu['index'])
      validate_live(probe_backend,probe_cfg,folder/'validation/resident_q4')
     with stage_model('source') as (backend,cfg,gpu):pass
     source_validated=True
'''
    if result.count(before) != 1:raise ValueError('Unexpected source gate layout')
    result = result.replace(before, after, 1)
    result = result.replace(' except BaseException as e:\n', " except QueueDrained:\n  status('complete',reason='queue_drained')\n except BaseException as e:\n", 1)
    result += '\n finally:session.close()\n'
    compile(result, 'resident-worker', 'exec')
    return result


def resident_claim(out, wid):
    ext = importlib.import_module('script.extend_q4_500')
    with ext.locked(out):
        if (out/'failure.json').exists():
            return None
        for uid, row in read(out/'claims.json').items():
            if row.get('state') == 'running' and row['worker_id'] == wid:
                if not (out/'source_runs'/uid/'result.json').exists() or (out/'attempts'/uid).exists():
                    raise ValueError('Only a completed source with no Q4 attempt may resume')
                return row['index'], uid
    return ext.claim(out, wid)


def worker(control, wid):
    verify_manifest(control)
    cfg = read(control/'config.json'); out = Path(cfg['out'])
    ext = importlib.import_module('script.extend_q4_500')
    from script_core import budget
    budget._worker = resident_process
    scope = dict(vars(ext), ResidentSession=ResidentSession, QueueDrained=QueueDrained,
                 work_remaining=remaining, owned=owned, resident_claim=resident_claim)
    exec(compile((control/'question_worker.py').read_text(), str(control/'question_worker.py'), 'exec'), scope)
    scope['worker'](out, wid)


def safe_idle_snapshot(out, rows):
    from script.gpu_shared_queue import process_snapshot, allocations, locked_registry
    processes = process_snapshot(); usage = allocations()
    with locked_registry() as leases:
        for row in rows:
            if identity(row['pid']) != row['identity']:
                raise ValueError('Old worker identity changed')
            status = read(out/'workers'/str(row['id'])/'status.json')
            if status['stage'] != 'waiting_for_gpu':
                raise ValueError('Old worker has active work; defer handoff')
            descendants = {row['pid']}
            while True:
                more = descendants | {pid for pid,p in processes.items() if p['parent'] in descendants}
                if more == descendants:break
                descendants = more
            if any(u['pid'] in descendants for u in usage):
                raise ValueError('Old worker owns a GPU process')
            if any(l['pid'] in descendants for l in leases.values()):
                raise ValueError('Old worker owns a GPU lease')
    for base in ['attempts','source_attempts']:
        if list((out/base).glob('**/*.pending.json')):
            raise ValueError('Pending calls prohibit handoff')


def prepare(out, control):
    ext = importlib.import_module('script.extend_q4_500'); ext.verify(out)
    q = read(out/'queue_status.json')
    old = Path(q['autoscaler']); oldcfg = read(old/'config.json')
    rows = oldcfg['adopted_workers']
    if len(q['workers']) != len(rows) or (out/'failure.json').exists():
        raise ValueError('Unexpected pool state')
    safe_idle_snapshot(out, rows)
    compatible(read(out/'qa_config.json'), read(out/'source_config.json'))
    control.mkdir()
    write(control/'config.json', dict(out=str(out), control=str(control), old_controller=dict(pid=q['controller_pid'],
          identity=identity(q['controller_pid'])), old_workers=rows, initial_worker_ids=[8,9,10,11],
          maximum_workers=8, next_worker_ids=[12,13,14,15], allowed_gpu_indices=['0','1','2','3'],
          admission_mib=32768, source_allocator_mib=30720, q4_allocator_mib=22528, poll_seconds=10,
          keep_model_and_lease_between_stages_and_questions=True, at=now()))
    (control/'resident_q4_500.py').write_text(Path(__file__).read_text())
    (control/'question_worker.py').write_text(generated_worker(inspect.getsource(ext.worker)))
    from script_core import budget
    (control/'model_process.py').write_text(process_code(inspect.getsource(budget._worker)))
    write(control/'published_before.json', {str(p):sha(p) for p in (out/'runs').glob('*/result.json')})
    write(control/'queue_before.json', q)
    write(control/'claims_before.json', read(out/'claims.json'))
    write(control/'frozen_hashes.json', {p.name:sha(p) for p in control.iterdir() if p.is_file()})
    print('RESIDENT_PREPARED', control, flush=True)


def queue(out, control, rows, statuses, state='running', **kw):
    write(out/'queue_status.json', dict(state=state, controller_pid=os.getpid(), workers=statuses,
          effective_worker_count=len(rows), configured_maximum_worker_count=8, allowed_gpu_indices=['0','1','2','3'],
          autoscaler=str(control), resident_control=str(control), resident_models=True, updated_at=now(),
          error=read(out/'failure.json') if (out/'failure.json').exists() else None, **kw))


def gpu_snapshot():
    from script.gpu_queue import inventory
    from script.gpu_shared_queue import locked_registry,effective_free,process_snapshot,allocations
    with locked_registry() as leases:
        devices=inventory();free,active=effective_free(devices,leases,process_snapshot(),allocations())
        leases.clear();leases.update(active)
    return devices,free,active


def supervise(control):
    verify_manifest(control); cfg = read(control/'config.json'); out = Path(cfg['out'])
    ext = importlib.import_module('script.extend_q4_500'); ext.verify(out)
    old = cfg['old_controller']; originals = cfg['old_workers']; stopped=[]; transferred=False
    rows=[]; children={}
    env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
             CUDA_DEVICE_ORDER='PCI_BUS_ID',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
    with (control/'controller.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (control/'launch.json').exists():raise ValueError('Already launched')
        if identity(old['pid']) != old['identity']:raise ValueError('Old controller identity changed')
        try:
            # Own the claim lock before stopping a controller that also takes it.
            with ext.locked(out):
                for row in [old]+originals:
                    if identity(row['pid']) != row['identity']:raise ValueError('Process changed')
                    os.kill(row['pid'],signal.SIGSTOP);stopped.append(row)
                deadline=time.monotonic()+5
                while not all(Path('/proc',str(r['pid']),'stat').read_text().rsplit(')',1)[1].split()[0] in ['T','t'] for r in stopped):
                    if time.monotonic()>deadline:raise RuntimeError('Processes did not stop')
                    time.sleep(.02)
                safe_idle_snapshot(out,originals)
                claims=read(out/'claims.json')
                unfinished=[(u,r) for u,r in claims.items() if r['state']=='running']
                if len(unfinished)>1:raise ValueError('Unexpected in-flight claim count')
                for uid,row in unfinished:
                    if not (out/'source_runs'/uid/'result.json').exists() or (out/'attempts'/uid).exists():
                        raise ValueError('Unsafe phase to transfer claim')
                write(control/'handoff.json',dict(at=now(),stopped=stopped,unfinished=unfinished,
                      published={str(p):sha(p) for p in (out/'runs').glob('*/result.json')}))
                # Keep a live controller visible to existing monitor/training jobs.
                queue(out,control,[],read(out/'queue_status.json')['workers'],handoff=True)
                for uid,row in unfinished:
                    row.update(worker_id=8,pid=None,previous_owner=dict(worker_id=row['worker_id'],pid=row['pid']),
                               handoff_at=now(),resume_from='completed_source')
                write(out/'claims.json',claims)
                transferred=True
            for row in stopped:
                os.kill(row['pid'],signal.SIGTERM);os.kill(row['pid'],signal.SIGCONT)
            deadline=time.monotonic()+10
            while any(identity(r['pid']) is not None for r in stopped):
                if time.monotonic()>deadline:raise RuntimeError('Old processes did not exit')
                time.sleep(.1)
            for row in originals:
                s=read(out/'workers'/str(row['id'])/'status.json')
                write(control/'old_worker_status'/f'{row["id"]}.json',s)
                write(out/'workers'/str(row['id'])/'status.json',dict(s,stage='superseded',superseded_at=now(),resident_control=str(control)))
        except BaseException:
            for row in stopped:
                if identity(row['pid'])==row['identity']:os.kill(row['pid'],signal.SIGCONT)
            if transferred:ext.fail(out,'resident_handoff',RuntimeError('Resident handoff failed; see controller log'))
            raise
        with (out/'controller.lock').open('a') as ownership:
            fcntl.flock(ownership,fcntl.LOCK_EX|fcntl.LOCK_NB)
            write(control/'launch.json',dict(controller_pid=os.getpid(),identity=identity(os.getpid()),at=now()))
            def launch(wid):
                if (out/'workers'/str(wid)).exists():raise ValueError('Worker ID already used')
                command=[sys.executable,'-u',str(control/'resident_q4_500.py'),'worker','--control',str(control),'--worker-id',str(wid)]
                with (control/f'worker-{wid}.log').open('x') as log:
                    child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True)
                row=dict(id=wid,pid=child.pid,identity=identity(child.pid),command=command)
                rows.append(row);children[child.pid]=child;write(control/'workers_launch.json',rows)
                if wid==8:
                    with ext.locked(out):
                        claims=read(out/'claims.json')
                        for r in claims.values():
                            if r.get('state')=='running' and r['worker_id']==8:r['pid']=child.pid
                        write(out/'claims.json',claims)
            for wid in cfg['initial_worker_ids']:launch(wid)
            while True:
                statuses={str(r['id']):read(out/'workers'/str(r['id'])/'status.json') for r in rows
                          if (out/'workers'/str(r['id'])/'status.json').exists()}
                for r in rows:
                    children[r['pid']].poll();s=statuses.get(str(r['id']),{})
                    if (identity(r['pid'])!=r['identity'] and s.get('stage')!='complete') or s.get('stage')=='failed':
                        ext.fail(out,r['id'],RuntimeError(s.get('error','Resident worker exited without completion')))
                with ext.locked(out):
                    report=ext.build_report(out)
                    report.update(parallel_worker_count=len(rows),configured_maximum_worker_count=8,resident_models=True,
                                  resident_control=str(control),stage_switch_setup_seconds=sum(read(f)['seconds'] for f in (out/'workers').glob('*/stage_switches/*.json')),
                                  resident_round_trip_gate_seconds=sum(read(f)['cost_summary']['total'] for f in (out/'workers').glob('*/validation/resident_q4/functional.json')),
                                  resident_round_trip_reader_seconds=sum(read(f)['cost_summary']['total'] for f in (out/'workers').glob('*/validation/resident_q4/reader_pair/result.json')))
                    write(out/'report.json',report)
                # Waiting initial workers already cover every GPU. Extra workers
                # are added only after all current workers have acquired a lease.
                if len(rows)<8 and remaining(out) and not (out/'failure.json').exists() and len(statuses)==len(rows) and all(
                        s['stage'] in ['query','validation','loading'] for s in statuses.values()):
                    devices,free,leases=gpu_snapshot()
                    if max(free.values(),default=0)>=32768:
                        launch(next(w for w in cfg['next_worker_ids'] if w not in {r['id'] for r in rows}))
                live=any(identity(r['pid'])==r['identity'] for r in rows)
                state='running' if live else 'complete' if report['complete'] else 'failed'
                queue(out,control,rows,statuses,state,unclaimed_questions=remaining(out))
                if not live:return
                time.sleep(cfg['poll_seconds'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','supervise','worker'])
    p.add_argument('--control',type=Path,required=True);p.add_argument('--out',type=Path);p.add_argument('--worker-id',type=int)
    a=p.parse_args();control=a.control.resolve()
    if a.action=='prepare':
        sys.path.insert(0,str(a.out.resolve()/'runtime'));prepare(a.out.resolve(),control)
    elif a.action=='supervise':supervise(control)
    else:worker(control,a.worker_id)
