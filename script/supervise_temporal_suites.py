"""Resume verified frozen experiments in small waves, tolerating PBS outages."""
import argparse
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def read(path):return json.loads(Path(path).read_text())


def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def now():return datetime.now(timezone.utc).isoformat()


def verify_snapshot(root):
    for name,h in read(root/'source_hashes.json').items():
        if hashlib.sha256((root/'source'/name).read_bytes()).hexdigest()!=h:
            raise ValueError('Frozen source changed: '+name)
    for name,h in read(root/'control_hashes.json').items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=h:
            raise ValueError('Frozen controls changed: '+name)


class PBSUnavailable(RuntimeError):
    pass


def query(ids):
    try:
        p=subprocess.run(['qstat','-x','-f','-F','json',*ids],capture_output=True,text=True,timeout=40)
        if p.returncode:raise PBSUnavailable('PBS status lookup failed: '+p.stderr.strip())
        jobs=read_json_jobs(p.stdout)
        if any(jid not in jobs or 'job_state' not in jobs[jid] for jid in ids):
            raise PBSUnavailable('PBS returned incomplete job status')
        return jobs
    except (subprocess.TimeoutExpired,OSError,ValueError,KeyError,TypeError) as exc:
        raise PBSUnavailable(f'PBS status lookup failed: {exc}') from exc


def query_reliably(ids,state,state_dir):
    """Never infer free slots or submit work from a failed status lookup."""
    phase=state['phase'];attempt=0
    while True:
        try:
            jobs=query(ids)
        except PBSUnavailable as exc:
            attempt+=1;delay=min(60,5*2**min(attempt-1,4))
            state.update(phase='waiting_for_pbs',updated_at=now(),pbs_last_error=str(exc),
                         pbs_consecutive_failures=attempt,pbs_retry_seconds=delay,
                         pbs_failures=state.get('pbs_failures',0)+1)
            write(state_dir/'status.json',state)
            print(f'PBS RETRY {attempt} in {delay}s: {exc}',flush=True)
            time.sleep(delay)
            continue
        state.update(phase=phase,pbs_consecutive_failures=0,pbs_retry_seconds=0,updated_at=now())
        if attempt:state['pbs_recovered_at']=now();print('PBS connection recovered',flush=True)
        write(state_dir/'status.json',state)
        return jobs


def read_json_jobs(value):return json.loads(value)['Jobs']


def tasks(temporal,aligned=None):
    result=[]
    temporal_experiment=read(temporal/'experiment.json')
    paired_summary='summary_deep' in temporal_experiment.get('arms',{})
    count=max(read(root/'experiment.json')['shards'] for root in (temporal,aligned) if root is not None)
    for group in range(count):
        for suite,worker in [('temporal','summary_pair' if paired_summary else 'temporal_cross'),('aligned','paired'),
                             ('temporal','temporal_local'),('aligned','videotree'),('temporal','cluster_qa')]:
            root=temporal if suite=='temporal' else aligned
            if root is None:continue
            if group<read(root/'experiment.json')['shards']:
                result.append(dict(suite=suite,worker=worker,group=group,root=str(root)))
    return result


def recover_tasks(planned):
    """Restore every submitted job before considering any new submission."""
    pending=[];active=[];seen=set()
    for task in planned:
        jobs=read(Path(task['root'])/'jobs.json')
        key='arm' if task['suite']=='temporal' else 'worker'
        matches=[j for j in jobs['formal'] if j.get(key)==task['worker'] and j['group']==task['group']]
        if len(matches)>1:raise ValueError('Duplicate recorded task: '+str(task))
        if not matches:pending.append(task);continue
        jid=matches[0]['job_id']
        if jid in seen:raise ValueError('Job recorded for multiple tasks: '+jid)
        seen.add(jid);active.append(dict(task,job_id=jid))
    return pending,active


