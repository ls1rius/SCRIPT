"""Prepare and run E with joint extraction and one resident model on GPU 0."""
import argparse
import ast
from copy import deepcopy
import fcntl
import importlib
import json
import os
import re
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
import controlled_memory_io as io

ARM='E_joint_entities'


def verify(out):
    for name,expected in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=expected:raise ValueError('Frozen E artifact changed: '+name)


def patch_runtime(runtime,core,semantic):
    """Extend only E's copied runtime; leave the original QA dispatch intact."""
    destination=runtime/'script_core'
    shutil.copy2(core,destination/'joint_entity_memory.py')
    shutil.copy2(semantic,destination/'semantic_memory.py')
    schemas=destination/'schemas.py';text=schemas.read_text()
    marker='def output_schema(operation, inputs):\n'
    assert text.count(marker)==1
    schemas.write_text(text.replace(marker,marker+"    if operation == 'e_joint_extract':\n        from .joint_entity_memory import output_schema as joint_schema\n        return joint_schema(inputs)\n",1))
    prompts=destination/'prompts.py'
    with prompts.open('a') as handle:
        handle.write("\nfrom .joint_entity_memory import PROMPT as E_JOINT_PROMPT\nCONTRACTS['e_joint_extract'] = E_JOINT_PROMPT\n")
    backend=destination/'backend.py';text=backend.read_text();before=ast.parse(text)
    marker="if operation in ('temporal_observe',"
    assert marker in text
    text=text.replace(marker,"if operation in ('e_joint_extract', 'temporal_observe',",1)
    marker="            if original_op == 'optimized_plan':\n"
    assert text.count(marker)==1
    text=text.replace(marker,"            if original_op == 'e_joint_extract':\n                kwargs['max_new_tokens'] = 1536\n"+marker,1)
    after=ast.parse(text)
    def dispatch(tree):
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='LocalModels')
        return ast.dump(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='call'),include_attributes=False)
    assert dispatch(before)==dispatch(after),'Original QA operation dispatch changed'
    backend.write_text(text)


def prepare(reference,D,out,prior_attempt=None):
    if out.exists():raise ValueError('Use a new E experiment directory')
    root=Path(__file__).resolve().parents[1]
    for name,expected in io.read(reference/'frozen_hashes.json').items():
        if io.sha(reference/name)!=expected:raise ValueError('Reference artifact changed: '+name)
    parent=Path(io.read(reference/'experiment.json')['parent'])
    prior_attempts=[]
    if prior_attempt is not None:
        prior_attempts=list(dict.fromkeys(io.read(prior_attempt/'experiment.json').get('prior_development_attempts',[])+[str(prior_attempt)]))
    assert io.read(reference/'validation/baseline_reproduction.json')['passed']
    out.mkdir(parents=True)
    for name in ['cohort.json','evaluation_only.json','historical_only.json','qa_config.json','qa_expected_artifacts.json']:
        shutil.copy2(reference/name,out/name)
    for name in ['packets','final_inputs/A_original']:
        shutil.copytree(reference/name,out/name)
    shutil.copytree(parent/'normalization_sources',out/'normalization_sources')
    shutil.copytree(parent/'final_inputs/B_lossless_layout',out/'source_observations')
    shutil.copytree(reference/'runtime/qa',out/'runtime/qa',symlinks=True,ignore=shutil.ignore_patterns('__pycache__'))
    patch_runtime(out/'runtime/qa',root/'script_core/joint_entity_memory.py',parent/'runtime/semantic/script_core/semantic_memory.py')
    control=out/'control';control.mkdir()
    for source,name in [(Path(__file__),Path(__file__).name),(reference/'control/compact_protocol.py','compact_protocol.py'),
        (reference/'control/controlled_memory_io.py','controlled_memory_io.py'),
        (root/'script/run_compact_resident.py','run_compact_resident.py')]:shutil.copy2(source,control/name)
    io.write(out/'experiment.json',dict(created_at=io.now(),planned=len(io.read(out/'cohort.json')),arm=ARM,
        reference_compact_run=str(reference),D_run=str(D),normalization_parent=str(parent),
        prior_development_attempts=prior_attempts,
        changes=['One source, seven categories in one model call; source-literal spans constrain output values; at most one role review.',
            'Local entity IDs from literal noun heads, compatible descriptors and bounded temporal context; unresolved identity remains separate.',
            'Entity-centered action/state timelines; raw clauses omitted only when an explicit full-clause grammar preserves their information.',
            'Source-only alias/merge/raw-retention strata reported separately on paired completed questions.'],
        max_identity_gap=8,max_state_gap=2,extraction_cap=1500,answer_reserve=45,
        resource_policy=dict(gpu_index='0',minimum_free_mib=24576,resident_until_complete=True),
        fixed='Same 300 questions, selected source captions, accepted visuals, QA model/precision/generation/prompt/parser. No new retrieval or images.',
        inference='Fresh E extraction and fresh E answers. Reference answers are comparison-only.',
        identity='Local text-based hypotheses, not independently verified physical identity.',
        failures='No automatic infrastructure retries, hidden context truncation, or answer fallback; source extraction failures retain the source clause.',
        subgroup_scope='Descriptive paired subgroups; bundled E changes do not isolate the causal contribution of an individual optimization.'))
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p)
        for p in out.rglob('*') if p.is_file() and not p.is_symlink()})
    verify(out);report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    print(json.dumps(dict(out=str(out),planned=300,state='prepared'),ensure_ascii=False))


