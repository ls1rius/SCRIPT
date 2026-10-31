"""Paired final-QA experiment on frozen temporal memory and visual observations.

The two new arms share instructions, memory, visual observations, option order,
model and parser. Only the supplied original-caption quotations differ. Saved
historical predictions are an additional reference, never an inference input.
"""
import argparse
from copy import deepcopy
import csv
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script_core.config import load_config,digest
from script_core.memory_evidence import supporting_quotes,qa_inputs
from script_core.state import write_json as write
from script.run_adaptive_comparison import now,read,sha
from script.run_followup_validation import snapshot,verify

ARMS=('matched_full','memory_quotes')


def strict_prediction(text):
    """A character named C in a sentence must never become option C."""
    clean=text.replace('**','').replace('`','').strip()
    choices={c.upper() for c in re.findall(r'(?im)^\s*prediction\s*:\s*([A-E])\s*[.]?\s*$',clean)}
    first=re.fullmatch(r'([A-E])[.]?',clean.split('\n',1)[0])
    if first:choices.add(first.group(1))
    if len(choices)!=1:raise ValueError('Final QA needs one unambiguous single-letter prediction')
    return str(ord(next(iter(choices)).upper())-ord('A'))


def without_quotes(inputs):
    value=deepcopy(inputs);prompt=value['prompts'][0]
    marker='\n\nSupporting original caption quotations:\n'
    start=prompt.index(marker)+len(marker)
    _,length=json.JSONDecoder().raw_decode(prompt[start:])
    value['prompts'][0]=prompt[:start]+'[]'+prompt[start+length:]
    return value


def final_inputs(packet,context,visual,maximum=12,per_fact=2):
    """Build a true one-variable pair: only the quotation JSON differs."""
    captions=packet['captions'];selected=context['selected_seconds'];memory=context['context']
    limited=supporting_quotes(captions,selected,memory,maximum,per_fact)
    all_cited=supporting_quotes(captions,selected,memory,len(captions),len(captions))
    support={q['caption_id']:q['supports'] for q in all_cited['quotes']}
    all_quotes=[dict(caption_id=captions[i]['id'],interval=deepcopy(captions[i]['interval']),
        text=captions[i]['text'],supports=support.get(captions[i]['id'],[])) for i in sorted(selected)]
    policy=dict(mode='paired_final_evidence',selected_caption_count=len(selected),
        empty_memory=limited['empty_memory'],
        source_policy='Use the supplied original quotations and memory. The memory and video evidence are partial; '
            'an absent quotation is not evidence that an event did not happen.')
    query=dict(packet['question'],id=packet['question']['question_id'])
    inputs={arm:qa_inputs(query,memory,dict(policy,quotes=quotes),visual)
        for arm,quotes in [('matched_full',all_quotes),('memory_quotes',limited['quotes'])]}
    if without_quotes(inputs['matched_full'])!=without_quotes(inputs['memory_quotes']):
        raise ValueError('Matched final-QA instructions or non-quotation evidence differ')
    return inputs,limited


