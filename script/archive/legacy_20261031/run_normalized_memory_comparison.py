"""Scheme 2: normalize scheme 1's exact evidence, then generate one final answer."""
from pathlib import Path
import sys
_PROJECT_ROOT = next(p for p in Path(__file__).resolve().parents if (p/'script_core').is_dir())
sys.path[:0] = [str(_PROJECT_ROOT), str(_PROJECT_ROOT/'script')]

import argparse
from collections import Counter
from copy import deepcopy
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT=_PROJECT_ROOT
sys.path.insert(0,str(ROOT))
from script_core.config import load_config,digest
from script_core.normalized_memory import evidence_units,normalize
from script_core.memory_evidence import qa_inputs
from script.run_memory_quotes_comparison import run_arm,strict_prediction
from script.run_adaptive_comparison import now,read,sha
from script.run_followup_validation import snapshot,verify
from script_core.state import write_json as write


def prepare(prior,out,supersedes=None):
    prior=Path(prior).resolve();out=Path(out).resolve()
    if out.exists():raise ValueError('Use a fresh output directory')
    verify(prior);ids=read(prior/'cohort.json')
    if len(ids)!=300:raise ValueError('Require the same fixed 300 as scheme 1')
    out.mkdir(parents=True);source_hashes={}
    for uid in ids:
        row=read(prior/'inputs'/f'{uid}.json');source=row['qa_inputs']['memory_quotes']
        bundle=evidence_units(source)
        # Normalizer inputs contain no question, option or answer. They are
        # derived solely from the evidence actually shown in scheme 1.
        write(out/'normalization_sources'/f'{uid}.json',bundle)
        write(out/'questions'/f'{uid}.json',row['question'])
        source_hashes[uid]=dict(scheme1_input_sha256=sha(prior/'inputs'/f'{uid}.json'),
            scheme1_final_qa_input_hash=digest(source),normalization_bundle_hash=digest(bundle))
    cfg=load_config(prior/'config.json')
    cfg['query']['tool_transient_retries']=0;cfg['query']['json_repair_retries']=0
    cfg['cache']['persistent_directory']='';cfg['compatibility']['cache_directory']=''
    write(out/'config.json',cfg);write(out/'cohort.json',ids)
    write(out/'evaluation_only.json',read(prior/'evaluation_only.json'))
    write(out/'control_artifacts.json',read(prior/'control_artifacts.json'))
    write(out/'source_hashes.json',source_hashes)
    write(out/'controls_source.json',dict(directory=str(prior),
        arms=['matched_full','memory_quotes'],upstream_inputs_frozen=True))
    retained_attempts=[]
    if supersedes:
        previous=Path(supersedes).resolve()
        if not (previous/'superseded.json').exists():raise ValueError('Prior attempt needs an explicit superseded record')
        costs=[]
        for path in [previous/'setup/cost_summary.json',*previous.glob('normalization/*/cost_summary.json')]:
            if path.exists():costs.append(read(path).get('budgeted_total',0.))
        previous_results=list(previous.glob('runs/normalized/*/result.json'))
        costs.extend(read(p)['cost_summary']['budgeted_total'] for p in previous_results)
        retained_attempts.append(dict(directory=str(previous),completed=len(previous_results),
            measured_processing_seconds=sum(costs),
            interrupted_pending_calls=[str(p) for p in previous.glob('normalization/*/raw_calls/*.pending.json')],
            policy='Prototype and all raw calls retained; measured costs separate; interrupted calls are not free work.'))
    write(out/'retained_attempts.json',retained_attempts)
    write(out/'experiment.json',dict(created_at=now(),planned=300,scheme=2,prior=str(prior),
        method='normalized-evidence-v1-nullable-format-v2',primary_control='scheme 1 memory_quotes on identical evidence',
        normalization='Local Qwen typed atomic fields; source-bound literal spans, unknown roles null.',
        original_text='No separate quote block. Important or unrepresented content is retained under a semantic category.',
        source_policy='Only scheme 1 saved memory, its supplied quotations and accepted visual assertions. No new retrieval or pixels.',
        preservation='All evidence units represented by atomic facts or categorized original text; informative-word coverage checked.',
        options_in_normalizer=False,answers_in_normalizer=False,question_in_normalizer=False,
        question_and_options='Original order, only supplied to final QA.',
        batch_size=4,max_facts_per_batch=12,normalization_max_new_tokens=1536,
        final_qa_max_new_tokens=cfg['models']['generation']['max_new_tokens'],
        parser='Same strict single-letter parser as scheme 1.',
        failures='Preserved; no automatic restart. Normalization format failures retain categorized source text.',
        development_cohort=True))
    write(out/'resource_policy.json',dict(allowed_gpus=['0','1','2','3'],preferred_gpu='2',
        minimum_free_mib=28*1024,stable_observations=3,poll_seconds=10,shared_gpu=True,
        note='One persistent Qwen worker only; free-memory check and shared-QA lease; no existing task is stopped.'))
    snapshot(ROOT,out/'source')
    paths=[p for p in out.rglob('*') if p.is_file() and not any(
        x in ('data','checkpoints','__pycache__') for x in p.relative_to(out).parts)]
    write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in paths})
    verify(out);summarize(out)
    write(out/'queue_status.json',dict(state='prepared',updated_at=now()))