def source_observations(out,uid,bundle):
    by_caption={}
    for unit in bundle['units']:
        for cid in unit['source_caption_ids']:
            if cid in by_caption:raise ValueError('Ambiguous E source-caption mapping')
            by_caption[cid]=unit
    raw=json.loads(io.parts(io.read(out/'source_observations'/f'{uid}.json'))[1])['observations']
    result=[]
    for row in raw:
        unit=by_caption[row['caption_id']]
        original=row['evidence']['original_text']
        if original.strip()!=unit['text'].strip() or row['sample_interval'] not in unit['source_intervals']:
            raise ValueError('E source text or time changed')
        result.append(dict(caption_id=row['caption_id'],frame_index=row['frame_index'],source_id=unit['id'],
            sample_interval=deepcopy(row['sample_interval']),original_text=original))
    if {r['caption_id'] for r in result}!=set(bundle['read_caption_ids']):raise ValueError('E source scope changed')
    return result


def extract_source(unit,meter,core,protocol):
    inputs=dict(unit=dict(id=unit['id'],text=core.clean_unit(unit)['text']),source_type='caption')
    if core.NEGATION.search(inputs['unit']['text']) or core.UNCERTAINTY.search(inputs['unit']['text']):
        return dict(accepted=dict(facts=[],mentions=[],rejected=[],uncertain=True),complete_simple=False,
            role_review=False,error=None,guarded_original=True)
    try:
        data,_=meter.json(core.OPERATION,inputs,tuple(core.COLLECTIONS.values())+('uncertain',))
        accepted=core.accept(unit,data)
        reviewed=False
        bad=[r for r in accepted['rejected'] if r['record']['kind']=='action']
        if bad:
            data,_=meter.json(core.OPERATION,dict(inputs,review=bad),tuple(core.COLLECTIONS.values())+('uncertain',))
            accepted=core.accept(unit,data);reviewed=True
        return dict(accepted=accepted,complete_simple=core.complete_simple_source(unit,accepted),
            role_review=reviewed,error=None)
    except Exception as exc:
        budget_limited=type(exc).__name__=='ResourceExhausted' or getattr(exc,'budget_limited',False)
        if protocol.is_infrastructure(exc) and not budget_limited:raise
        return dict(accepted=dict(facts=[],mentions=[],rejected=[],uncertain=True),complete_simple=False,
            role_review=False,error=dict(type=type(exc).__name__,message=str(exc)))