def submit(task):
    root=Path(task['root']);jobs=read(root/'jobs.json')
    key='arm' if task['suite']=='temporal' else 'worker'
    existing=next((j for j in jobs['formal'] if j.get(key)==task['worker'] and j['group']==task['group']),None)
    if existing:return existing['job_id']
    if task['suite']=='temporal':
        env=f'TEMPORAL_DIR={root},TEMPORAL_STAGE=formal,TEMPORAL_ARM={task["worker"]},TEMPORAL_GROUP={task["group"]}'
        pbs='temporal_entity.pbs'
    else:
        env=f'ALIGNED_DIR={root},ALIGNED_STAGE=formal,ALIGNED_WORKER={task["worker"]},ALIGNED_GROUP={task["group"]}'
        pbs='videotree_aligned.pbs'
    (root/'logs').mkdir(exist_ok=True)
    summary_mode=read(root/'experiment.json').get('entity_source')=='summary'
    worker_name={'temporal_cross':'cross','temporal_local':'local','cluster_qa':'cqa','summary_pair':'pair'}.get(task['worker'],task['worker'][:5])
    job_name=f'st-{worker_name}-{task["group"]:02d}' if summary_mode else f'r7-{task["worker"][:5]}-{task["group"]:02d}'
    cmd=['qsub','-N',job_name,
         '-o',str(root/'logs'/f'{task["worker"]}-{task["group"]}.pbs.log'),
         '-v',env,str(root/'source/script'/pbs)]
    p=subprocess.run(cmd,capture_output=True,text=True,timeout=40)
    if p.returncode:raise RuntimeError('Formal submission failed: '+p.stderr.strip())
    jid=p.stdout.strip()
    jobs['formal'].append({key:task['worker'],'group':task['group'],'job_id':jid,
        'verified_before_submission':True,'submitted_by':'supervise_temporal_suites.py'})
    jobs['submission_mode']='at most two unfinished formal jobs across both suites'
    jobs['report_mode']='CPU summary after all formal jobs pass execution checks'
    write(root/'jobs.json',jobs)
    print('SUBMITTED',jid,task['suite'],task['worker'],task['group'],flush=True)
    return jid


def inspect(task,complete=True):
    root=Path(task['root']);issues=[]
    ids=read(root/f'shards/{task["group"]}.json')
    arms=(['continuous_flat','continuous_script'] if task['worker']=='paired' else
          ['summary_deep','temporal_cross'] if task['worker']=='summary_pair' else [task['worker']])
    for uid in ids:
        for arm in arms:
            if task['suite']=='temporal':path=root/'arms'/arm/'runs'/uid/'result.json'
            elif arm=='videotree':path=root/'videotree/runs'/uid/'result.json'
            else:path=root/'paired/runs'/uid/arm/'result.json'
            if not path.exists():
                if complete:issues.append(dict(question_id=uid,arm=arm,kind='integrity',error='missing result'))
                continue
            result=read(path)
            error=result.get('error');kind='query_error'
            if not error and str((result.get('answer') or {}).get('selected_option')) not in list('01234'):
                error='missing or invalid final answer';kind='integrity'
            if (result.get('cost_summary',{}).get('budget_violation') or
                              result.get('directory_cost_summary',{}).get('budget_violation')):
                error='processing budget violation';kind='integrity'
            if error:issues.append(dict(question_id=uid,arm=arm,kind=kind,error=error))
    return issues


def check_task(task,state,complete,continue_query_errors):
    issues=inspect(task,complete=complete)
    failures=[i for i in issues if i['kind']!='query_error' or not continue_query_errors]
    if failures:
        state.update(failed_task=task,execution_issues=issues)
        raise RuntimeError('Formal execution failed; further submission stopped; submitted jobs left running')
    for issue in issues:
        record=dict(suite=task['suite'],group=task['group'],job_id=task['job_id'],**issue)
        if record not in state['query_errors']:
            state['query_errors'].append(record)
            print('QUERY ERROR (retained for scoring): '+json.dumps(record),flush=True)


