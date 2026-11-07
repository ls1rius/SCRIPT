"""Explicit GPU withdrawal with durable call-prefix preservation and live adoption."""
import argparse
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import signal
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script.scale_live_parallel_pool import read,write,now,identity,sha,queue_snapshot,worker_health


def descendants(pid):
    from script.gpu_shared_queue import process_snapshot
    processes=process_snapshot();found={pid}
    while True:
        expanded=found|{p for p,row in processes.items() if row['parent'] in found}
        if expanded==found:break
        found=expanded
    return {p:processes[p]['start'] for p in found if p in processes}


def journal_prefix(path):
    import json
    rows=[]
    if not path.exists():return rows
    for line in path.read_text().splitlines():
        if not line.strip():continue
        try:rows.append(json.loads(line))
        except json.JSONDecodeError:break
    return rows


def stop_and_checkpoint(out,control,row,target):
    import json
    pid=row['pid']
    if identity(pid)!=row['identity']:raise ValueError('Worker PID changed before withdrawal')
    os.kill(pid,signal.SIGSTOP)
    deadline=time.monotonic()+2
    while Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[0]!='T':
        if time.monotonic()>deadline:raise RuntimeError('Worker did not stop')
        time.sleep(.001)
    children=descendants(pid)
    with (control/'claims.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        claims=read(control/'claims.json')
        active=[(q,v) for q,v in claims.items() if v.get('pid')==pid and v['state']=='running']
        if len(active)>1:raise ValueError('Worker has multiple running claims')
        info=dict(worker=row,at=now(),processes=children,question_id=None,regions={},pending=[],
                  interrupted_budget_seconds=0.,successful_seconds=0.)
        if active:
            uid,claim=active[0];info.update(question_id=uid,index=claim['index'])
            run=control/'workers'/str(row['id'])/'view/runs'/uid if row['scheme']=='O' else control/'attempts'/uid/'run'
            info['original_attempt']=str(run)
            for f in run.glob('**/calls.jsonl'):
                rows=journal_prefix(f)
                if any(c.get('error') for c in rows):raise ValueError('Migration cannot hide an existing failed call')
                info['regions'][str(f.parent.relative_to(run))]=rows
                info['successful_seconds']+=sum(c['actual_cost'] for c in rows)
            for f in run.rglob('*.pending.json'):
                pending=read(f);pending.update(path=str(f),created_unix=f.stat().st_mtime,
                    observed_elapsed_seconds=max(0,time.time()-f.stat().st_mtime))
                info['pending'].append(pending)
                # Retain the full reserved allowance conservatively, not an
                # invented exact GPU measurement or a free interrupted call.
                info['interrupted_budget_seconds']+=pending['reservation']
            stage=out/'source_staging'/uid
            if stage.exists():
                archived=target/'source_staging'/uid;archived.parent.mkdir(parents=True,exist_ok=True)
                stage.rename(archived);info['archived_source_staging']=str(archived)
            claims[uid].update(state='migration_paused',migration=str(target),interrupted_at=now())
            write(control/'claims.json',claims)
        write(target/'checkpoints'/f'{row["scheme"]}-{row["id"]}.json',info)
    # Terminate only these verified owned processes. Their incomplete raw files
    # remain archived; no Python exception is converted into a pool failure.
    for child,token in children.items():
        if child!=pid and identity(child)==token:os.kill(child,signal.SIGTERM)
    if identity(pid)==row['identity']:
        os.kill(pid,signal.SIGTERM)
        os.kill(pid,signal.SIGCONT)
    for child,token in children.items():
        end=time.monotonic()+5
        while identity(child)==token and time.monotonic()<end:time.sleep(.05)
        if identity(child)==token:raise RuntimeError('Withdrawn owned process remained alive: '+str(child))
    write(control/'workers'/str(row['id'])/'status.json',dict(stage='migrated_off_gpu',pid=pid,
        gpu=row['gpu'],question_id=info['question_id'],model_released=True,at=now(),checkpoint=str(target/'checkpoints'/f'{row["scheme"]}-{row["id"]}.json')))
    return info


def prepare(folder):
    if folder.exists():raise ValueError('Fresh migration directory required')
    schemes={'P':'P-N-source-300-20261104-r4','O':'O-Oplus-N-question-only-300-source-bound-20261104-r4'}
    configs={}
    for scheme,name in schemes.items():
        out=(ROOT/'workdirs'/name).resolve();status=read(out/'queue_status.json');control=Path(status['scheduler'])
        if status['state']=='complete':continue
        if status['state']!='running' or (control/'failure.json').exists():raise ValueError('Require healthy live pool')
        rows=[]
        for wid,value in status['workers'].items():
            if value['stage']!='query':raise ValueError('Inspect non-query worker before migration')
            rows.append(dict(id=int(wid),pid=value['pid'],identity=identity(value['pid']),gpu=value['gpu'],scheme=scheme))
        configs[scheme]=dict(out=str(out),control=str(control),old_controller=dict(pid=status['controller_pid'],identity=identity(status['controller_pid'])),rows=rows)
    folder.mkdir(parents=True)
    write(folder/'config.json',dict(schemes=configs,allowed_gpus=['0','1'],at=now(),
        scope='User-requested immediate GPU2/3 withdrawal; preserve successful calls and conservatively charge interrupted reservations'))


def supervise(folder):
    cfg=read(folder/'config.json');schemes=cfg['schemes'];rows_by_scheme={};checkpoints=[]
    with ExitStack() as stack:
        for scheme,c in schemes.items():
            old=c['old_controller']
            if identity(old['pid'])!=old['identity']:raise ValueError('Controller identity changed')
            os.kill(old['pid'],signal.SIGTERM)
        for scheme,c in schemes.items():
            old=c['old_controller'];end=time.monotonic()+10
            while identity(old['pid'])==old['identity']:
                if time.monotonic()>end:raise RuntimeError('Old controller remained alive')
                time.sleep(.05)
            out=Path(c['out']);control=Path(c['control'])
            lock=stack.enter_context((out/'controller.lock').open('a'));fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            kept=[]
            for row in c['rows']:
                if row['gpu'] in cfg['allowed_gpus']:kept.append(row)
                else:checkpoints.append(stop_and_checkpoint(out,control,row,folder))
            rows_by_scheme[scheme]=kept
        write(folder/'released.json',dict(at=now(),checkpoints=checkpoints,kept=rows_by_scheme,
            allowed_gpus=cfg['allowed_gpus'],pid=os.getpid()))
        while True:
            additions=read(folder/'additional_workers.json') if (folder/'additional_workers.json').exists() else {}
            done=True
            for scheme,c in schemes.items():
                out=Path(c['out']);control=Path(c['control'])
                rows=rows_by_scheme[scheme]+additions.get(scheme,[])
                snapshot=queue_snapshot(out,control,rows)
                for row in rows:
                    state=snapshot['workers'].get(str(row['id']),{})
                    reason=worker_health(row,state)
                    if reason and not (control/'failure.json').exists():
                        write(control/'failure.json',dict(message=reason,at=now(),worker_id=row['id']))
                if scheme=='P':
                    from script.run_p_full_experiment import report
                else:
                    from script.o_composition_context_eval import report
                with (control/'claims.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX);result=report(out)
                    result.update(parallel_worker_count=len(rows),allowed_gpu_indices=['0','1'],gpu_migration=str(folder),
                        scheduler_failure=read(control/'failure.json') if (control/'failure.json').exists() else None)
                    write(out/'report.json',result)
                alive=any(identity(row['pid'])==row['identity'] for row in rows)
                pending=[v for v in read(control/'claims.json').values() if v['state']=='migration_paused']
                if not alive and not pending:
                    snapshot['state']='complete' if result['complete'] and not (control/'failure.json').exists() else 'failed'
                if snapshot['state']!='complete':done=False
                snapshot.update(allowed_gpu_indices=['0','1'],migration_paused_questions=len(pending),gpu_migration=str(folder))
                for path in [out/'queue_status.json',control/'queue_status.json',control/'status.json']:write(path,snapshot)
                active=[s for s in snapshot['workers'].values() if s.get('stage')=='query']
                if active:
                    first=min(active,key=lambda v:v['index']);write(out/'progress.json',dict(stage='parallel_queries',index=first['index'],question_id=first['question_id'],active_queries=active,updated_at=now()))
            write(folder/'status.json',dict(pid=os.getpid(),state='complete' if done else 'running',updated_at=now()))
            if done:return
            time.sleep(5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['prepare','supervise']);parser.add_argument('--folder',type=Path,required=True)
    args=parser.parse_args();(prepare if args.mode=='prepare' else supervise)(args.folder.resolve())