def functional(out,backend,cfg,Ledger,Meter,core,protocol):
    sentences=['#C C wipes the red book with a cloth.','#C C draws on paper with a pen.',
        '#C The book is wet.','#C C moves the book from the shelf into the bag with her left hand.',
        '#C The book changes from wet to dry.','#C The machine produces a basket.',
        '#C C folds the book then C moves the book.','#C C never wipes the book.',
        '#C The book is made of bamboo.','#C The book is on the table.']
    folder=out/'functional';ledger=Ledger(600,answer_reserve=0,calls_path=folder/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,folder/'raw_calls');results=[]
    try:
        for i,text in enumerate(sentences):
            value=extract_source(dict(id=f'U{i}',text=text),meter,core,protocol)
            results.append(value);io.write(folder/'source_results'/f'U{i}.json',value)
        expected=[('action',dict(actor='C',verb='wipes',patient='red book',instrument='cloth')),
            ('action',dict(actor='C',verb='draws',surface='paper',instrument='pen')),
            ('state',dict(entity='book',dimension='wetness',value='wet')),
            ('action',dict(source_location='shelf',destination='bag',body_part='left hand')),
            ('change',dict(entity='book',before='wet',after='dry')),
            ('result',dict(entity='machine',predicate='produces',value='basket')),
            ('event_link',dict(connector='then',link_type='before')),None,
            ('attribute',dict(entity='book',property='material',value='bamboo')),
            ('relation',dict(left='book',right='table',relation_type='spatial'))]
        failures=[]
        for i,item in enumerate(expected):
            if results[i]['error']:failures.append(dict(unit=i,error=results[i]['error']))
            elif item and not any(f['kind']==item[0] and all(
                re.sub(r'^(?:a|an|the|his|her)\s+','',f.get(k,''),flags=re.I)==v
                for k,v in item[1].items()) for f in results[i]['accepted']['facts']):
                failures.append(dict(unit=i,missing=item))
        if results[7]['accepted']['facts'] or results[7]['complete_simple']:failures.append(dict(unit=7,error='negative clause converted'))
        if any(f['kind']=='attribute' for f in results[9]['accepted']['facts']):
            failures.append(dict(unit=9,error='spatial preposition misclassified as attribute'))
        if len([f for f in results[6]['accepted']['facts'] if f['kind']=='action'])!=2:
            failures.append(dict(unit=6,error='multi-action clause lost an explicit action'))
        original=io.read(out/'final_inputs/A_original'/f'{io.read(out/"cohort.json")[0]}.json')
        checked,_=meter.call('temporal_qa_check',original)
        output,_=meter.call('videotree_qa',original)
        expected_answer=io.read(out/'historical_only.json')[io.read(out/'cohort.json')[0]]['selected_option']
        if str(output['data']['selected_option'])!=str(expected_answer):failures.append(dict(error='Original QA smoke prediction differs'))
        gate=dict(passed=not failures,failures=failures,source_examples=10,
            questions_shown_to_extractor=False,original_QA_smoke=dict(input_tokens=checked['input_tokens'],
            selected_option=output['data']['selected_option'],expected=expected_answer),updated_at=io.now())
        io.write(out/'validation/functional.json',gate)
        return gate
    finally:io.write(folder/'cost_summary.json',ledger.summary());meter.close()


def work(out):
    verify(out)
    sys.path.insert(0,str(out/'control'))
    import compact_protocol as protocol
    from run_compact_resident import ResidentBackend
    cfg,backend,Ledger,Meter=protocol.warm_backend(out,'qa','E_resident')
    core=importlib.import_module('script_core.joint_entity_memory')
    original_warm=protocol.warm_backend
    protocol.warm_backend=lambda *args: (cfg,ResidentBackend(backend),Ledger,Meter)
    protocol.io.ARMS=(ARM,);protocol.io.ORDERS=((ARM,),)*6
    try:
        gate_path=out/'validation/functional.json'
        gate=io.read(gate_path) if gate_path.exists() else functional(out,backend,cfg,Ledger,Meter,core,protocol)
        if not gate['passed']:return 4
        policy=io.read(out/'experiment.json')
        for index,uid in enumerate(io.read(out/'cohort.json')):
            if (out/'runs'/ARM/uid/'result.json').exists():continue
            folder=out/'materialized'/uid
            if folder.exists():raise ValueError('Partially spent E query requires explicit recovery: '+uid)
            bundle=io.read(out/'normalization_sources'/f'{uid}.json')
            rows=source_observations(out,uid,bundle)
            ledger=Ledger(policy['extraction_cap'],answer_reserve=policy['answer_reserve'],calls_path=folder/'calls.jsonl')
            meter=Meter(backend,ledger,cfg,folder/'raw_calls');source_results={}
            began=time.process_time()
            try:
                for unit in bundle['units']:
                    io.write(out/'progress.json',dict(stage='joint_extraction',index=index,question_id=uid,
                        source_id=unit['id'],source_units=len(bundle['units']),sources_completed=len(source_results),updated_at=io.now()))
                    value=extract_source(unit,meter,core,protocol);source_results[unit['id']]=value
                    io.write(folder/'source_results'/f'{unit["id"]}.json',value)
                memory,audit=core.materialize(rows,source_results,policy['max_identity_gap'],policy['max_state_gap'])
                description,render_audit=core.render(memory)
                original=io.read(out/'final_inputs/A_original'/f'{uid}.json');prefix,_,suffix=io.parts(original)
                inputs=deepcopy(original);inputs['prompts'][0]=prefix+description+suffix
                if io.fixed(inputs)!=io.fixed(original):raise ValueError('E changed the original fixed QA contract')
                audit.update(render=render_audit,source_units=len(bundle['units']),
                    failed_source_units=sum(v['error'] is not None for v in source_results.values()),
                    role_reviews=sum(v['role_review'] for v in source_results.values()),
                    joint_model_calls=sum(c['operation']==core.OPERATION for c in ledger.calls),
                    preparation_cpu_seconds=time.process_time()-began)
                io.write(folder/'memory.json',memory);io.write(folder/'audit.json',audit)
                io.write(out/'final_inputs'/ARM/f'{uid}.json',inputs)
                io.write(folder/'sealed.json',{name:io.sha(folder/name) for name in ['memory.json','audit.json']})
            finally:io.write(folder/'cost_summary.json',ledger.summary());meter.close()
            code=protocol.qa_worker(out,index,index+1,'E_resident_qa')
            report(out)
            if code:return code
        return 0
    finally:protocol.warm_backend=original_warm;backend.close()


