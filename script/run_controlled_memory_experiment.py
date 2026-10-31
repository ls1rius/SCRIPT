"""Schedule original-protocol A/B/C evidence comparisons with frozen runtimes."""
import argparse
from collections import Counter
from copy import deepcopy
import fcntl
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

sys.path.insert(0,str(Path(__file__).resolve().parent))
import controlled_memory_io as io


def verify(out):
    for name,expected in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=expected:raise ValueError('Frozen artifact changed: '+name)


def semantic_runtime(out):
    sys.path.insert(0,str(out/'runtime/semantic'))
    return importlib.import_module('script.semantic_evidence_runtime')


def validate_memory(memory,bundle,r):
    importlib.import_module('script_core.semantic_memory').validate_coverage(bundle,memory)
    sources={u['id']:u for u in memory['sources']}
    for kind,collection in io.COLLECTIONS.items():
        for f in memory[collection]:
            fact=dict(kind=kind,**{k:v for k,v in f.items() if k not in ('id','kind')})
            passed,reason=r.binding_supported(fact,sources[f['source_id']]['text'])
            if not passed:raise ValueError('Stored fact fails original binding validator: '+str(reason))


def render(out,uid,r):
    folder=out/'materialized'/uid
    memory=io.read(folder/'memory.json');bundle=io.read(out/'normalization_sources'/f'{uid}.json')
    validate_memory(memory,bundle,r)
    original=io.read(out/'final_inputs/A_original'/f'{uid}.json')
    lossless=io.read(out/'final_inputs/B_lossless_layout'/f'{uid}.json')
    inputs,audit=io.semantic_input(original,lossless,memory,bundle)
    io.write(out/'final_inputs/C_semantic'/f'{uid}.json',inputs)
    io.write(out/'render_audits'/f'{uid}.json',audit)
    io.write(folder/'sealed.json',{name:io.sha(folder/name) for name in
             ('memory.json','audit.json','cost_summary.json','calls.jsonl') if (folder/name).exists()})


def blind_audit(out,pilot,r):
    pool={}
    for uid in io.read(pilot/'cohort.json'):
        memory=io.read(pilot/'materialized'/uid/'memory.json')
        for source in memory['sources']:
            if source['source_type']!='caption':continue
            key=io.digest([source['text'],source['status']])
            pool.setdefault(key,(memory,source))
    # Match the source-only audit ordering established before looking at benchmark outcomes.
    keys=sorted(pool,key=lambda k:__import__('hashlib').sha256(
        (pool[k][1]['text']+'\0'+pool[k][1]['status']).encode()).hexdigest())[:100]
    entries=[];reasons=Counter()
    for i,key in enumerate(keys):
        memory,source=pool[key];evidence,rejected=io.projected_evidence(memory,source)
        reasons.update(row['reason'] for row in rejected)
        entries.append(dict(audit_id=f'S{i:03}',source_text=source['text'],
            source_hash=io.digest([source['text'],source['status']]),
            projected_evidence=evidence,quarantined_records=rejected,
            review='Source-only review: retained fields supported; missing/uncertain details remain literal.'))
    record=dict(passed=len(entries)==100,reviewer='assistant_source_only',updated_at=io.now(),
        pool='All caption sources in the fixed first-20 pilot, deduplicated by text and status.',
        selection='First 100 by SHA256(text + NUL + status), independent of benchmark outcomes.',
        questions_options_labels_shown=False,source_count=len(entries),entries=entries,
        quarantine_reasons=dict(reasons),
        limitation='A source-only audit of 100 units is not an exhaustive independent semantic verification.')
    io.write(out/'validation/blind_source_audit.json',record)
    return record