def summarize(out):
    out=Path(out);ids=read(out/'cohort.json');truth=read(out/'evaluation_only.json')
    prior=Path(read(out/'controls_source.json')['directory'])
    results={u:read(p) for u in ids if (p:=out/'runs/normalized'/u/'result.json').exists()}
    controls={arm:{u:read(p) for u in ids if (p:=prior/'runs'/arm/u/'result.json').exists()}
        for arm in ['matched_full','memory_quotes']}
    def correct(row,u):return not row.get('error') and str(row.get('selected_option'))==str(truth[u])
    def stats(rows,cohort):
        present=[u for u in cohort if u in rows];n=len(present);c=sum(correct(rows[u],u) for u in present)
        return dict(planned=len(cohort),completed=n,pending=len(cohort)-n,correct=c,
            errors=sum(bool(rows[u].get('error')) for u in present),
            accuracy=c/len(cohort) if n==len(cohort) and cohort else None,
            accuracy_among_completed=c/n if n else None)
    comparisons={}
    for arm,rows in controls.items():
        paired=[u for u in ids if u in rows and u in results]
        fixed=[u for u in paired if correct(results[u],u) and not correct(rows[u],u)]
        worse=[u for u in paired if correct(rows[u],u) and not correct(results[u],u)]
        n=len(fixed)+len(worse)
        comparisons[arm]=dict(paired_completed=len(paired),control=stats(rows,paired),normalized=stats(results,paired),
            corrected=fixed,regressed=worse,corrected_count=len(fixed),regressed_count=len(worse),
            accuracy_delta_percentage_points=(len(fixed)-len(worse))/len(paired)*100 if paired else None,
            mcnemar_exact_p=min(1.,2*sum(math.comb(n,k) for k in range(min(len(fixed),len(worse))+1))/2**n) if n else 1.,
            partial=len(paired)!=len(ids))
    audits={u:read(p) for u in ids if (p:=out/'normalization'/u/'audit.json').exists()}
    counts=Counter();categories=Counter();attributes=Counter();retained=Counter()
    for audit in audits.values():
        counts.update({k:audit[k] for k in ['source_units','normalized_facts','retained_units','raw_text_removed_units']})
        categories.update(audit['facts_by_type']);attributes.update(audit['attribute_fields']);retained.update(audit['retained_by_type'])
    report=dict(updated_at=now(),planned=len(ids),normalized=stats(results,ids),
        complete=len(results)==len(ids),comparisons=comparisons,
        representation=dict(completed=len(audits),counts=dict(counts),facts_by_type=dict(categories),
            attribute_fields=dict(attributes),retained_by_type=dict(retained),
            batch_failures=sum(len(a['batch_failures']) for a in audits.values()),
            rejected_records=sum(len(a['rejected_records']) for a in audits.values())),
        primary_comparison='normalized versus memory_quotes on the same completed question IDs.',
        raw_text_policy='Categorized source text is intentionally retained when atomic fields lose content.',
        normalization_processing_seconds=sum(r.get('normalization_seconds',0) for r in results.values()),
        final_qa_processing_seconds=sum(r.get('seconds',0) for r in results.values()),
        retained_attempts=read(out/'retained_attempts.json') if (out/'retained_attempts.json').exists() else [],
        representation_groups={group:{'normalized':stats(results,[u for u in results if predicate(audits.get(u,{}))]),
            'memory_quotes':stats(controls['memory_quotes'],[u for u in results if predicate(audits.get(u,{}))])}
            for group,predicate in [('zero_source_units',lambda a:a.get('source_units')==0),
                ('nonempty_fully_structured',lambda a:a.get('source_units',0)>0 and not a.get('retained_units')),
                ('categorized_originals_retained',lambda a:a.get('retained_units',0)>0)]},
        development_cohort=True,accuracy_uses_pending_as_separate=True)
    write(out/'report.json',report);return report


