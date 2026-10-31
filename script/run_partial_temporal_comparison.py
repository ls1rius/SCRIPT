"""Paired 300-question temporal organization on frozen adaptive_partial evidence."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import statistics
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script.run_adaptive_comparison import now,read,sha,verify as verify_prior
from script.run_followup_validation import snapshot,verify,stop_on_resource_error
from script_core.config import digest
from script_core.state import write_json as write


def prepare(prior,out):
    from script_core.partial_temporal import project,observation_pairs,identity_pairs,VERSION
    if out.exists():raise ValueError('Use a fresh experiment directory')
    verify_prior(prior)
    ids=read(prior/'cohort.json')
    if len(ids)!=300 or len(set(ids))!=300:raise ValueError('Require the fixed 300-question cohort')
    out.mkdir(parents=True);snapshot(ROOT,out/'source')
    for name in ('cohort.json','input_hashes.json','evaluation_only.json','settings.json'):
        shutil.copy2(prior/name,out/name)
    shutil.copytree(prior/'inputs',out/'inputs')
    cfg=read(prior/'config.json');cfg['cache']['persistent_directory']=str(out/'cache')
    cfg['compatibility']['cache_directory']='';write(out/'config.json',cfg)
    shutil.copy2(prior/'artifacts.json',out/'control_artifacts.json')
    controls={};structure={};origins={}
    for uid in ids:
        source=prior/'formal/arms/adaptive_partial/runs'/uid
        result=read(source/'result.json');memory=read(source/'network.json');packet=read(out/'inputs'/f'{uid}.json')
        if result.get('error') or not result.get('answer'):raise ValueError('Control must have a successful answer: '+uid)
        calls=[json.loads(s) for s in (source/'calls.jsonl').read_text().splitlines()]
        qa=[c for c in calls if c['operation']=='videotree_qa']
        if len(qa)!=1 or qa[0]['error']:raise ValueError('Require exactly one saved successful control QA')
        first_check=next(i for i,c in enumerate(calls) if c['operation']=='temporal_qa_check')
        shared=result['cost_summary']['initialization']+sum(c['actual_cost']+
            c.get('tool_cache',{}).get('budget_dependency_seconds',0.) for c in calls[:first_check])
        bundle=dict(schema_version=1,packet_hash=digest(packet),memory=memory,
            opened_scenes=result['opened_scenes'],selected_seconds=result['selected_seconds'],
            supplemental_visual=result['supplemental_visual'],source_incomplete=result['incomplete'],
            preparation_seconds=shared,qa_input=qa[0]['parameters'],
            directory_cost_summary=result['directory_cost_summary'])
        write(out/'evidence'/f'{uid}.json',bundle)
        controls[uid]=dict(answer=dict(selected_option=result['answer']['selected_option']),error=None,
            total_measured_seconds=result['total_measured_seconds'],incomplete=result['incomplete'])
        converted=project(memory)
        structure[uid]=dict(entities=len(memory['entities']),pairs=len(observation_pairs(converted)),
            scenes=len({e['scene_id'] for e in memory['entities'].values()}),relations=len(memory['relations']),
            identity_candidates=len(identity_pairs(converted,result['opened_scenes'])))
        origins[uid]={name:sha(source/name) for name in ('result.json','network.json','calls.jsonl')}
    write(out/'controls.json',controls);write(out/'source_origins.json',origins)
    gate=[]
    for predicate in (lambda x:x['pairs']>0,lambda x:x['scenes']>1,lambda x:x['relations']>0,lambda x:x['entities']==0):
        found=next((u for u in ids if u not in gate and predicate(structure[u])),None)
        if found:gate.append(found)
    write(out/'verification/cpu_projection.json',dict(passed=True,count=300,
        temporal_pairs=sum(x['pairs'] for x in structure.values()),
        identity_candidates=sum(x['identity_candidates'] for x in structure.values()),
        multiple_entity_scenes=sum(x['scenes']>1 for x in structure.values()),structure=structure))
    write(out/'experiment.json',dict(created_at=now(),prior=str(prior),arm='adaptive_partial_temporal',method=VERSION,
        count=300,gate_ids=gate,gate_selection='Structure only: continuity pairs, multiple entity scenes, relation, empty memory. No accuracy selection.',
        evidence='Exact prior adaptive_partial scene summaries, local extraction and verified visual observations',
        intervention='Only temporal continuity and cross-scene organization followed by independent QA',
        budget='Original evidence preparation charged inside the same 1500-second online cap; directory source cost added once',
        timing='Actual new organization+QA and attributed first-run total reported separately; mixed shared-GPU dates',
        original_controls='Original 300 successful partial results; saved answers/labels used only for reporting',
        scheduling=dict(preferred_gpu='0',allowed_gpus=['0','1','2','3'],minimum_free_mib=40*1024,
            stable_observations=3,poll_seconds=15,max_workers=1,automatic_retry=False)))
    paths=[p for p in out.rglob('*') if p.is_file() and not any(x in ('data','checkpoints','__pycache__') for x in p.relative_to(out).parts)]
    write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in paths});verify(out)
    write(out/'queue_status.json',dict(state='prepared',updated_at=now()))


def summarize(out):
    ids=read(out/'cohort.json');truth=read(out/'evaluation_only.json');controls=read(out/'controls.json')
    rows={u:read(p) for u in ids if (p:=out/'formal/runs'/u/'result.json').exists() and 'total_measured_seconds' in read(p)}
    def correct(row,u):return not row.get('error') and str((row.get('answer')or{}).get('selected_option'))==str(truth[u])
    avg=lambda key:statistics.mean(r[key] for r in rows.values()) if rows else None
    write(out/'report.json',dict(updated_at=now(),completed=len(rows),planned=300,
        correct=sum(correct(r,u) for u,r in rows.items()),errors=sum(bool(r.get('error')) for r in rows.values()),
        control_correct_same_ids=sum(correct(controls[u],u) for u in rows),control_correct_full_300=sum(correct(controls[u],u) for u in ids),
        corrected=[u for u,r in rows.items() if correct(r,u) and not correct(controls[u],u)],
        regressed=[u for u,r in rows.items() if correct(controls[u],u) and not correct(r,u)],
        mean_total_seconds=avg('total_measured_seconds'),mean_new_processing_seconds=avg('incremental_processing_seconds'),
        mean_organization_seconds=avg('organization_seconds'),
        control_mean_seconds_same_ids=statistics.mean(controls[u]['total_measured_seconds'] for u in rows) if rows else None,
        identity_comparisons=sum(r.get('identity_comparisons',0) for r in rows.values()),
        temporal_pairs=sum(r.get('temporal_pair_count',0) for r in rows.values()),
        incomplete=sum(bool(r.get('incomplete')) for r in rows.values()),
        policy='Fixed 300 denominator; pending separate; source preparation counted, additional validation QA/cold setup separate'))


def worker(out):
    from script_core.backend import preflight
    from script_core.budget import BudgetLedger,Meter,ModelWorker
    from script_core.config import load_config
    from script_core.partial_temporal import run
    verify(out);cfg,artifacts=preflight(load_config(out/'config.json'));old=read(out/'control_artifacts.json')
    for name in ('vlm','visual_encoder','detector','text_embedding'):
        if artifacts[name]['hash']!=old[name]['hash']:raise ValueError('Model changed: '+name)
    if artifacts['software']!=old['software']:raise ValueError('Control runtime software changed')
    write(out/'artifacts.json',artifacts);backend=ModelWorker(cfg)
    try:
        meter=Meter(backend,BudgetLedger(600,answer_reserve=0,calls_path=out/'setup/calls.jsonl'),cfg,out/'setup/raw_calls')
        try:meter.call('prepare_vlm',{},build=True)
        finally:write(out/'setup/cost_summary.json',meter.ledger.summary());meter.close()
        experiment=read(out/'experiment.json');gate=experiment['gate_ids'];ids=read(out/'cohort.json')
        ordered=gate+[u for u in ids if u not in gate]
        for i,uid in enumerate(ordered):
            target=out/'formal/runs'/uid
            if target.exists():raise ValueError('Attempt exists; no automatic replay')
            write(out/'progress.json',dict(updated_at=now(),question_id=uid,completed=i,planned=300,
                stage='regression' if i<len(gate) else 'formal'))
            packet=read(out/'inputs'/f'{uid}.json');bundle=read(out/'evidence'/f'{uid}.json')
            result=run(packet,bundle,backend,cfg,target,read(out/'settings.json'))
            directory=bundle['directory_cost_summary'];result['directory_cost_summary']=directory
            result['total_measured_seconds']=directory.get('budgeted_total',directory['total'])+result['cost_summary']['budgeted_total']
            result['organization_seconds']=sum(v for k,v in result['cost_summary']['by_operation'].items()
                if k not in ('videotree_qa','temporal_qa_check','temporal_qa_context_selection'))
            write(target/'result.json',result);summarize(out)
            stop_on_resource_error(out,dict(id='partial_temporal'),result,i+1)
            if i+1==len(gate):
                results=[read(out/'formal/runs'/u/'result.json') for u in gate]
                structure=read(out/'verification/cpu_projection.json')['structure']
                passed=all(not r['error'] and r.get('answer') and not r['cost_summary']['budget_violation']
                    and not r['incomplete'] and r.get('temporal_pair_count',0)==structure[u]['pairs']
                    and r.get('identity_comparisons',0)==structure[u]['identity_candidates'] for u,r in zip(gate,results))
                for uid in gate:
                    calls=[json.loads(line) for line in (out/'formal/runs'/uid/'calls.jsonl').read_text().splitlines()]
                    passed=passed and all(not c.get('input_pixels') and not c.get('model_frame_views') for c in calls)
                write(out/'verification/gate.json',dict(passed=bool(passed),count=len(gate),accuracy_not_used=True,
                    temporal_pairs=sum(r.get('temporal_pair_count',0) for r in results),
                    identity_judgments=sum(r.get('identity_comparisons',0) for r in results),updated_at=now()))
                if not passed:return 2
        write(out/'completion.json',dict(completed=300,updated_at=now()));return 0
    finally:backend.close()


def queue(out):
    from script.run_optimized_comparison import inventory
    verify(out);policy=read(out/'experiment.json')['scheduling'];stable={}
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'worker_launch.json').exists():raise ValueError('Worker already attempted; inspect retained state')
        while True:
            gpus={i:r for i,r in inventory().items() if i in policy['allowed_gpus']}
            stable={i:stable.get(i,0)+1 if r['free_mib']>=policy['minimum_free_mib'] else 0 for i,r in gpus.items()}
            write(out/'queue_status.json',dict(state='waiting_for_gpu_memory',updated_at=now(),pid=os.getpid(),
                preferred_gpu=policy['preferred_gpu'],minimum_free_mib=policy['minimum_free_mib'],gpus=gpus,stable_observations=stable))
            for index in sorted(gpus,key=lambda i:(i!=policy['preferred_gpu'],-gpus[i]['free_mib'])):
                if stable[index]<policy['stable_observations']:continue
                row=gpus[index];lease=(Path('/tmp')/f'videoagent-{os.getuid()}-{row["uuid"]}.lock').open('a')
                try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:lease.close();continue
                fresh=inventory().get(index)
                if not fresh or fresh['uuid']!=row['uuid'] or fresh['free_mib']<policy['minimum_free_mib']:
                    lease.close();continue
                env=dict(os.environ,CUDA_VISIBLE_DEVICES=row['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',HF_HUB_OFFLINE='1',
                    HF_HOME=str(ROOT/'checkpoints/.cache/huggingface'),HF_MODULES_CACHE=str(ROOT/'checkpoints/.cache/modules'),
                    TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
                with (out/'worker.log').open('a') as log:
                    proc=subprocess.Popen([sys.executable,'-u',str(out/'source/script'/Path(__file__).name),'worker','--out',str(out)],
                        cwd=out/'source',env=env,stdout=log,stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
                launched=dict(state='running',updated_at=now(),pid=os.getpid(),worker_pid=proc.pid,gpu_index=index,gpu_uuid=row['uuid'])
                write(out/'worker_launch.json',launched)
                while proc.poll() is None:
                    write(out/'queue_status.json',dict(launched,updated_at=now()));time.sleep(5)
                lease.close();write(out/'queue_status.json',dict(launched,state='complete' if proc.returncode==0 else 'failed',
                    exit_code=proc.returncode,updated_at=now()));return proc.returncode
            time.sleep(policy['poll_seconds'])


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--prior',type=Path);parser.add_argument('--detach',action='store_true')
    args=parser.parse_args();out=args.out.resolve()
    try:
        if args.mode=='prepare':
            prepare(args.prior.resolve(),out)
            if args.detach:
                command=[sys.executable,'-u',str(out/'source/script'/Path(__file__).name),'queue','--out',str(out)]
                start=out/'start_queue.sh';start.write_text('#!/bin/bash\nset -euo pipefail\nexec '+shlex.join(command)+' >> '+shlex.quote(str(out/'controller.log'))+' 2>&1\n')
                session='script-partial-temporal-'+str(int(time.time()))
                subprocess.run(['tmux','new-session','-d','-s',session,'bash '+shlex.quote(str(start))],check=True)
                write(out/'launch.json',dict(session=session,command=command,created_at=now()))
            return 0
        if args.mode=='report':summarize(out);return 0
        return queue(out) if args.mode=='queue' else worker(out)
    except Exception:
        write(out/('worker_failure.json' if args.mode=='worker' else 'controller_failure.json'),dict(updated_at=now(),traceback=traceback.format_exc()));raise


if __name__=='__main__':raise SystemExit(main())