def result_stats(rows,ids,truth,planned):
    correct=sum(not rows[u].get('error') and str(rows[u]['selected_option'])==str(truth[u]) for u in ids)
    tokens=[rows[u]['input_tokens'] for u in ids if rows[u].get('input_tokens') is not None]
    return dict(completed=len(ids),planned=planned,correct=correct,errors=sum(bool(rows[u].get('error')) for u in ids),
        accuracy=correct/planned if len(ids)==planned else None,accuracy_among_completed=correct/len(ids) if ids else None,
        mean_input_tokens=statistics.mean(tokens) if tokens else None,
        mean_qa_seconds=statistics.mean(rows[u]['seconds'] for u in ids) if ids else None)


def report(out):
    ids=io.read(out/'cohort.json');truth=io.read(out/'evaluation_only.json');policy=io.read(out/'experiment.json')
    roots={a:Path(policy['reference_compact_run']) for a in ['A_original','B_compact_text','C_compact_text']}
    roots.update(D_state_segments=Path(policy['D_run']),E_joint_entities=out)
    rows={a:{u:io.read(p) for u in ids if (p:=root/'runs'/a/u/'result.json').exists()} for a,root in roots.items()}
    common=[u for u in ids if all(u in rs for rs in rows.values())]
    correct=lambda a,u:not rows[a][u].get('error') and str(rows[a][u]['selected_option'])==str(truth[u])
    comparisons={}
    for a in roots:
        if a==ARM:continue
        paired=[u for u in ids if u in rows[a] and u in rows[ARM]]
        gained=[u for u in paired if correct(ARM,u) and not correct(a,u)]
        lost=[u for u in paired if correct(a,u) and not correct(ARM,u)]
        comparisons['E_vs_'+a]=dict(paired=len(paired),corrected=gained,regressed=lost,
            delta_percentage_points=100*(len(gained)-len(lost))/len(paired) if paired else None)
    audits={u:io.read(p) for u in ids if (p:=out/'materialized'/u/'audit.json').exists()}
    strata={}
    for label,key in [('with_state_merging','merged_state_segments'),('with_alias_links','alias_links'),('with_raw_replacement','original_clauses_replaced')]:
        selected=[u for u in common if audits.get(u,{}).get(key,0)>0]
        strata[label]={a:result_stats(rs,selected,truth,len(ids)) for a,rs in rows.items()}
    costs={stage:sum(float(json.loads(line).get('actual_cost') or 0)
        for path in (out/stage).rglob('calls.jsonl') for line in path.read_text().splitlines())
        for stage in ['setup','functional','materialized','runs']}
    normalized_parent=Path(policy['normalization_parent'])
    reference_calls=[];reference_cost=[];paired_E_calls=0;paired_E_cost=0
    for uid in rows[ARM]:
        path=normalized_parent/'materialized'/uid/'calls.jsonl'
        if path.exists() and (path.parent/'sealed.json').exists():
            calls=[json.loads(line) for line in path.read_text().splitlines()]
            reference_calls.append(sum(c['operation']=='semantic_memory_extract' for c in calls))
            reference_cost.append(sum(float(c.get('actual_cost') or 0) for c in calls))
            paired_E_calls+=audits[uid]['joint_model_calls']
            own=out/'materialized'/uid/'calls.jsonl'
            paired_E_cost+=sum(float(json.loads(line).get('actual_cost') or 0) for line in own.read_text().splitlines())
    keys=['source_rows','entities','alias_links','ambiguous_mentions','merged_state_segments','original_clauses_retained',
          'original_clauses_replaced','joint_model_calls','failed_source_units','role_reviews']
    development_cost=sum(float(json.loads(line).get('actual_cost') or 0)
        for attempt in policy.get('prior_development_attempts',[]) for path in Path(attempt).rglob('calls.jsonl')
        for line in path.read_text().splitlines())
    interrupted_estimate=sum(io.read(path).get('interrupted_unlogged_wall_estimate_seconds',0)
        for attempt in policy.get('prior_development_attempts',[])
        if (path:=Path(attempt)/'control/retirement.json').exists())
    summary=dict(updated_at=io.now(),planned=len(ids),complete=len(rows[ARM])==len(ids),
        arms={ARM:result_stats(rows[ARM],list(rows[ARM]),truth,len(ids))},common_completed=len(common),
        paired_common={a:result_stats(rs,common,truth,len(ids)) for a,rs in rows.items()},comparisons=comparisons,
        prepared_inputs=len(audits),materialization_totals={k:sum(v[k] for v in audits.values()) for k in keys},
        paired_source_only_subgroups=strata,measured_seconds_by_stage=costs,
        prior_development_seconds=development_cost,measured_seconds_including_development=sum(costs.values())+development_cost,
        prior_interrupted_unlogged_wall_estimate_seconds=interrupted_estimate,
        reference_extraction_completed_E=dict(questions=len(reference_calls),model_calls=sum(reference_calls),seconds=sum(reference_cost),
            E_model_calls=paired_E_calls,E_seconds=paired_E_cost),
        bundled_changes=True,subgroups_are_descriptive=True,development_cohort=True)
    io.write(out/'report.json',summary);return summary