def worker(out):
    from script_core.backend import preflight
    from script_core.budget import BudgetLedger,Meter,ModelWorker
    verify(out)
    write(out/'progress.json',dict(stage='preflight',completed=0,planned=300,updated_at=now(),pid=os.getpid()))
    print('Verifying same local checkpoints and runtime',flush=True)
    cfg,artifacts=preflight(load_config(out/'config.json'));old=read(out/'control_artifacts.json')
    for kind in ('vlm','visual_encoder','detector','text_embedding'):
        if artifacts[kind]['hash']!=old[kind]['hash']:raise ValueError('Checkpoint changed: '+kind)
    if artifacts['software']!=old['software']:raise ValueError('Runtime software changed')
    write(out/'artifacts.json',artifacts);backend=ModelWorker(cfg)
    try:
        setup=Meter(backend,BudgetLedger(600,answer_reserve=0,calls_path=out/'setup/calls.jsonl'),cfg,out/'setup/raw_calls')
        try:setup.call('prepare_vlm',{},build=True)
        finally:write(out/'setup/cost_summary.json',setup.ledger.summary());setup.close()
        for i,uid in enumerate(read(out/'cohort.json')):
            sources=read(out/'normalization_sources'/f'{uid}.json');question=read(out/'questions'/f'{uid}.json')
            folder=out/'normalization'/uid
            if folder.exists():raise ValueError('Existing normalization attempt must retain spent work')
            write(out/'progress.json',dict(stage='normalizing',question_id=uid,completed=i,planned=300,updated_at=now(),pid=os.getpid()))
            ledger=BudgetLedger(cfg['budget']['online_cap'],answer_reserve=cfg['budget']['final_answer_reserve'],
                calls_path=folder/'calls.jsonl');meter=Meter(backend,ledger,cfg,folder/'raw_calls')
            began=time.monotonic()
            try:
                normalized,audit=normalize(sources,meter,batch_size=4)
                write(folder/'memory.json',normalized);write(folder/'audit.json',audit)
            finally:
                seconds=time.monotonic()-began
                write(folder/'cost_summary.json',ledger.summary());meter.close()
            # Same final instructions/parser as scheme 1. Raw sentences now
            # appear only inside a categorized retained_evidence collection.
            inputs=qa_inputs(dict(question,id=uid),normalized,dict(sources['qa_policy'],quotes=[]),[])
            write(out/'final_inputs'/f'{uid}.json',inputs)
            write(out/'progress.json',dict(stage='final_qa',question_id=uid,completed=i,planned=300,updated_at=now(),pid=os.getpid()))
            result=run_arm(out,uid,'normalized',inputs,backend,cfg)
            result.update(normalization_seconds=seconds,normalization_audit=audit,
                normalization_cost_summary=ledger.summary(),normalization_bundle_hash=digest(sources))
            write(out/'runs/normalized'/uid/'result.json',result);report=summarize(out)
            print('NORMALIZED_QA',i+1,uid,'facts',audit['normalized_facts'],'retained',audit['retained_units'],
                'units',audit['source_units'],'error',result['error'],flush=True)
            if result['error'] and any(x in result['error']['message'].lower() for x in (
                    'cuda','out of memory','worker','deadline','timeout','exceeded reserved')):
                write(out/'resource_failure.json',dict(question_id=uid,result=result,automatic_retry=False));return 2
            if i==3:
                audits=[read(out/'normalization'/u/'audit.json') for u in read(out/'cohort.json')[:4]]
                passed=all(a['all_source_units_accounted_for'] for a in audits) and sum(a['normalized_facts'] for a in audits)>0
                write(out/'verification/first_cases.json',dict(passed=passed,accuracy_used_for_gate=False,
                    evidence_preserved=True,normalized_facts=sum(a['normalized_facts'] for a in audits),updated_at=now()))
                if not passed:return 3
        report=summarize(out);write(out/'completion.json',dict(complete=report['complete'],updated_at=now()));return 0
    finally:backend.close()