def prepare(prior,out,minimum_free_mib=32*1024):
    prior=Path(prior).resolve();out=Path(out).resolve()
    if out.exists():raise ValueError('Use a fresh experiment directory')
    ids=read(prior/'cohorts.json')['300']
    if len(ids)!=300 or len(set(ids))!=300:raise ValueError('Require the complete fixed prior 300')
    cfg=load_config(ROOT/'configs/script_memory_quotes.yaml')
    source_cfg=read(prior/'config.json')
    cfg['models']=deepcopy(source_cfg['models']);cfg['seed']=source_cfg['seed']
    cfg['compatibility']['cache_directory']='';cfg['cache']['persistent_directory']=''
    cfg['query']['tool_transient_retries']=0
    out.mkdir(parents=True)
    controls={};origins={};structure={};truth=read(prior/'evaluation_only.json')
    for uid in ids:
        result_path=prior/'runs/script'/uid/'result.json';result=read(result_path)
        if result.get('error') or not result.get('answer'):raise ValueError('Missing completed control: '+uid)
        directory=result_path.resolve().parent
        context_path=directory/'qa_context.json'
        if not context_path.exists():context_path=directory/'partial_temporal/qa_context.json'
        context=read(context_path);packet=read(prior/'inputs'/f'{uid}.json')
        # Freeze only answer-free inputs; no source prediction/explanation enters these files.
        packet=dict(question=packet['question'],captions=packet['captions'])
        visual=result['supplemental_visual']['observations']
        inputs,audit=final_inputs(packet,context,visual)
        write(out/'inputs'/f'{uid}.json',dict(question=packet['question'],qa_inputs=inputs,
            evidence_selection=audit,memory_hash=digest(context['context']),visual_hash=digest(visual),
            selected_seconds=context['selected_seconds']))
        controls[uid]=dict(selected_option=result['answer']['selected_option'],error=None,
            response=result['answer'].get('text',''))
        origins[uid]=dict(result=str(result_path.resolve()),result_sha256=sha(result_path),
            context=str(context_path),context_sha256=sha(context_path))
        structure[uid]=dict(empty_memory=audit['empty_memory'],visual_observations=len(visual),
            original_captions=len(context['selected_seconds']),quotes=len(audit['quotes']))
    write(out/'cohort.json',ids);write(out/'controls.json',controls)
    write(out/'evaluation_only.json',{u:truth[u] for u in ids})
    write(out/'source_origins.json',origins);write(out/'structure.json',structure)
    write(out/'config.json',cfg);write(out/'control_artifacts.json',read(prior/'artifacts.json'))
    write(out/'experiment.json',dict(created_at=now(),prior=str(prior),planned=300,arms=list(ARMS),
        intervention='Only supplied original-caption quotation JSON differs between the two new arms.',
        fixed='Same saved final memory, accepted visual observations, question, options, system/user template and strict parser.',
        max_quotes=12,quotes_per_fact=2,upstream_rerun=False,typed_layout=False,
        historical_control='Saved original temporal results, secondary reference with different prompt/parser.',
        primary_comparison='memory_quotes versus matched_full; both freshly generated.',
        order='Alternate arm order per question; cohort order fixed without outcome selection.',
        parser='Strict single-letter prediction; legacy backend prediction retained for audit.',
        failures='Count wrong; no automatic retries or deletion of spent attempts.',
        timing='Final QA only; upstream preparation reused. Shared GPU load prevents controlled speed claims.',
        development_cohort=True))
    write(out/'resource_policy.json',dict(gpu_index='0',minimum_free_mib=minimum_free_mib,
        stable_observations=3,poll_seconds=10,shared_gpu=True,
        note='Final-QA-only worker; admission uses currently free memory. Existing tasks are not stopped.'))
    snapshot(ROOT,out/'source')
    paths=[p for p in out.rglob('*') if p.is_file() and not any(
        part in ('data','checkpoints','__pycache__') for part in p.relative_to(out).parts)]
    write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in paths})
    verify(out);summarize(out)
    write(out/'queue_status.json',dict(state='prepared',updated_at=now()))


def summarize(out):
    out=Path(out);ids=read(out/'cohort.json');truth=read(out/'evaluation_only.json')
    historical=read(out/'controls.json');structure=read(out/'structure.json')
    rows={arm:{u:read(p) for u in ids if (p:=out/'runs'/arm/u/'result.json').exists()} for arm in ARMS}
    def correct(row,uid):return not row.get('error') and str(row.get('selected_option'))==str(truth[uid])
    def stats(data,cohort):
        present=[u for u in cohort if u in data];n=len(present)
        c=sum(correct(data[u],u) for u in present)
        return dict(planned=len(cohort),completed=n,pending=len(cohort)-n,correct=c,
            errors=sum(bool(data[u].get('error')) for u in present),
            accuracy=c/len(cohort) if n==len(cohort) and cohort else None,
            accuracy_among_completed=c/n if n else None,
            mean_qa_seconds=sum(data[u].get('seconds',0) for u in present)/n if n else None)
    paired=[u for u in ids if all(u in rows[arm] for arm in ARMS)]
    fixed=[u for u in paired if correct(rows['memory_quotes'][u],u) and not correct(rows['matched_full'][u],u)]
    regressed=[u for u in paired if correct(rows['matched_full'][u],u) and not correct(rows['memory_quotes'][u],u)]
    discordant=len(fixed)+len(regressed)
    p=min(1.,2*sum(math.comb(discordant,k) for k in range(min(len(fixed),len(regressed))+1))/2**discordant) if discordant else 1.
    report=dict(updated_at=now(),planned=len(ids),paired_completed=len(paired),complete=len(paired)==len(ids),
        arms={arm:stats(rows[arm],ids) for arm in ARMS},historical=stats(historical,ids),
        paired=dict(corrected=fixed,regressed=regressed,corrected_count=len(fixed),regressed_count=len(regressed),
            net_correct=len(fixed)-len(regressed),
            accuracy_delta_percentage_points=(len(fixed)-len(regressed))/len(paired)*100 if paired else None,
            mcnemar_exact_p=p,partial=len(paired)!=len(ids)),
        groups={group:{arm:stats(rows[arm],[u for u in paired if predicate(structure[u])]) for arm in ARMS}
            for group,predicate in [('empty_memory',lambda s:s['empty_memory']),
                ('nonempty_memory',lambda s:not s['empty_memory']),
                ('with_visual',lambda s:s['visual_observations']>0),
                ('without_visual',lambda s:not s['visual_observations'])]},
        historical_changes={arm:dict(
            corrected=[u for u in rows[arm] if correct(rows[arm][u],u) and not correct(historical[u],u)],
            regressed=[u for u in rows[arm] if correct(historical[u],u) and not correct(rows[arm][u],u)]) for arm in ARMS},
        primary_comparison='Fresh matched_full versus fresh memory_quotes. Historical scores are a secondary reference.',
        development_cohort=True,timing='Final QA only on a shared GPU; no controlled speed claim.')
    write(out/'report.json',report)
    with (out/'per_question.csv.tmp').open('w',newline='') as f:
        columns=['question_id','truth','historical','matched_full','memory_quotes','empty_memory','quotes','original_captions']
        writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader()
        for u in ids:
            writer.writerow(dict(question_id=u,truth=truth[u],historical=historical[u]['selected_option'],
                **{arm:rows[arm].get(u,{}).get('selected_option') for arm in ARMS},
                **{k:structure[u][k] for k in ['empty_memory','quotes','original_captions']}))
    (out/'per_question.csv.tmp').replace(out/'per_question.csv')
    return report