def prepare(design,semantic,pilot,out,detach):
    cpu_started=time.process_time()
    if out.exists():raise ValueError('Use a fresh experiment directory')
    for root in (design,semantic,pilot):
        for name,expected in io.read(root/'frozen_hashes.json').items():
            if io.sha(root/name)!=expected:raise ValueError('Source artifact changed: '+str(root/name))
    plan=io.read(design/'experiment.json');out.mkdir(parents=True)
    for name in ('cohort.json','smoke_cohort.json','evaluation_only.json','historical_only.json','source_hashes.json'):
        shutil.copy2(design/name,out/name)
    for name in ('final_inputs','normalization_sources'):
        shutil.copytree(design/name,out/name)
    original=Path(plan['original_run']);packets=Path(plan['captured_input_source'])/'inputs'
    shutil.copytree(packets,out/'packets')
    for label,source in [('qa',Path(plan['original_source'])),('semantic',semantic/'source')]:
        shutil.copytree(source,out/'runtime'/label,symlinks=True,ignore=shutil.ignore_patterns('__pycache__'))
    for label,source in [('qa',original),('semantic',semantic)]:
        cfg=io.read(source/'config.json')
        cfg['cache']['persistent_directory']='';cfg['compatibility']['cache_directory']=''
        cfg['query']['tool_transient_retries']=0;cfg['query']['json_repair_retries']=0
        io.write(out/f'{label}_config.json',cfg)
        shutil.copy2(source/'control_artifacts.json',out/f'{label}_expected_artifacts.json')
    for name in ('run_controlled_memory_experiment.py','controlled_memory_io.py'):
        target=out/'control'/name;target.parent.mkdir(exist_ok=True)
        shutil.copy2(Path(__file__).parent/name,target)
    project=Path(__file__).resolve().parents[1]
    for src,name in [(project/'docs/Controlled_Memory_Experiment_2026-10-28.md','experiment_design.md'),
                     (project/'docs/Semantic_Memory_Data_Structure_2026-10-28.md','data_structure.md')]:
        shutil.copy2(src,out/name)
    shutil.copytree(pilot/'functional',out/'functional',ignore=shutil.ignore_patterns('raw_calls','calls.jsonl'))
    shutil.copy2(pilot/'functional_input.json',out/'functional_input.json')
    r=semantic_runtime(out)
    gate=r.functional_gate(io.read(out/'functional/memory.json'),io.read(out/'functional/audit.json'))
    if not gate['passed']:raise ValueError('Frozen normalizer functional examples failed')
    io.write(out/'validation/functional_reuse.json',dict(passed=True,source=str(pilot/'functional'),
        normalizer_sha256=io.sha(out/'runtime/semantic/script_core/semantic_memory.py'),
        pilot_normalizer_sha256=io.sha(pilot/'source/script_core/semantic_memory.py'),
        gate=gate,new_model_calls=0,
        measured_seconds=sum(float(json.loads(line).get('actual_cost') or 0)
            for line in (pilot/'functional/calls.jsonl').read_text().splitlines())))
    if io.sha(out/'runtime/semantic/script_core/semantic_memory.py')!=io.sha(pilot/'source/script_core/semantic_memory.py'):
        raise ValueError('Functional examples came from another normalizer')
    reused=0;source_runs=set()
    for uid in io.read(out/'cohort.json'):
        src=semantic/'materialized'/uid
        if not all((src/name).exists() for name in ('memory.json','audit.json','cost_summary.json','calls.jsonl')):continue
        if io.sha(semantic/'source_bundles'/f'{uid}.json')!=io.sha(out/'normalization_sources'/f'{uid}.json'):
            raise ValueError('Reuse normalization source differs')
        dest=out/'materialized'/uid;dest.mkdir(parents=True)
        for name in ('memory.json','audit.json','cost_summary.json','calls.jsonl'):shutil.copy2(src/name,dest/name)
        seconds=sum(float(json.loads(line).get('actual_cost') or 0) for line in (dest/'calls.jsonl').read_text().splitlines())
        io.write(dest/'reuse.json',dict(source=str(src.resolve()),measured_seconds=seconds,
            copied_files={name:io.sha(dest/name) for name in ('memory.json','audit.json','cost_summary.json','calls.jsonl')},
            raw_calls_source=str((src/'raw_calls').resolve()),
            policy='Immutable memory/audit/call-ledger snapshot; prior extraction is counted, final answers are not reused.'))
        source_runs.add(src.resolve().parents[1])
        render(out,uid,r);reused+=1
    audit=blind_audit(out,pilot,r)
    if not audit['passed']:raise ValueError('Source audit is incomplete')
    request=dict(created_at=io.now(),planned=300,arms=list(io.ARMS),design=str(design),
        original_reference='186/300, original prompt and legacy parser; fresh replay required',
        main_comparison='C_semantic versus A_original',
        execution_overrides='All arms: persistent answer cache disabled and failure retries disabled. Models/generation/prompt/parser unchanged.',
        scope='Only Description evidence changes; old temporal memory and visual observations remain fixed.',
        reused_normalizations=reused,new_final_answers_reused=0,source_only_audit_passed=True,
        reused_setup_seconds_by_source={str(root):sum(float(json.loads(line).get('actual_cost') or 0)
            for f in (root/'setup').rglob('calls.jsonl') for line in f.read_text().splitlines()) for root in source_runs},
        prepare_cpu_seconds=time.process_time()-cpu_started,
        minimum_free_mib=43008,stable_observations=3,poll_seconds=10,
        allowed_gpus=['0','1','2','3'],preferred_gpu='2',batch_size=20,
        failures='Answer/schema/context errors preserved, counted wrong, then continue; infrastructure failures stop with full cost records.',
        renderer_guards='Quarantine unsupported roles and preserve entire original source; never silently relabel facts.',
        development_cohort=True)
    io.write(out/'experiment.json',request)
    # Every future generated input gets a per-question seal; this manifest freezes prepared inputs and code.
    frozen={str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file() and not p.is_symlink()}
    io.write(out/'frozen_hashes.json',frozen);verify(out);io.report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    if detach:
        command=[sys.executable,'-u',str(out/'control/run_controlled_memory_experiment.py'),'queue','--out',str(out)]
        with (out/'controller.log').open('a') as log:
            proc=subprocess.Popen(command,cwd=out,stdin=subprocess.DEVNULL,stdout=log,
                                  stderr=subprocess.STDOUT,start_new_session=True)
        io.write(out/'controller.json',dict(pid=proc.pid,command=command,started_at=io.now()))
        print(json.dumps(dict(out=str(out),controller_pid=proc.pid,reused_normalizations=reused,source_audit=100)),flush=True)


