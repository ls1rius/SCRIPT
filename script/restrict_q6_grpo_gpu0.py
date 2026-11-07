"""Adopt the resident GRPO learner; drain and release every other worker safely."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time


def configure(control):
    cfg=json.loads((control/'config.json').read_text())
    if cfg['allowed_gpu_indices']!=['0'] or cfg['learner']['id']!=0:
        raise ValueError('This controller permits only the existing GPU0 learner')
    sys.path.insert(0,cfg['runtime'])
    return cfg


def hold_pending(jobs):
    return {key:dict(row,state='held_for_gpu0_release') if row['state']=='pending' else dict(row)
            for key,row in jobs.items()}


def restore_pending(jobs):
    return {key:dict(row,state='pending') if row['state']=='held_for_gpu0_release' else dict(row)
            for key,row in jobs.items()}


def running_jobs(jobs,wid):
    return [key for key,row in jobs.items() if row['state']=='running' and row.get('worker_id')==wid]


def process_alive(row):
    from script.scale_live_parallel_pool import identity
    return identity(row['pid'])==row['identity']


def terminate_idle(row):
    """Caller owns jobs.lock and has ruled out in-flight model calls."""
    from script.scale_live_parallel_pool import identity
    from script.gpu_shared_queue import process_snapshot,locked_registry
    snapshot=process_snapshot();descendants={row['pid']}
    while True:
        expanded=descendants|{pid for pid,p in snapshot.items() if p['parent'] in descendants}
        if expanded==descendants:break
        descendants=expanded
    owned=[dict(pid=pid,identity=snapshot[pid]['start']) for pid in descendants if pid in snapshot]
    for process in owned:
        if identity(process['pid'])==process['identity']:
            os.kill(process['pid'],signal.SIGTERM)
            if process['pid']==row['pid']:os.kill(process['pid'],signal.SIGCONT)
    deadline=time.monotonic()+10
    while any(identity(p['pid'])==p['identity'] for p in owned):
        if time.monotonic()>deadline:
            for process in owned:
                if identity(process['pid'])==process['identity']:os.kill(process['pid'],signal.SIGKILL)
            break
        time.sleep(.1)
    with locked_registry() as leases:
        for key,lease in list(leases.items()):
            if lease['pid']==row['pid'] and lease['start']==row['identity']:leases.pop(key)
    return owned


def retire_if_idle(out,row):
    from script.scale_live_parallel_pool import read,write,now
    from script.q6_parallel_rl import locked
    with locked(out):
        jobs=read(out/'jobs.json')
        if running_jobs(jobs,row['id']):return None
        status=read(out/'workers'/str(row['id'])/'status.json')
        if not process_alive(row):
            if status['stage']=='released_by_user':return status
            raise RuntimeError('Worker exited before controlled retirement')
        if status['stage'] not in ['waiting_for_gpu','actor_ready','rollout']:return None
        os.kill(row['pid'],signal.SIGSTOP)
        # Recheck after stopping the parent: it may have entered a setup/gate call.
        status=read(out/'workers'/str(row['id'])/'status.json')
        if status['stage'] not in ['waiting_for_gpu','actor_ready','rollout']:
            os.kill(row['pid'],signal.SIGCONT);return None
        if running_jobs(read(out/'jobs.json'),row['id']):
            os.kill(row['pid'],signal.SIGCONT);return None
        processes=terminate_idle(row)
        final=dict(stage='released_by_user',pid=row['pid'],previous_stage=status['stage'],
                   reason='GPU0-only user policy; completed in-flight job before release',
                   retired_processes=processes,at=now())
        write(out/'workers'/str(row['id'])/'status.json',final)
        return final


def watch(control):
    cfg=configure(control)
    from script.scale_live_parallel_pool import read,write,now,identity,verify_manifest
    from script.q6_parallel_rl import locked
    from script.train_q6_rl import evaluation_report
    out=Path(cfg['out']);verify_manifest(out);verify_manifest(control)
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (control/'launch.json').exists():raise ValueError('Controller already launched')
        if not process_alive(cfg['learner']):raise ValueError('Original learner identity changed')
        write(control/'launch.json',dict(pid=os.getpid(),identity=identity(os.getpid()),at=now()))
        with locked(out):
            original=read(out/'jobs.json');write(control/'jobs_before_release.json',original)
            write(out/'jobs.json',hold_pending(original))
        retired={};restored=False
        while True:
            if not restored:
                for row in cfg['retire_workers']:
                    if str(row['id']) not in retired:
                        result=retire_if_idle(out,row)
                        if result is not None:retired[str(row['id'])]=result
                if len(retired)==len(cfg['retire_workers']):
                    with locked(out):write(out/'jobs.json',restore_pending(read(out/'jobs.json')))
                    restored=True
                    write(control/'release_complete.json',dict(retired_workers=retired,learner=cfg['learner'],
                        learner_model=cfg['learner_model'],learner_and_optimizer_preserved=True,at=now()))
            status=read(out/'workers/0/status.json')
            failure=read(out/'failure.json') if (out/'failure.json').exists() else None
            live=process_alive(cfg['learner'])
            if not live and status['stage']!='complete' and not failure:
                failure=dict(message='Adopted learner exited before completion',at=now());write(out/'failure.json',failure)
            trained=(out/'training/result.json').exists();evaluation=evaluation_report(out) if trained else None
            complete=bool(evaluation and evaluation['complete'] and not live and not failure)
            stage='failed' if failure else 'complete' if complete else 'validation300' if trained else 'GRPO8_training'
            write(out/'queue_status.json',dict(state='failed' if failure else 'complete' if complete else 'running',stage=stage,
                controller_pid=os.getpid(),workers={'0':status},allowed_gpu_indices=['0'],user_reserved_gpu_indices=['1','2','3'],
                maximum_model_workers=1,primary_arm='Qplus',group_size=8,resource_controller=str(control),
                release_in_progress=not restored,retired_workers=retired,updated_at=now()))
            write(out/'report.json',dict(stage=stage,complete=complete,primary_arm='Qplus',epochs=3,group_size=8,
                training_progress=read(out/'training/status.json') if (out/'training/status.json').exists() else None,
                training=read(out/'training/result.json') if trained else None,evaluation=evaluation,failure=failure,
                resource_controller=str(control),allowed_gpu_indices=['0'],release_in_progress=not restored,updated_at=now()))
            if not live:return
            time.sleep(1 if not restored else 5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--control',type=Path,required=True)
    args=parser.parse_args();watch(args.control.resolve())
