"""Repair run: reproduce the original baseline and fully materialize read evidence."""
import argparse
from collections import Counter
from copy import deepcopy
import csv
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from script.run_adaptive_comparison import read,now,sha
from script.run_followup_validation import snapshot,verify
from script_core.config import load_config,digest
from script_core.state import write_json as write
from script_core.complete_memory import source_bundle,coverage_index,compact_view,final_input
from script_core.normalized_memory import normalize
from script.run_memory_quotes_comparison import strict_prediction

ARMS=('original_replay','complete_with_quotes','complete_structured')


def prepare(prior,out):
    prior=Path(prior).resolve();out=Path(out).resolve()
    if out.exists():raise ValueError('Use a fresh repair-run directory')
    verify(prior);ids=read(prior/'cohort.json');origins=read(prior/'source_origins.json')
    old_source=Path(read(prior/'experiment.json')['prior'])
    if len(ids)!=300:raise ValueError('Require the fixed 300')
    out.mkdir(parents=True);controls={};totals=Counter()
    for uid in ids:
        folder=Path(origins[uid]['result']).parent
        result=read(folder/'result.json');context=read(folder/'qa_context.json')
        packet=read(old_source/'inputs'/f'{uid}.json')
        packet=dict(question=packet['question'],captions=packet['captions'])
        original=next(json.loads(line)['parameters'] for line in (folder/'calls.jsonl').read_text().splitlines()
            if json.loads(line)['operation']=='videotree_qa')
        visual=result['supplemental_visual']['observations']
        from script_core.temporal_pipeline import qa_context_inputs
        replay=qa_context_inputs(dict(packet['question'],id=uid),packet['captions'],
            context['selected_seconds'],context['context'],visual_observations=visual)
        if replay!=original:raise ValueError('Original QA input not exactly reconstructable: '+uid)
        bundle=source_bundle(packet,context,visual)
        # Both representations retain the original system and answer prompt.
        placeholder=dict(source_type='caption',temporal_caption_order=bundle['read_caption_ids'])
        for quotes in (True,False):final_input(original,packet,placeholder,visual,quotes)
        write(out/'inputs'/f'{uid}.json',dict(packet=packet,original=original,
            original_input_hash=digest(original),selected_seconds=context['selected_seconds'],visual=visual))
        write(out/'source_bundles'/f'{uid}.json',bundle)
        controls[uid]=dict(selected_option=result['answer']['selected_option'],
            response=result['answer']['text'],source=str(folder/'result.json'))
        totals.update(read_captions=len(bundle['read_caption_ids']),units=len(bundle['units']),
            archived_captions=len(packet['captions']))
    cfg=load_config(prior/'config.json');cfg['query']['tool_transient_retries']=0
    cfg['query']['json_repair_retries']=0;cfg['cache']['persistent_directory']=''
    cfg['compatibility']['cache_directory']=''
    write(out/'config.json',cfg);write(out/'cohort.json',ids);write(out/'historical.json',controls)
    write(out/'evaluation_only.json',read(prior/'evaluation_only.json'))
    write(out/'control_artifacts.json',read(prior/'control_artifacts.json'))
    write(out/'experiment.json',dict(created_at=now(),planned=300,arms=list(ARMS),
        historical_baseline=dict(correct=186,accuracy=.62,scope='Original saved temporal QA, unchanged legacy parser'),
        prior_template_control=dict(correct=165,accuracy=.55,scope='Different template/parser, not the original baseline'),
        repairs=['Materialize every already-read caption, even if the old planner declared raw-text sufficiency.',
            'Never use sparse entity citations or quotation limits as a storage filter.',
            'Separate actions, states, attributes and relations; retain unknown details as categorized originals.',
            'Do not replay action assertions through a physical-state merger.',
            'Keep original head, question/options order, visual observations and legacy answer parser.'],
        scope='Same already-read caption scope as the original 62% baseline, not additional unseen captions.',
        normalization='All read-source units; no question/options/answers in normalization calls.',
        materialization_shared_once=True,coverage_required=1.,shards=2,source_counts=dict(totals),
        control_gate='Saved original QA inputs exact; first four control predictions per shard must reproduce.',
        answer_protocol='Legacy parser for comparable primary score; strict parse is an audit only.',
        context_overflow='Use original full-input fallback and report it explicitly; never silently drop source information.',
        development_cohort=True))
    write(out/'resource_policy.json',dict(allowed_gpus=['0','1','2','3'],minimum_free_mib=28*1024,
        stable_observations=3,poll_seconds=10,max_workers=2,shared_gpu=True))
    snapshot(ROOT,out/'source')
    paths=[p for p in out.rglob('*') if p.is_file() and not any(
        part in ('data','checkpoints','__pycache__') for part in p.relative_to(out).parts)]
    write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in paths})
    verify(out);summarize(out)
    write(out/'queue_status.json',dict(state='prepared',updated_at=now()))