def warm_backend(out,mode,label):
    sys.path.insert(0,str(out/'runtime'/mode))
    from script_core.backend import preflight
    from script_core.config import load_config
    from script_core.budget import BudgetLedger,Meter,ModelWorker
    cfg,artifacts=preflight(load_config(out/f'{mode}_config.json'))
    old=io.read(out/f'{mode}_expected_artifacts.json')
    for k in ('vlm','visual_encoder','detector','text_embedding'):
        if artifacts[k]['hash']!=old[k]['hash']:raise ValueError('Checkpoint differs: '+k)
    if artifacts['software']!=old['software']:raise ValueError('Runtime software differs')
    io.write(out/'artifacts'/f'{label}.json',artifacts)
    backend=ModelWorker(cfg)
    folder=out/'setup'/label;meter=Meter(backend,BudgetLedger(600,answer_reserve=0,calls_path=folder/'calls.jsonl'),cfg,folder/'raw_calls')
    try:meter.call('prepare_vlm',{},build=True)
    except Exception:
        backend.close();raise
    finally:
        io.write(folder/'cost_summary.json',meter.ledger.summary());meter.close()
    return cfg,backend,BudgetLedger,Meter


def is_infrastructure(error):
    text=str(error).lower()
    return any(word in text for word in ('cuda','out of memory','worker','timeout','deadline','exceeded reserved'))