def main(temporal,aligned,state_dir,continue_query_errors=False):
    state_dir.mkdir(parents=True,exist_ok=True)
    lock=(state_dir/'supervisor.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=dict(pid=os.getpid(),started_at=now(),phase='waiting_for_verification',
        temporal=str(temporal),aligned=str(aligned) if aligned else None,formal_pending_limit=2,
        continue_query_errors=continue_query_errors,query_errors=[])
    write(state_dir/'status.json',state)
    try:
        roots=[root for root in (temporal,aligned) if root is not None]
        for root in roots:verify_snapshot(root)
        checks={str(root):read(root/'jobs.json')['verification'] for root in roots}
        while True:
            live=query_reliably(list(checks.values()),state,state_dir);finished=True
            for root,jid in checks.items():
                job=live[jid]
                if job['job_state']!='F':finished=False;continue
                status=Path(root)/'verification/status.json'
                if not status.exists():status=Path(root)/'smoke/status.json'
                if job.get('Exit_status')!=0 or not status.exists() or not read(status).get('passed'):
                    raise RuntimeError(f'GPU verification failed: {jid}; no formal work submitted')
            state.update(updated_at=now(),verification_states={jid:live[jid]['job_state'] for jid in checks.values()})
            write(state_dir/'status.json',state)
            if finished:break
            time.sleep(30)
        state['phase']='formal';planned,active=recover_tasks(tasks(temporal,aligned));completed=[]
        state.update(active=active,completed=completed,remaining_tasks=len(planned))
        write(state_dir/'status.json',state)
        for root in roots:verify_snapshot(root)
        while planned or active:
            # Inspect a finished wave BEFORE filling newly available slots.
            # A new execution failure therefore cannot fan out across the cohort.
            if active:
                for task in active:
                    check_task(task,state,complete=False,continue_query_errors=continue_query_errors)
                live=query_reliably([t['job_id'] for t in active],state,state_dir)
                remaining=[]
                for task in active:
                    job=live[task['job_id']]
                    if job['job_state']!='F':remaining.append(task);continue
                    check_task(task,state,complete=True,continue_query_errors=continue_query_errors)
                    if job.get('Exit_status')!=0:
                        state.update(failed_task=task,pbs_exit=job.get('Exit_status'))
                        raise RuntimeError('Formal execution failed; further submission stopped')
                    completed.append(task)
                active=remaining
            while planned and len(active)<2:
                task=planned.pop(0);task['job_id']=submit(task);active.append(task)
            state.update(updated_at=now(),active=active,completed=completed,remaining_tasks=len(planned))
            write(state_dir/'status.json',state)
            if active:time.sleep(30)
        reports=[(temporal,'run_temporal_entity.py')]
        if aligned is not None:reports.append((aligned,'run_videotree_aligned.py'))
        for root,script in reports:
            p=subprocess.run([sys.executable,str(root/'source/script'/script),'report','--out',str(root)],
                capture_output=True,text=True,timeout=120)
            (root/'report.cpu.log').write_text(p.stdout+p.stderr)
            if p.returncode:raise RuntimeError('CPU summary failed: '+str(root))
        state.update(phase='complete_with_query_errors' if state['query_errors'] else 'complete',updated_at=now())
        write(state_dir/'status.json',state)
    except Exception as exc:
        state.update(phase='stopped_on_error',updated_at=now(),error=f'{type(exc).__name__}: {exc}')
        write(state_dir/'status.json',state)
        raise
    finally:
        lock.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temporal',required=True);parser.add_argument('--aligned')
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--continue-query-errors',action='store_true',
        help='Keep individual failed predictions in scoring and continue other questions; integrity failures still stop submissions')
    args=parser.parse_args()
    main(Path(args.temporal).resolve(),Path(args.aligned).resolve() if args.aligned else None,Path(args.state_dir).resolve(),args.continue_query_errors)