def answer_once(out,uid,arm,inputs,backend,cfg,fallback=None):
    from script_core.budget import BudgetLedger,Meter,ToolFailure
    folder=out/'runs'/arm/uid
    if folder.exists():raise ValueError('Existing spent QA attempt: '+str(folder))
    ledger=BudgetLedger(cfg['budget']['online_cap'],answer_reserve=cfg['budget']['final_answer_reserve'],
        calls_path=folder/'calls.jsonl');meter=Meter(backend,ledger,cfg,folder/'raw_calls')
    result=dict(question_id=uid,arm=arm,selected_option=None,error=None,input_hash=digest(inputs),fallback_original=False)
    began=time.monotonic()
    try:
        try:check,_=meter.call('temporal_qa_check',inputs,final=True)
        except ToolFailure as exc:
            if fallback is None or 'exceeds input allowance' not in str(exc):raise
            inputs=fallback;result['fallback_original']=True;result['fallback_reason']=str(exc)
            result['effective_input_hash']=digest(inputs)
            check,_=meter.call('temporal_qa_check',inputs,final=True)
        output,_=meter.call('videotree_qa',inputs,final=True)
        result.update(selected_option=output['data']['selected_option'],raw_response=output['data']['text'],input_tokens=check['input_tokens'])
        try:result['strict_audit_option']=strict_prediction(result['raw_response'])
        except ValueError:result['strict_audit_option']=None
    except Exception as exc:result['error']=dict(type=type(exc).__name__,message=str(exc))
    finally:
        result.update(seconds=time.monotonic()-began,cost_summary=ledger.summary())
        write(folder/'result.json',result);meter.close()
    return result


def worker(out,shard):
    from script_core.backend import preflight
    from script_core.budget import BudgetLedger,Meter,ModelWorker
    verify(out);progress=out/'progress'/f'{shard}.json'
    write(progress,dict(stage='preflight',shard=shard,updated_at=now(),completed=0))
    cfg,artifacts=preflight(load_config(out/'config.json'));old=read(out/'control_artifacts.json')
    for kind in ('vlm','visual_encoder','detector','text_embedding'):
        if artifacts[kind]['hash']!=old[kind]['hash']:raise ValueError('Original checkpoint changed')
    if artifacts['software']!=old['software']:raise ValueError('Runtime software changed')
    write(out/'artifacts'/f'{shard}.json',artifacts);backend=ModelWorker(cfg)
    try:
        setup=Meter(backend,BudgetLedger(600,answer_reserve=0,calls_path=out/'setup'/str(shard)/'calls.jsonl'),
            cfg,out/'setup'/str(shard)/'raw_calls')
        try:setup.call('prepare_vlm',{},build=True)
        finally:write(out/'setup'/str(shard)/'cost_summary.json',setup.ledger.summary());setup.close()
        ids=[u for i,u in enumerate(read(out/'cohort.json')) if i%2==shard]
        expected=read(out/'historical.json')
        for i,uid in enumerate(ids):
            row=read(out/'inputs'/f'{uid}.json');bundle=read(out/'source_bundles'/f'{uid}.json')
            if digest(row['original'])!=row['original_input_hash']:raise ValueError('Original input changed')
            write(progress,dict(stage='original_replay',question_id=uid,shard=shard,completed=i,planned=len(ids),updated_at=now()))
            baseline=answer_once(out,uid,'original_replay',row['original'],backend,cfg)
            if i<4 and (baseline['error'] or baseline['selected_option']!=expected[uid]['selected_option']):
                write(out/'verification'/f'baseline_mismatch_{shard}.json',dict(question_id=uid,expected=expected[uid],actual=baseline))
                return 4
            if i==3:write(out/'verification'/f'baseline_gate_{shard}.json',dict(passed=True,count=4,accuracy_used_for_gate=False))
            folder=out/'materialized'/uid
            if folder.exists():raise ValueError('Materialization already attempted')
            ledger=BudgetLedger(cfg['budget']['online_cap'],answer_reserve=cfg['budget']['final_answer_reserve'],
                calls_path=folder/'calls.jsonl');meter=Meter(backend,ledger,cfg,folder/'raw_calls')
            write(progress,dict(stage='materializing_all_read_sources',question_id=uid,shard=shard,completed=i,
                source_units=len(bundle['units']),planned=len(ids),updated_at=now()))
            try:
                normalized,audit=normalize(bundle,meter)
                coverage=coverage_index(row['packet'],bundle,normalized)
                packed=compact_view(bundle,normalized,coverage)
                write(folder/'memory.json',normalized);write(folder/'coverage.json',coverage)
                write(folder/'audit.json',audit);write(folder/'packed.json',packed)
            finally:write(folder/'cost_summary.json',ledger.summary());meter.close()
            order=ARMS[1:] if i%2==0 else tuple(reversed(ARMS[1:]))
            for arm in order:
                inputs=final_input(row['original'],row['packet'],packed,row['visual'],arm=='complete_with_quotes')
                write(out/'final_inputs'/arm/f'{uid}.json',inputs)
                result=answer_once(out,uid,arm,inputs,backend,cfg,fallback=row['original'])
                if result['error']:return 2
            print('COMPLETE_MEMORY',shard,i+1,uid,'covered',coverage['covered_captions'],
                'facts',audit['normalized_facts'],'residual',audit['retained_units'],flush=True)
        write(out/'completion'/f'{shard}.json',dict(complete=True,updated_at=now()));return 0
    finally:backend.close()