def qa_worker(out,start,end,phase):
    verify(out)
    cfg,backend,Ledger,Meter=warm_backend(out,'qa',phase)
    ids=io.read(out/'cohort.json');history=io.read(out/'historical_only.json')
    try:
        for index in range(start,end):
            uid=ids[index]
            arms=(io.ARMS[0],) if phase=='baseline_smoke' else io.ORDERS[index%6]
            for arm in arms:
                if phase=='treatment_smoke' and arm==io.ARMS[0]:continue
                folder=out/'runs'/arm/uid
                if (folder/'result.json').exists():continue
                if folder.exists():raise ValueError('Partially spent QA folder: '+str(folder))
                inputs=io.read(out/'final_inputs'/arm/f'{uid}.json')
                original=io.read(out/'final_inputs/A_original'/f'{uid}.json')
                if io.fixed(inputs)!=io.fixed(original):raise ValueError('Fixed QA contract changed')
                io.write(out/'progress.json',dict(stage=phase,question_id=uid,index=index,arm=arm,planned=300,updated_at=io.now()))
                ledger=Ledger(cfg['budget']['online_cap'],answer_reserve=cfg['budget']['final_answer_reserve'],calls_path=folder/'calls.jsonl')
                meter=Meter(backend,ledger,cfg,folder/'raw_calls');began=time.monotonic()
                result=dict(question_id=uid,arm=arm,selected_option=None,error=None,input_sha256=io.sha(out/'final_inputs'/arm/f'{uid}.json'))
                try:
                    checked,_=meter.call('temporal_qa_check',inputs,final=True)
                    output,_=meter.call('videotree_qa',inputs,final=True)
                    result.update(selected_option=output['data']['selected_option'],raw_response=output['data']['text'],input_tokens=checked['input_tokens'])
                    packet=io.read(out/'packets'/f'{uid}.json')
                    result['strict_audit']=io.strict_audit(result['raw_response'],packet['packet']['question']['options'])
                except Exception as exc:result['error']=dict(type=type(exc).__name__,message=str(exc))
                finally:
                    result.update(seconds=time.monotonic()-began,cost_summary=ledger.summary())
                    io.write(folder/'result.json',result);meter.close()
                print('CONTROLLED_QA',index+1,arm,'option',result['selected_option'],'error',result['error'],flush=True)
                if result['error'] and is_infrastructure(result['error']):return 2
        if phase=='baseline_smoke':
            mismatches=[]
            for uid in ids[:20]:
                result=io.read(out/'runs/A_original'/uid/'result.json')
                if result['error'] or str(result['selected_option'])!=str(history[uid]['selected_option']):
                    mismatches.append(dict(question_id=uid,expected=history[uid]['selected_option'],actual=result))
            io.write(out/'validation/baseline_reproduction.json',dict(passed=not mismatches,checked=20,
                accuracy_used_as_gate=False,mismatches=mismatches,updated_at=io.now()))
            if mismatches:return 4
        return 0
    finally:backend.close()


def normalize_worker(out,start,end,phase):
    verify(out);ids=io.read(out/'cohort.json')
    pending=[u for u in ids[start:end] if not (out/'final_inputs/C_semantic'/f'{u}.json').exists()]
    if not pending:return 0
    cfg,backend,Ledger,Meter=warm_backend(out,'semantic',phase)
    r=importlib.import_module('script.semantic_evidence_runtime')
    try:
        for uid in pending:
            folder=out/'materialized'/uid
            if folder.exists():raise ValueError('Partially spent normalization folder: '+uid)
            io.write(out/'progress.json',dict(stage=phase,question_id=uid,index=ids.index(uid),planned=300,updated_at=io.now()))
            ledger=Ledger(1500,answer_reserve=45,calls_path=folder/'calls.jsonl')
            meter=Meter(backend,ledger,cfg,folder/'raw_calls')
            try:memory,audit=r.normalize(io.read(out/'normalization_sources'/f'{uid}.json'),meter)
            finally:
                io.write(folder/'cost_summary.json',ledger.summary());meter.close()
            io.write(folder/'memory.json',memory);io.write(folder/'audit.json',audit)
            render(out,uid,r)
            print('CONTROLLED_NORMALIZE',ids.index(uid)+1,uid,audit['facts_by_type'],flush=True)
        return 0
    finally:backend.close()


def inventory():
    text=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.free,utilization.gpu','--format=csv,noheader,nounits'],text=True)
    result={}
    for line in text.splitlines():
        index,uuid,free,util=[v.strip() for v in line.split(',')]
        result[index]=dict(uuid=uuid,free_mib=int(free),utilization=int(util))
    return result