def run_arm(out,uid,arm,inputs,backend,cfg):
    from script_core.budget import BudgetLedger,Meter
    target=out/'runs'/arm/uid
    if target.exists():raise ValueError('A spent final-QA attempt already exists: '+str(target))
    ledger=BudgetLedger(cfg['budget']['online_cap'],answer_reserve=cfg['budget']['final_answer_reserve'],
        calls_path=target/'calls.jsonl');meter=Meter(backend,ledger,cfg,target/'raw_calls')
    began=time.monotonic();result=dict(question_id=uid,arm=arm,selected_option=None,error=None,
        input_hash=digest(inputs),raw_response=None)
    try:
        checked,_=meter.call('temporal_qa_check',inputs,final=True)
        raw,_=meter.call('videotree_qa',inputs,final=True)
        response=raw['data']['text'];result['raw_response']=response
        result['legacy_selected_option']=raw['data']['selected_option']
        result['selected_option']=strict_prediction(response)
        result['parser_disagrees_with_legacy']=result['selected_option']!=raw['data']['selected_option']
        result['input_tokens']=checked['input_tokens']
    except Exception as exc:
        result['error']=dict(type=type(exc).__name__,message=str(exc))
    finally:
        result.update(seconds=time.monotonic()-began,cost_summary=ledger.summary())
        write(target/'result.json',result);meter.close()
    return result


def worker(out):
    from script_core.backend import preflight
    from script_core.budget import BudgetLedger,Meter,ModelWorker
    verify(out)
    write(out/'progress.json',dict(stage='preflight',updated_at=now(),pid=os.getpid(),completed_pairs=0,planned=300))
    print('Verifying frozen model checkpoints and runtime',flush=True)
    cfg,artifacts=preflight(load_config(out/'config.json'))
    old=read(out/'control_artifacts.json')
    for kind in ('vlm','visual_encoder','detector','text_embedding'):
        if artifacts[kind]['hash']!=old[kind]['hash']:raise ValueError('Checkpoint changed: '+kind)
    if artifacts['software']!=old['software']:raise ValueError('Model runtime software changed')
    write(out/'artifacts.json',artifacts);backend=ModelWorker(cfg)
    try:
        write(out/'progress.json',dict(stage='model_prepare',updated_at=now(),pid=os.getpid(),completed_pairs=0,planned=300))
        meter=Meter(backend,BudgetLedger(600,answer_reserve=0,calls_path=out/'setup/calls.jsonl'),cfg,out/'setup/raw_calls')
        try:meter.call('prepare_vlm',{},build=True)
        finally:write(out/'setup/cost_summary.json',meter.ledger.summary());meter.close()
        for i,uid in enumerate(read(out/'cohort.json')):
            row=read(out/'inputs'/f'{uid}.json')
            if without_quotes(row['qa_inputs'][ARMS[0]])!=without_quotes(row['qa_inputs'][ARMS[1]]):
                raise ValueError('Frozen paired input invariant failed')
            order=ARMS if i%2==0 else tuple(reversed(ARMS))
            for arm in order:
                write(out/'progress.json',dict(updated_at=now(),pid=os.getpid(),question_id=uid,arm=arm,
                    completed_pairs=i,planned=300,stage='final_qa'))
                result=run_arm(out,uid,arm,row['qa_inputs'][arm],backend,cfg)
                report=summarize(out)
                print('QA',i+1,arm,uid,'error',result['error'],'paired',report['paired_completed'],flush=True)
                if result['error']:
                    message=result['error']['message'].lower()
                    if any(s in message for s in ('cuda','out of memory','worker','deadline','timeout','exceeded reserved')):
                        write(out/'resource_failure.json',dict(question_id=uid,arm=arm,result=result,
                            automatic_retry=False,updated_at=now()));return 2
            if i==3:
                write(out/'verification/first_pairs.json',dict(pairs=4,passed=True,
                    fixed_nonquotation_inputs=True,accuracy_used_for_gate=False,updated_at=now()))
        report=summarize(out);write(out/'completion.json',dict(complete=report['complete'],updated_at=now()))
        return 0
    finally:backend.close()