def queue(out):
    from script.run_optimized_comparison import inventory
    verify(out);policy=read(out/'resource_policy.json');stable={}
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'worker_launch.json').exists():raise ValueError('Worker already attempted; no automatic restart')
        while True:
            rows={i:r for i,r in inventory().items() if i in policy['allowed_gpus']}
            stable={i:stable.get(i,0)+1 if r['free_mib']>=policy['minimum_free_mib'] else 0 for i,r in rows.items()}
            write(out/'queue_status.json',dict(state='waiting_for_gpu_memory',updated_at=now(),pid=os.getpid(),
                gpus=rows,stable_observations=stable,minimum_free_mib=policy['minimum_free_mib']))
            for index in sorted(rows,key=lambda i:(i!=policy['preferred_gpu'],-rows[i]['free_mib'])):
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
                command=[sys.executable,'-u',str(out/'source/script'/Path(__file__).name),'worker','--out',str(out)]
                with (out/'worker.log').open('a') as log:
                    proc=subprocess.Popen(command,cwd=out/'source',env=env,stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
                launch=dict(state='running',gpu_index=index,gpu_uuid=row['uuid'],controller_pid=os.getpid(),
                    worker_pid=proc.pid,command=command,updated_at=now(),shared_gpu=True)
                write(out/'worker_launch.json',launch)
                while proc.poll() is None:
                    write(out/'queue_status.json',dict(launch,updated_at=now()));time.sleep(5)
                write(out/'queue_status.json',dict(launch,state='complete' if proc.returncode==0 else 'failed',
                    exit_code=proc.returncode,updated_at=now()));lease.close();return proc.returncode
            time.sleep(policy['poll_seconds'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--prior',type=Path)
    parser.add_argument('--supersedes',type=Path,help='Retain and separately charge a stopped prototype attempt')
    parser.add_argument('--detach',action='store_true');args=parser.parse_args(argv);out=args.out.resolve()
    try:
        if args.mode=='prepare':
            if not args.prior:raise ValueError('--prior is required')
            prepare(args.prior,out,args.supersedes)
            if args.detach:
                command=[sys.executable,'-u',str(out/'source/script'/Path(__file__).name),'queue','--out',str(out)]
                with (out/'controller.log').open('a') as log:
                    proc=subprocess.Popen(command,cwd=out/'source',stdin=subprocess.DEVNULL,stdout=log,
                        stderr=subprocess.STDOUT,start_new_session=True)
                write(out/'launch.json',dict(pid=proc.pid,command=command,updated_at=now()))
            return 0
        if args.mode=='report':summarize(out);return 0
        return worker(out) if args.mode=='worker' else queue(out)
    except Exception:
        if out.exists():write(out/'failure.json',dict(mode=args.mode,traceback=traceback.format_exc(),updated_at=now()))
        raise


if __name__=='__main__':raise SystemExit(main())