def queue(out):
    verify(out);policy=io.read(out/'experiment.json')
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Experiment already attempted')
        stable={}
        while True:
            rows=inventory();stable={i:stable.get(i,0)+1 if row['free_mib']>=policy['minimum_free_mib'] else 0 for i,row in rows.items() if i in policy['allowed_gpus']}
            chosen=None
            for i in sorted(stable,key=lambda i:(i!=policy['preferred_gpu'],-rows[i]['free_mib'])):
                if stable[i]<policy['stable_observations']:continue
                candidate=(Path('/tmp')/f'videoagent-finalqa-{os.getuid()}-{rows[i]["uuid"]}.lock').open('a')
                try:fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:candidate.close();continue
                fresh=inventory().get(i)
                if not fresh or fresh['uuid']!=rows[i]['uuid'] or fresh['free_mib']<policy['minimum_free_mib']:
                    candidate.close();continue
                chosen=i;lease=candidate;row=fresh;break
            if chosen is not None:break
            io.write(out/'queue_status.json',dict(state='waiting_for_gpu_memory',minimum_free_mib=policy['minimum_free_mib'],
                stable_observations=stable,gpus=rows,controller_pid=os.getpid(),updated_at=io.now()))
            time.sleep(policy['poll_seconds'])
        env=dict(os.environ,CUDA_VISIBLE_DEVICES=row['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',HF_HUB_OFFLINE='1',
            TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
        io.write(out/'launch.json',dict(gpu_index=chosen,gpu_uuid=row['uuid'],started_at=io.now(),controller_pid=os.getpid()))
        stages=[('qa',0,20,'baseline_smoke'),('qa',0,20,'treatment_smoke')]
        for start in range(20,300,20):stages.extend([('normalize',start,min(start+20,300),f'normalize_{start}'),('qa',start,min(start+20,300),f'qa_{start}')])
        try:
            for mode,start,end,phase in stages:
                command=[sys.executable,'-u',str(Path(__file__).resolve()),mode,'--out',str(out),
                    '--start',str(start),'--end',str(end),'--phase',phase]
                logpath=out/'logs'/f'{phase}.log';logpath.parent.mkdir(exist_ok=True)
                with logpath.open('a') as log:
                    proc=subprocess.Popen(command,cwd=out,env=env,stdin=subprocess.DEVNULL,stdout=log,
                                          stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
                io.write(out/'workers'/f'{phase}.json',dict(pid=proc.pid,command=command,started_at=io.now()))
                while proc.poll() is None:
                    io.report(out);io.write(out/'queue_status.json',dict(state='running',phase=phase,worker_pid=proc.pid,
                        controller_pid=os.getpid(),gpu_index=chosen,updated_at=io.now()));time.sleep(10)
                io.report(out)
                if proc.returncode:
                    io.write(out/'queue_status.json',dict(state='baseline_reproduction_failed' if proc.returncode==4 else 'failed',
                        phase=phase,returncode=proc.returncode,updated_at=io.now()))
                    return proc.returncode
            result=io.report(out)
            io.write(out/'queue_status.json',dict(state='complete' if result['complete'] else 'failed',updated_at=io.now()))
            return 0 if result['complete'] else 2
        finally:lease.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['prepare','queue','qa','normalize','report'])
    p.add_argument('--out',type=Path,required=True);p.add_argument('--design',type=Path);p.add_argument('--semantic',type=Path)
    p.add_argument('--pilot',type=Path);p.add_argument('--detach',action='store_true')
    p.add_argument('--start',type=int,default=0);p.add_argument('--end',type=int,default=20);p.add_argument('--phase',default='baseline_smoke')
    args=p.parse_args();out=args.out.resolve()
    try:
        if args.mode=='prepare':return prepare(args.design.resolve(),args.semantic.resolve(),args.pilot.resolve(),out,args.detach)
        if args.mode=='queue':return queue(out)
        if args.mode=='qa':return qa_worker(out,args.start,args.end,args.phase)
        if args.mode=='normalize':return normalize_worker(out,args.start,args.end,args.phase)
        io.report(out);return 0
    except Exception as exc:
        io.write(out/'failures'/f'{args.mode}_{args.phase}.json',dict(type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc(),updated_at=io.now()))
        raise


if __name__=='__main__':raise SystemExit(main())