def summarize(out):
    out=Path(out)
    with (out/'report.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        ids=read(out/'cohort.json');truth=read(out/'evaluation_only.json');old=read(out/'historical.json')
        rows={a:{u:read(p) for u in ids if (p:=out/'runs'/a/u/'result.json').exists()} for a in ARMS}
        def correct(r,u):return not r.get('error') and str(r.get('selected_option'))==str(truth[u])
        def stats(data,cohort):
            present=[u for u in cohort if u in data];n=len(present);c=sum(correct(data[u],u) for u in present)
            return dict(planned=len(cohort),completed=n,correct=c,pending=len(cohort)-n,
                errors=sum(bool(data[u].get('error')) for u in present),
                original_fallbacks=sum(bool(data[u].get('fallback_original')) for u in present),
                accuracy=c/len(cohort) if n==len(cohort) and cohort else None,
                accuracy_among_completed=c/n if n else None)
        pairs={}
        for arm in ARMS[1:]:
            same=[u for u in ids if u in rows[arm] and u in rows['original_replay']]
            fixed=[u for u in same if correct(rows[arm][u],u) and not correct(rows['original_replay'][u],u)]
            worse=[u for u in same if correct(rows['original_replay'][u],u) and not correct(rows[arm][u],u)]
            pairs[arm]=dict(completed=len(same),corrected=fixed,regressed=worse,
                corrected_count=len(fixed),regressed_count=len(worse),
                accuracy_delta_percentage_points=(len(fixed)-len(worse))/len(same)*100 if same else None)
        audits=[read(p) for p in (out/'materialized').glob('*/audit.json')]
        coverage=[read(p) for p in (out/'materialized').glob('*/coverage.json')]
        summary=dict(updated_at=now(),planned=300,historical_baseline=stats(old,ids),
            arms={a:stats(rows[a],ids) for a in ARMS},comparisons=pairs,
            baseline_replication=dict(completed=len(rows['original_replay']),
                prediction_mismatches=[u for u,r in rows['original_replay'].items() if r['selected_option']!=old[u]['selected_option']]),
            materialization=dict(completed=len(coverage),read_captions=sum(c['read_captions'] for c in coverage),
                covered_captions=sum(c['covered_captions'] for c in coverage),
                all_complete=all(c['coverage']==1 for c in coverage),
                normalized_facts=sum(a['normalized_facts'] for a in audits),retained_units=sum(a['retained_units'] for a in audits)),
            scope='Original already-read evidence. Original 62% prompt, option order and legacy parser preserved.',
            complete=all(len(rows[a])==len(ids) for a in ARMS),development_cohort=True)
        write(out/'report.json',summary)
        return summary


def queue(out):
    from script.run_optimized_comparison import inventory
    verify(out);policy=read(out/'resource_policy.json');stable={};running={};finished={};leases={}
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if any((out/'launches').glob('*.json')):raise ValueError('A shard was already attempted')
        while len(finished)<2:
            for shard,proc in list(running.items()):
                if proc.poll() is not None:
                    finished[shard]=proc.returncode;leases.pop(shard).close();running.pop(shard)
            rows={i:r for i,r in inventory().items() if i in policy['allowed_gpus']}
            stable={i:stable.get(i,0)+1 if r['free_mib']>=policy['minimum_free_mib'] else 0 for i,r in rows.items()}
            for shard in range(2):
                if shard in running or shard in finished:continue
                for index in sorted(rows,key=lambda i:-rows[i]['free_mib']):
                    row=rows[index]
                    if stable[index]<policy['stable_observations']:continue
                    lease=(Path('/tmp')/f'videoagent-finalqa-{os.getuid()}-{row["uuid"]}.lock').open('a')
                    try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:lease.close();continue
                    fresh=inventory().get(index)
                    if not fresh or fresh['uuid']!=row['uuid'] or fresh['free_mib']<policy['minimum_free_mib']:
                        lease.close();stable[index]=0;continue
                    env=dict(os.environ,CUDA_VISIBLE_DEVICES=row['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',
                        HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                        OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
                    command=[sys.executable,'-u',str(out/'source/script'/Path(__file__).name),'worker',
                        '--out',str(out),'--shard',str(shard)]
                    (out/'logs').mkdir(exist_ok=True)
                    with (out/'logs'/f'{shard}.log').open('a') as log:
                        proc=subprocess.Popen(command,cwd=out/'source',env=env,stdin=subprocess.DEVNULL,
                            stdout=log,stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
                    running[shard]=proc;leases[shard]=lease
                    write(out/'launches'/f'{shard}.json',dict(worker_pid=proc.pid,gpu_index=index,gpu_uuid=row['uuid'],
                        shard=shard,command=command,updated_at=now()));stable[index]=0;break
            summarize(out)
            write(out/'queue_status.json',dict(state='running' if running else 'waiting_for_gpu_memory',
                active_shards={str(s):dict(pid=p.pid,**{k:v for k,v in read(out/'launches'/f'{s}.json').items() if k in ['gpu_index','gpu_uuid']})
                    for s,p in running.items()},finished_shards=finished,gpus=rows,stable_observations=stable,updated_at=now()))
            if len(finished)<2:time.sleep(policy['poll_seconds'])
        result=summarize(out);ok=all(c==0 for c in finished.values()) and result['complete']
        write(out/'queue_status.json',dict(state='complete' if ok else 'failed',finished_shards=finished,updated_at=now()))
        return 0 if ok else 2


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['prepare','queue','worker','report'])
    p.add_argument('--out',type=Path,required=True);p.add_argument('--prior',type=Path)
    p.add_argument('--shard',type=int,choices=[0,1],default=0);p.add_argument('--detach',action='store_true')
    args=p.parse_args(argv);out=args.out.resolve()
    try:
        if args.mode=='prepare':
            if not args.prior:raise ValueError('--prior is required')
            prepare(args.prior,out)
            if args.detach:
                command=[sys.executable,'-u',str(out/'source/script'/Path(__file__).name),'queue','--out',str(out)]
                with (out/'controller.log').open('a') as log:
                    proc=subprocess.Popen(command,cwd=out/'source',stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                write(out/'controller_launch.json',dict(pid=proc.pid,command=command,updated_at=now()))
            return 0
        if args.mode=='report':summarize(out);return 0
        return worker(out,args.shard) if args.mode=='worker' else queue(out)
    except Exception:
        if out.exists():write(out/f'failure_{args.mode}_{args.shard}.json',dict(traceback=traceback.format_exc(),updated_at=now()))
        raise


if __name__=='__main__':raise SystemExit(main())
