"""Repair/continue a frozen local run without rewriting its spent attempts.

Successful source runs are referenced read-only. Every new attempt has its own
durable traces, and paired source/dependent results always share one bundle.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script.run_four_arm_300_local import now,worker_env,verify_snapshot,require_idle
from script.run_temporal_entity import ARMS,execute,read,write,sha,report
from script_core.config import load_config


def successful(path):
    if not path.exists():return False
    value=read(path)
    answer=value.get('answer') or {}
    return not value.get('error') and str(answer.get('selected_option')) in {'0','1','2','3','4'}


def source_usable(path):
    if not successful(path/'result.json') or not (path/'evidence_bundle.json').exists():return False
    value=read(path/'evidence_bundle.json')
    return not value.get('incomplete') and not value['network'].get('diagnostics')


def prepare(reference,out,gpus):
    if out.exists():raise ValueError('Repair output must be a new directory')
    if len(gpus)!=2:raise ValueError('Use two released experiment GPUs')
    inventory=require_idle(gpus)
    out.mkdir(parents=True)
    for name in ('config.json','settings.json','cohort.json','input_hashes.json',
                 'questions.jsonl','evaluation_only.json'):
        shutil.copy2(reference/name,out/name)
    for name in ('inputs','input_ready'):
        (out/name).symlink_to(reference/name,target_is_directory=True)
    plan=deepcopy(read(reference/'experiment.json'))
    plan.update(created_at=now(),repair_of=str(reference),
        repair_policy='Keep original attempts; reference valid successes; fresh budgets are separate attempts, not resumed budgets',
        repair_groups={'local':dict(index=gpus[0],uuid=inventory[gpus[0]]['uuid']),
                       'paired':dict(index=gpus[1],uuid=inventory[gpus[1]]['uuid'])},
        repair_changes=['Three entities per observation page, bounded sparse states, metered smaller timeout retries',
            'Bind retry entity names to supporting original caption IDs',
            'Exclude and record unsupported optional relations/causes',
            'Preserve literal source newlines; regenerate malformed structured output from canonical IDs',
            'Parse unambiguous explicit QA declarations; cancel original QA at its deadline'],
        mixed_source_revisions=True,accuracy_is_not_a_gate=True)
    write(out/'experiment.json',plan)
    reused={arm:[] for arm in ARMS};pending={arm:[] for arm in ARMS}
    for uid in read(out/'cohort.json'):
        old_source=reference/'arms/summary_deep/runs'/uid
        paired_good=source_usable(old_source)
        for arm in ARMS:
            old=reference/'arms'/arm/'runs'/uid
            good=successful(old/'result.json')
            if arm in ('summary_deep','temporal_cross'):good=good and paired_good
            parent=out/'arms'/arm/'runs';parent.mkdir(parents=True,exist_ok=True)
            if good:
                (parent/uid).symlink_to(old,target_is_directory=True);reused[arm].append(uid)
            else:pending[arm].append(uid)
    source=out/'source';source.mkdir()
    for folder in ('script_core','script','tests','configs','data_extraction','kmeans_pytorch'):
        shutil.copytree(ROOT/folder,source/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for path in ROOT.glob('*.py'):shutil.copy2(path,source/path.name)
    for name in ('data','checkpoints'):(source/name).symlink_to((ROOT/name).resolve(),target_is_directory=True)
    hashes={str(p.relative_to(source)):sha(p) for p in source.rglob('*') if p.is_file()
            and not any(x in ('data','checkpoints','__pycache__') for x in p.relative_to(source).parts)}
    write(out/'source_hashes.json',hashes)
    write(out/'control_hashes.json',{n:sha(out/n) for n in
        ('config.json','settings.json','cohort.json','experiment.json','questions.jsonl')})
    write(out/'repair_manifest.json',dict(created_at=now(),reference=str(reference),
        reused=reused,pending=pending,original_source_hashes=read(reference/'source_hashes.json')))
    for folder in ('logs','workers','verification','attempts'):(out/folder).mkdir(exist_ok=True)
    return plan


def archive_attempt(out,arm,uid):
    target=out/'arms'/arm/'runs'/uid
    if target.is_symlink():raise ValueError('Never mutate a successful source run')
    if not target.exists():return
    archive=out/'attempts'/arm/uid;archive.mkdir(parents=True,exist_ok=True)
    number=len(list(archive.glob('attempt-*')))+1
    target.rename(archive/f'attempt-{number:02d}')


def worker(out,group,max_attempts):
    from script_core.backend import preflight
    from script_core.budget import ModelWorker
    verify_snapshot(out)
    cfg,artifacts=preflight(load_config(out/'config.json'))
    write(out/'verification'/f'{group}-artifacts.json',artifacts)
    backend=ModelWorker(cfg);ids=read(out/'cohort.json')
    arms=['cluster_qa','temporal_local'] if group=='local' else ['summary_deep','temporal_cross']
    state=out/'workers'/f'{group}.json'
    try:
        for attempt in range(1,max_attempts+1):
            for uid in ids:
                for arm in arms:
                    target=out/'arms'/arm/'runs'/uid
                    if successful(target/'result.json'):
                        if arm!='summary_deep' or source_usable(target):continue
                    # A dependent arm cannot acquire different source evidence.
                    if arm=='temporal_cross' and not source_usable(out/'arms/summary_deep/runs'/uid):
                        continue
                    archive_attempt(out,arm,uid)
                    write(state,dict(group=group,pid=os.getpid(),state='running',arm=arm,
                        video_id=uid,attempt=attempt,updated_at=now()))
                    print('REPAIR_START',group,arm,uid,'attempt',attempt,flush=True)
                    result=execute(out,arm,uid,backend,cfg)
                    print('REPAIR_RESULT',arm,uid,'attempt',attempt,
                          'answer',(result.get('answer')or{}).get('selected_option'),
                          'error',result.get('error'),flush=True)
                    write(target/'repair_attempt.json',dict(attempt=attempt,finished_at=now(),
                        reference=read(out/'experiment.json')['repair_of'],separate_fresh_budget=True))
    finally:backend.close(graceful=True)
    failures={arm:[uid for uid in ids if not successful(out/'arms'/arm/'runs'/uid/'result.json')
                  or (arm=='summary_deep' and not source_usable(out/'arms'/arm/'runs'/uid))] for arm in arms}
    write(state,dict(group=group,pid=os.getpid(),state='finished' if not any(failures.values())
        else 'finished_with_errors',updated_at=now(),failures=failures))
    return int(any(failures.values()))


def status(out):
    ids=read(out/'cohort.json');groups={};manifest=read(out/'repair_manifest.json');counts={}
    for group in ('local','paired'):
        path=out/'workers'/f'{group}.json'
        if path.exists():groups[group]=read(path)
    for arm in ARMS:
        root=out/'arms'/arm/'runs';done=sum((root/uid/'result.json').exists() for uid in ids)
        ok=sum(successful(root/uid/'result.json') for uid in ids)
        counts[arm]=dict(planned=len(ids),completed=done,successful=ok,errors=done-ok,
                         reused=len(manifest['reused'][arm]))
    value=dict(updated_at=now(),arms=counts,workers=groups,
               complete=all(c['successful']==c['planned'] for c in counts.values()))
    write(out/'progress.json',value)
    return value


def controller(out,max_attempts):
    verify_snapshot(out);plan=read(out/'experiment.json');jobs={};logs={}
    require_idle([v['index'] for v in plan['repair_groups'].values()])
    for group,gpu in plan['repair_groups'].items():
        logs[group]=(out/'logs'/f'{group}.log').open('a')
        command=[sys.executable,'-u',str(Path(__file__).resolve()),'worker','--out',str(out),
                 '--group',group,'--max-attempts',str(max_attempts)]
        jobs[group]=subprocess.Popen(command,cwd=ROOT,env=worker_env(gpu['uuid']),
            stdout=logs[group],stderr=subprocess.STDOUT)
    write(out/'processes.json',{g:dict(pid=p.pid,**plan['repair_groups'][g]) for g,p in jobs.items()})
    try:
        while any(p.poll() is None for p in jobs.values()):
            status(out);time.sleep(10)
        value=status(out);report(out)
        write(out/'finished.json',dict(finished_at=now(),complete=value['complete'],
                                      exit_codes={g:p.returncode for g,p in jobs.items()}))
        return int(not value['complete'])
    finally:
        for log in logs.values():log.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['prepare','controller','worker','status'])
    p.add_argument('--out',type=Path,required=True);p.add_argument('--reference',type=Path)
    p.add_argument('--gpus',default='4,5');p.add_argument('--group',choices=['local','paired'])
    p.add_argument('--max-attempts',type=int,default=3)
    a=p.parse_args();out=a.out.resolve()
    if not 1<=a.max_attempts<=3:p.error('max-attempts must be between 1 and 3')
    if a.command=='prepare':
        if a.reference is None:p.error('--reference is required')
        prepare(a.reference.resolve(),out,a.gpus.split(','));print(json.dumps(status(out)),flush=True);return 0
    if a.command=='status':print(json.dumps(status(out)),flush=True);return 0
    if a.command=='worker':
        if a.group is None:p.error('--group is required')
        return worker(out,a.group,a.max_attempts)
    return controller(out,a.max_attempts)


if __name__=='__main__':raise SystemExit(main())