def queue(out):
    verify(out);policy=io.read(out/'experiment.json')['resource_policy']
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('E worker already attempted; inspect retained state')
        while True:
            text=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.free','--format=csv,noheader,nounits'],text=True)
            rows={parts[0]:(parts[1],int(parts[2])) for line in text.splitlines() if (parts:=[s.strip() for s in line.split(',')])}
            uuid,free=rows[policy['gpu_index']]
            if free>=policy['minimum_free_mib']:break
            io.write(out/'queue_status.json',dict(state='waiting_for_initial_gpu_memory',controller_pid=os.getpid(),
                gpu_index=policy['gpu_index'],free_mib=free,minimum_free_mib=policy['minimum_free_mib'],updated_at=io.now()))
            time.sleep(10)
        with (Path('/tmp')/f'videoagent-E-{os.getuid()}-{uuid}.lock').open('a') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            env=dict(os.environ,CUDA_VISIBLE_DEVICES=uuid,CUDA_DEVICE_ORDER='PCI_BUS_ID',
                HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
            command=[sys.executable,'-u',str(Path(__file__).resolve()),'worker','--out',str(out)]
            (out/'logs').mkdir(exist_ok=True)
            with (out/'logs/worker.log').open('a') as log:
                proc=subprocess.Popen(command,cwd=out,env=env,stdin=subprocess.DEVNULL,
                    stdout=log,stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
            launch=dict(controller_pid=os.getpid(),worker_pid=proc.pid,gpu_index=policy['gpu_index'],gpu_uuid=uuid,command=command,started_at=io.now())
            io.write(out/'launch.json',launch)
            while proc.poll() is None:
                report(out)
                io.write(out/'queue_status.json',dict(launch,state='running',updated_at=io.now()))
                time.sleep(5)
            summary=report(out)
            state='complete' if not proc.returncode and summary['complete'] else 'failed'
            io.write(out/'queue_status.json',dict(launch,state=state,returncode=proc.returncode,updated_at=io.now()))
            return proc.returncode or (0 if summary['complete'] else 2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--reference',type=Path)
    parser.add_argument('--D',type=Path)
    parser.add_argument('--prior-attempt',type=Path)
    args=parser.parse_args();out=args.out.resolve()
    try:
        if args.mode=='prepare':prepare(args.reference.resolve(),args.D.resolve(),out,args.prior_attempt.resolve() if args.prior_attempt else None)
        elif args.mode=='worker':return work(out)
        elif args.mode=='queue':return queue(out)
        else:print(json.dumps(report(out),ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        import traceback
        if out.exists():io.write(out/'failures'/f'{args.mode}.json',dict(type=type(exc).__name__,
            message=str(exc),traceback=traceback.format_exc(),updated_at=io.now()))
        raise


if __name__=='__main__':raise SystemExit(main())