def queue(out):
    from script.run_optimized_comparison import inventory
    verify(out);policy=read(out/'resource_policy.json');stable=0
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'worker_launch.json').exists():raise ValueError('Worker already attempted; inspect retained results')
        while True:
            row=inventory().get(policy['gpu_index'])
            stable=stable+1 if row and row['free_mib']>=policy['minimum_free_mib'] else 0
            write(out/'queue_status.json',dict(state='waiting_for_gpu_memory',updated_at=now(),pid=os.getpid(),
                gpu_index=policy['gpu_index'],gpu=row,stable_observations=stable,
                minimum_free_mib=policy['minimum_free_mib'],shared_gpu=True))
            if stable>=policy['stable_observations']:
                # A separate shared-QA lease serializes this new workload class;
                # the running original experiment is intentionally left intact.
                lease=(Path('/tmp')/f'videoagent-finalqa-{os.getuid()}-{row["uuid"]}.lock').open('a')
                try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:lease.close();stable=0;time.sleep(policy['poll_seconds']);continue
                fresh=inventory().get(policy['gpu_index'])
                if not fresh or fresh['uuid']!=row['uuid'] or fresh['free_mib']<policy['minimum_free_mib']:
                    lease.close();stable=0;continue
                env=dict(os.environ,CUDA_VISIBLE_DEVICES=row['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                    OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
                command=[sys.executable,'-u',str(out/'source/script'/Path(__file__).name),'worker','--out',str(out)]
                with (out/'worker.log').open('a') as log:
                    proc=subprocess.Popen(command,cwd=out/'source',env=env,stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
                launch=dict(state='running',updated_at=now(),controller_pid=os.getpid(),worker_pid=proc.pid,
                    gpu_index=policy['gpu_index'],gpu_uuid=row['uuid'],shared_gpu=True,command=command)
                write(out/'worker_launch.json',launch)
                while proc.poll() is None:
                    write(out/'queue_status.json',dict(launch,updated_at=now()));time.sleep(5)
                lease.close();write(out/'queue_status.json',dict(launch,
                    state='complete' if proc.returncode==0 else 'failed',exit_code=proc.returncode,updated_at=now()))
                return proc.returncode
            time.sleep(policy['poll_seconds'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--prior',type=Path)
    parser.add_argument('--detach',action='store_true')
    parser.add_argument('--minimum-free-mib',type=int,default=32*1024,
        help='GPU admission threshold for this final-QA-only worker')
    args=parser.parse_args(argv);out=args.out.resolve()
    try:
        if args.mode=='prepare':
            if not args.prior:raise ValueError('--prior is required for prepare')
            if args.minimum_free_mib<24*1024:raise ValueError('Final-QA admission needs at least 24 GiB free')
            prepare(args.prior,out,args.minimum_free_mib)
            if args.detach:
                command=[sys.executable,'-u',str(out/'source/script'/Path(__file__).name),'queue','--out',str(out)]
                with (out/'controller.log').open('a') as log:
                    proc=subprocess.Popen(command,cwd=out/'source',stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                write(out/'launch.json',dict(pid=proc.pid,command=command,updated_at=now()))
            return 0
        if args.mode=='report':summarize(out);return 0
        return queue(out) if args.mode=='queue' else worker(out)
    except Exception:
        if out.exists():write(out/'failure.json',dict(mode=args.mode,updated_at=now(),traceback=traceback.format_exc()))
        raise


if __name__=='__main__':raise SystemExit(main())
