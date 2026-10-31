"""H: F-style on-demand evidence with G open states and unknown-actor rules."""
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

ARM='H_generalized_lazy'


def verify(out):
    for name,expected in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=expected:raise ValueError('Frozen H artifact changed: '+name)


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Unsupported frozen F runtime marker: '+old)
    return text.replace(old,new,1)


def patch_runtime(runtime,root):
    """Install H into a copy of frozen F without changing F or QA dispatch."""
    dest=runtime/'script_core'
    for name in ['generalized_entity_memory.py','generalized_lazy_memory.py']:
        shutil.copy2(root/'script_core'/name,dest/name)
    path=dest/'schemas.py'
    marker='def output_schema(operation, inputs):\n'
    path.write_text(replace_once(path.read_text(),marker,marker+
        "    if operation in ('h_plan_fields', 'h_extract_fields'):\n"
        "        from .generalized_lazy_memory import output_schema as h_schema\n"
        "        return h_schema(operation, inputs)\n"))
    with (dest/'prompts.py').open('a') as handle:
        handle.write("\nfrom .generalized_lazy_memory import PLAN_PROMPT as H_PLAN_PROMPT, EXTRACT_PROMPT as H_EXTRACT_PROMPT\n"
            "CONTRACTS['h_plan_fields']=H_PLAN_PROMPT\nCONTRACTS['h_extract_fields']=H_EXTRACT_PROMPT\n")
    path=dest/'backend.py';text=path.read_text();before=ast.parse(text)
    text=replace_once(text,"if operation in ('f_plan_fields',", "if operation in ('h_plan_fields', 'h_extract_fields', 'f_plan_fields',")
    text=replace_once(text,"if original_op in ('f_plan_fields', 'f_extract_fields'):",
        "if original_op in ('h_plan_fields', 'h_extract_fields', 'f_plan_fields', 'f_extract_fields'):")
    def dispatch(tree):
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='LocalModels')
        return ast.dump(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='call'),include_attributes=False)
    if dispatch(before)!=dispatch(ast.parse(text)):
        raise ValueError('H changed the original QA dispatch')
    path.write_text(text)


def prepare(reference,out,G=None,prior=None,gpu_index='3',batch_size=2):
    if out.exists():raise ValueError('Use a fresh H directory')
    if batch_size not in (1,2,4):raise ValueError('Unsupported H batch size')
    root=Path(__file__).resolve().parents[1]
    for name,expected in io.read(reference/'frozen_hashes.json').items():
        if io.sha(reference/name)!=expected:raise ValueError('Frozen F artifact changed: '+name)
    parent_policy=io.read(reference/'experiment.json')
    if parent_policy['arm']!='F_lazy_script':raise ValueError('H must derive from a frozen F experiment')
    if not io.read(reference/'validation/functional.json')['passed']:
        raise ValueError('F functional gate did not pass')
    if G is not None:
        if io.read(G/'experiment.json')['arm']!='G_generalized_entities':
            raise ValueError('G comparison must refer to a G experiment')
        if io.read(reference/'cohort.json')!=io.read(G/'cohort.json'):
            raise ValueError('G and F must use the same ordered cohort')
    out.mkdir(parents=True)
    for name in ['cohort.json','evaluation_only.json','historical_only.json','qa_config.json','qa_expected_artifacts.json']:
        shutil.copy2(reference/name,out/name)
    for name in ['packets','final_inputs/A_original','normalization_sources','source_observations']:
        shutil.copytree(reference/name,out/name)
    shutil.copytree(reference/'runtime/qa',out/'runtime/qa',symlinks=True,ignore=shutil.ignore_patterns('__pycache__'))
    patch_runtime(out/'runtime/qa',root)
    control=out/'control';control.mkdir()
    for source,name in [(Path(__file__),Path(__file__).name),
            (reference/'control/compact_protocol.py','compact_protocol.py'),
            (reference/'control/controlled_memory_io.py','controlled_memory_io.py'),
            (reference/'control/qa_evidence_view.py','qa_evidence_view.py'),
            (reference/'control/run_joint_entity_experiment.py','run_joint_entity_experiment.py'),
            (reference/'control/run_compact_resident.py','run_compact_resident.py'),
            (root/'script/generalized_lazy_execution.py','generalized_lazy_execution.py'),
            (root/'script_core/batch_extraction.py','batch_extraction.py')]:shutil.copy2(source,control/name)
    ids=io.read(out/'cohort.json')
    videos=[io.read(out/'packets'/f'{uid}.json')['packet']['question']['video_id'] for uid in ids]
    priors=list(dict.fromkeys(io.read(prior/'experiment.json').get('prior_development_attempts',[])+[str(prior)])) if prior else []
    io.write(out/'experiment.json',dict(created_at=io.now(),planned=len(ids),arm=ARM,
        parent_F_run=str(reference),G_run=str(G) if G else None,
        reference_compact_run=parent_policy['reference_compact_run'],
        D_run=parent_policy['D_run'],E_run=parent_policy['E_run'],
        normalization_parent=parent_policy['normalization_parent'],
        prior_development_attempts=priors,
        database='database/h_script.sqlite3',extraction_cap=1500,answer_reserve=45,extraction_batch_size=batch_size,
        resource_policy=dict(gpu_index=str(gpu_index),minimum_free_mib=32768,stable_observations=3,resident_until_complete=True),
        changes=['F-style task planning, incremental fields, source-preserving projection and persistent SQLite storage.',
            'One states category replaces all fixed states.dimension fields; cache all explicit source conditions as entity/description.',
            'G source-bound state and optional unknown-actor validation; keep eight property categories and existing role/link categories.',
            'Use an independent H database/version, with fresh H extraction and final answers; F/G answers are comparison-only.'],
        projection='Per-sample compact fields; no E/G entity timeline or D interval merging.',
        fixed='Same ordered tasks, upstream selected sources, accepted visuals, QA model/precision/generation/prompt/parser. No new retrieval or images.',
        extraction='Source-only inputs and requested fields; planner alone sees question/options. F 1024-token extraction cap retained; no added role-review calls.',
        raw_policy='Every selected source remains visible as whole-clause-validated facts or literal original text.',
        scope='Current task source scope only, even when the cache holds evidence from another task.',
        state_representation='entity_description',fixed_state_dimensions=False,
        unique_videos=len(set(videos)),benchmark_scope='Internal development cohort; repeated reads are separate mechanism checks.',
        failures='No automatic retries, answer reuse, hidden truncation or infrastructure fallback.',
        checked_absent='This source has no explicit assertion for the requested field, not physical absence.'))
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file() and not p.is_symlink()})
    verify(out);report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    print(json.dumps(dict(out=str(out),planned=len(ids),state='prepared')))


def extract_source(unit,fields,meter,core,protocol):
    text=core.joint.clean_unit(unit)['text'];inputs=dict(unit=dict(id=unit['id'],text=text),requested_fields=fields,source_type='caption')
    empty=dict(facts=[],mentions=[],rejected=[],uncertain=False)
    if core.joint.NEGATION.search(text) or core.joint.UNCERTAINTY.search(text):
        return dict(accepted=dict(empty,uncertain=True),error=None,model_called=False,guarded_original=True)
    schema=core.output_schema(core.EXTRACT,inputs)
    if all(schema['properties'][k]['maxItems']==0 for k in core.joint.COLLECTIONS.values()):
        return dict(accepted=empty,error=None,model_called=False,guarded_original=True)
    try:
        data,_=meter.json(core.EXTRACT,inputs,tuple(core.joint.COLLECTIONS.values())+('uncertain',))
        core.validate_output(data,schema)
        accepted=core.joint.accept(unit,data)
        return dict(accepted=accepted,error=None,model_called=True)
    except Exception as exc:
        budget_limited=type(exc).__name__=='ResourceExhausted' or getattr(exc,'budget_limited',False)
        if protocol.is_infrastructure(exc) and not budget_limited:raise
        return dict(accepted=dict(empty,uncertain=True),error=dict(type=type(exc).__name__,message=str(exc)),model_called=True)


def functional(out,backend,cfg,Ledger,Meter,core,protocol,renderer):
    folder=out/'functional';ledger=Ledger(600,answer_reserve=0,calls_path=folder/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,folder/'raw_calls');failures=[];checks=[]
    unit=dict(id='U0',text='#C C moves the red book with her left hand.',source_caption_ids=['C0'],source_intervals=[[9,10]])
    row=dict(caption_id='C0',frame_index=9,source_id='U0',sample_interval=[9,10],original_text=unit['text'])
    try:
        with core.ScriptDB(folder/'script.sqlite3',extractor_version=core.VERSION) as store:
            callback=lambda u,fields:extract_source(u,fields,meter,core,protocol)
            first,base=store.ensure('functional',[unit],[row],['actions.base'],callback)
            extra,added=store.ensure('functional',[unit],[row],['actions.base','actions.body_part'],callback)
            def no_call(*args):raise AssertionError('Repeated field read performed extraction')
            again,warm=store.ensure('functional',[unit],[row],['actions.base','actions.body_part'],no_call)
            if warm.get('missing_fields') or core.digest(extra)!=core.digest(again):failures.append('Repeated read did not reuse identical fields')
            if not any(f.get('body_part')=='left hand' for f in extra['U0']['accepted']['facts']):failures.append('Explicit body_part lost')
            if len(extra['U0']['accepted']['facts'])!=1 or extra['U0']['accepted']['facts'][0].get('patient')!='red book':failures.append('Incremental role did not join its unique existing action core')
            if any('body_part' in f for f in first['U0']['accepted']['facts']):failures.append('Unrequested role extracted on first task')
            body,view,audit=store.view('functional',dict(question='Which hand?'),[row],extra,renderer)
            if not body.startswith('Frame [9,10): Action:') or 'body_part="left hand"' not in body:failures.append('C-style renderer did not preserve requested fields')
            checks.append(dict(case='base_then_new_task_then_repeat',first=base,incremental=added,repeated=warm,description=body))
            examples=[('#C The book is wet.',['states'],'state',dict(description='wet')),
                ('#C The book is on the table.',['relations.spatial'],'relation',dict(predicate='on',right='table')),
                ('#C C wipes the red book with a cloth.',['actions.instrument'],'action',dict(instrument='cloth')),
                ('#C C never wipes the book.',['actions.base'],None,{}),
                ('#C The server is unreachable.',['states'],'state',dict(entity='server',description='unreachable')),
                ('#C The device is ready for pairing.',['states'],'state',dict(entity='device',description='ready for pairing')),
                ('#C The liquid is viscous.',['states'],'state',dict(entity='liquid',description='viscous')),
                ('#C The application is awaiting review.',['states'],'state',dict(entity='application',description='awaiting review')),
                ('#C The box was moved with a crane.',['actions.base','actions.instrument'],'action',dict(verb='moved',instrument='crane'))]
            for i,(text,fields,kind,expected) in enumerate(examples):
                u=dict(unit,id=f'X{i}',text=text,source_caption_ids=[f'X{i}'])
                r=dict(row,source_id=u['id'],caption_id=f'X{i}',original_text=text)
                result,stats=store.ensure(f'case{i}',[u],[r],fields,callback);accepted=result[u['id']]['accepted']
                def matches(fact):
                    if fact['kind']!=kind:return False
                    for key,value in expected.items():
                        observed=fact.get(key,'')
                        if kind=='state' and key=='description':
                            if value not in observed:return False
                        elif re.sub(r'^(?:a|an|the|his|her)\s+','',observed,flags=re.I)!=value:
                            return False
                    return True
                if kind and not any(matches(f) for f in accepted['facts']):failures.append(dict(case=i,missing=expected,result=result))
                if not kind and (accepted['facts'] or not accepted['uncertain']):failures.append('Negative statement not guarded')
                if any(f['kind']=='state' and ('dimension' in f or 'value' in f) for f in accepted['facts']):failures.append('Legacy state leaked into H')
                if 'was moved' in text and any('actor' in f for f in accepted['facts'] if f['kind']=='action'):failures.append('Unknown actor invented')
                body,view,audit=store.view(f'case{i}',{},[r],result,renderer)
                checks.append(dict(case=i,result=result,stats=stats,description=body,audit=audit))
            task=dict(question='Which hand does C use to move the book?',options=['left hand','right hand'])
            plan,hit=store.plan('plan_case',task,[unit],meter)
            if 'actions.body_part' not in plan['fields'] or plan['source_ids']!=['U0']:failures.append(dict(planner_missing_required_field=plan))
            checks.append(dict(case='task_plan',plan=plan,cache_hit=hit))
            open_task=dict(question='What condition is the server in?',options=['unreachable','ready'])
            plan,hit=store.plan('open_state_plan',open_task,[unit],meter)
            if 'states' not in plan['fields'] or any(f.startswith('states.') for f in plan['fields']):failures.append('Planner did not use open states category')
            checks.append(dict(case='open_state_plan',plan=plan,cache_hit=hit))
            # Source-only failure probes: an absent requested change cannot
            # remove the observations that support an inferred goal or method.
            for i,text in enumerate(['#C C cuts paper with scissors.','#C C welds metal with a welding machine.']):
                u=dict(unit,id=f'G{i}',text=text,source_caption_ids=[f'G{i}'])
                r=dict(row,source_id=u['id'],caption_id=f'G{i}',original_text=text)
                result,stats=store.ensure(f'guard{i}',[u],[r],['changes'],callback)
                body,view,audit=store.view(f'guard{i}',{},[r],result,renderer)
                if text not in body or audit['visible_source_rows']!=1:
                    failures.append(dict(case='absent_change_lost_observation',source=text))
                checks.append(dict(case='absent_change_preserves_source',stats=stats,description=body,audit=audit))
            for i,task in enumerate([
                    dict(question='What is the overall goal of these repeated adjustments?',options=['prepare an object','clean an object']),
                    dict(question='Which tools does C use to cut paper and wipe a table?',options=['scissors and cloth','knife and sponge'])]):
                plan,hit=store.plan(f'plan_support{i}',task,[unit],meter)
                if 'actions.base' not in plan['fields']:failures.append('Planner dropped supporting actions')
                if i==1 and 'actions.instrument' not in plan['fields']:failures.append('Tool planner omitted instrument')
                checks.append(dict(case='supporting_task_plan',plan=plan,cache_hit=hit))
        original=io.read(out/'final_inputs/A_original'/f'{io.read(out/"cohort.json")[0]}.json')
        checked,_=meter.call('temporal_qa_check',original);answer,_=meter.call('videotree_qa',original)
        expected=io.read(out/'historical_only.json')[io.read(out/'cohort.json')[0]]['selected_option']
        if str(answer['data']['selected_option'])!=str(expected):failures.append('Original QA smoke prediction differs')
        gate=dict(passed=not failures,failures=failures,checks=checks,original_QA_smoke=dict(selected_option=answer['data']['selected_option'],expected=expected),updated_at=io.now())
        io.write(out/'validation/functional.json',gate);return gate
    finally:io.write(folder/'cost_summary.json',ledger.summary());meter.close()


def work(out):
    verify(out);sys.path.insert(0,str(out/'control'))
    import compact_protocol as protocol
    from run_compact_resident import ResidentBackend
    from run_joint_entity_experiment import source_observations
    from qa_evidence_view import compact_description
    from generalized_lazy_execution import (install_worker, restore_ledger, SourceExtractor,
        ensure_batched, extraction_counts, EXECUTION_VERSION)
    sys.path.insert(0,str(out/'runtime/qa'))
    install_worker()
    cfg,backend,Ledger,Meter=protocol.warm_backend(out,'qa','H_batch_resident')
    Ledger=restore_ledger(Ledger)
    core=importlib.import_module('script_core.generalized_lazy_memory')
    original_warm=protocol.warm_backend
    protocol.warm_backend=lambda *args:(cfg,ResidentBackend(backend),Ledger,Meter)
    protocol.io.ARMS=(ARM,);protocol.io.ORDERS=((ARM,),)*6
    try:
        gate_path=out/'validation/functional.json'
        gate=io.read(gate_path) if gate_path.exists() else functional(out,backend,cfg,Ledger,Meter,core,protocol,compact_description)
        if not gate['passed']:return 4
        policy=io.read(out/'experiment.json')
        version=core.VERSION+':'+io.digest(dict(models=cfg['models'],extractor={name:io.sha(out/'runtime/qa/script_core'/name) for name in ['generalized_lazy_memory.py','generalized_entity_memory.py','lazy_script_memory.py','joint_entity_memory.py','semantic_memory.py','schemas.py','prompts.py','backend.py']}))
        with core.ScriptDB(out/policy['database'],version) as store:
            for index,uid in enumerate(io.read(out/'cohort.json')):
                if (out/'runs'/ARM/uid/'result.json').exists():continue
                folder=out/'materialized'/uid
                if folder.exists():
                    continuation=out/'control/execution_optimization/checkpoint.json'
                    checkpoint=io.read(continuation) if continuation.exists() else {}
                    if uid!=checkpoint.get('partial_question_id'):
                        raise ValueError('Partially spent H query requires explicit recovery: '+uid)
                    if (folder/'sealed.json').exists():
                        code=protocol.qa_worker(out,index,index+1,'H_resident_qa');report(out)
                        if code:return code
                        continue
                bundle=io.read(out/'normalization_sources'/f'{uid}.json');allrows=source_observations(out,uid,bundle)
                task=io.read(out/'packets'/f'{uid}.json')['packet']['question'];video=task['video_id']
                ledger=Ledger(policy['extraction_cap'],answer_reserve=policy['answer_reserve'],calls_path=folder/'calls.jsonl')
                meter=Meter(backend,ledger,cfg,folder/'raw_calls');began=time.monotonic()
                try:
                    io.write(out/'progress.json',dict(stage='task_plan',index=index,question_id=uid,updated_at=io.now()))
                    plan,plan_hit=store.plan(video,task,bundle['units'],meter)
                    units=[u for u in bundle['units'] if u['id'] in set(plan['source_ids'])]
                    rows=[r for r in allrows if r['source_id'] in set(plan['source_ids'])]
                    io.write(folder/'plan.json',dict(plan,plan_cache_hit=plan_hit,authorized_source_units=len(bundle['units']),selected_source_units=len(units)))
                    def progress(i,unit,missing):
                        io.write(out/'progress.json',dict(stage='missing_field_extraction',index=index,question_id=uid,
                            source_id=unit['id'],selected_source_units=len(units),sources_completed=i,missing_fields=missing,updated_at=io.now()))
                    extractor=SourceExtractor(meter,core,protocol,extract_source,policy.get('extraction_batch_size',2))
                    results,stats=ensure_batched(store,video,units,rows,plan['fields'],extractor,progress)
                    description,memory,audit=store.view(video,task,rows,results,compact_description)
                    # This verifies field reuse, not answer reuse. No second QA call.
                    def no_call(*args):raise AssertionError('Warm field read extracted again')
                    repeat,warm=store.ensure(video,units,rows,plan['fields'],no_call)
                    if core.digest(repeat)!=core.digest(results) or warm.get('missing_fields'):raise ValueError('Warm-read verification failed')
                    original=io.read(out/'final_inputs/A_original'/f'{uid}.json');prefix,_,suffix=io.parts(original)
                    inputs=deepcopy(original);inputs['prompts'][0]=prefix+description+suffix
                    if io.fixed(inputs)!=io.fixed(original):raise ValueError('H changed original fixed QA contract')
                    audit.update(cache=stats,warm_read=warm,warm_read_identical=True,fields=plan['fields'],
                        authorized_source_units=len(bundle['units']),selected_source_units=len(units),
                        **extraction_counts(ledger.calls,core.EXTRACT),
                        execution_version=EXECUTION_VERSION,execution_batch_size=extractor.batch_size,
                        planning_model_calls=sum(c['operation']==core.PLAN for c in ledger.calls),preparation_seconds=time.monotonic()-began)
                    io.write(folder/'memory.json',memory);io.write(folder/'audit.json',audit)
                    io.write(out/'final_inputs'/ARM/f'{uid}.json',inputs)
                    io.write(folder/'sealed.json',{name:io.sha(folder/name) for name in ['memory.json','audit.json','plan.json']})
                finally:io.write(folder/'cost_summary.json',ledger.summary());meter.close()
                code=protocol.qa_worker(out,index,index+1,'H_resident_qa');report(out)
                if code:return code
        return 0
    finally:protocol.warm_backend=original_warm;backend.close()


def report(out):
    from run_joint_entity_experiment import result_stats
    ids=io.read(out/'cohort.json');truth=io.read(out/'evaluation_only.json');policy=io.read(out/'experiment.json')
    roots={a:Path(policy['reference_compact_run']) for a in ['A_original','B_compact_text','C_compact_text']}
    roots.update(D_state_segments=Path(policy['D_run']),E_joint_entities=Path(policy['E_run']),
        F_lazy_script=Path(policy['parent_F_run']),H_generalized_lazy=out)
    if policy.get('G_run'):roots['G_generalized_entities']=Path(policy['G_run'])
    rows={a:{u:io.read(p) for u in ids if (p:=root/'runs'/a/u/'result.json').exists()} for a,root in roots.items()}
    comparisons={}
    correct=lambda a,u:not rows[a][u].get('error') and str(rows[a][u]['selected_option'])==str(truth[u])
    for arm in roots:
        if arm==ARM:continue
        pairs=[u for u in ids if u in rows[arm] and u in rows[ARM]]
        comparisons['H_vs_'+arm]=dict(paired=len(pairs),corrected=[u for u in pairs if correct(ARM,u) and not correct(arm,u)],
            regressed=[u for u in pairs if correct(arm,u) and not correct(ARM,u)],
            delta_percentage_points=100*(sum(correct(ARM,u) for u in pairs)-sum(correct(arm,u) for u in pairs))/len(pairs) if pairs else None)
    audits=[io.read(p) for u in ids if (p:=out/'materialized'/u/'audit.json').exists()]
    costs={stage:sum(float(json.loads(line).get('actual_cost') or 0) for path in (out/stage).rglob('calls.jsonl')
        for line in path.read_text().splitlines()) for stage in ['setup','functional','materialized','runs']}
    development_cost=sum(float(json.loads(line).get('actual_cost') or 0) for prior in policy.get('prior_development_attempts',[]) for path in Path(prior).rglob('calls.jsonl') for line in path.read_text().splitlines())
    interrupted=sum(io.read(p).get('interrupted_unlogged_wall_estimate_seconds',0) for prior in policy.get('prior_development_attempts',[]) if (p:=Path(prior)/'control/retirement.json').exists())
    keys=['field_extraction_model_calls','field_extraction_source_requests','extraction_batches','planning_model_calls','authorized_source_units','selected_source_units','visible_source_rows','open_state_facts','actorless_action_facts']
    coverage=dict(empty_descriptions=sum(v['visible_source_rows']==0 for v in audits),
        selected_source_rows=sum(v.get('selected_source_rows',v.get('source_rows',0)) for v in audits),
        visible_source_rows=sum(v['visible_source_rows'] for v in audits),
        retained_original_rows=sum(v.get('retained_original_rows',0) for v in audits))
    summary=dict(updated_at=io.now(),planned=len(ids),complete=len(rows[ARM])==len(ids),unique_videos=policy['unique_videos'],
        arms={ARM:result_stats(rows[ARM],list(rows[ARM]),truth,len(ids))},comparisons=comparisons,
        primary_comparison='H_vs_F_lazy_script',state_representation='entity_description',fixed_state_dimensions=False,
        paired={a:{arm:result_stats(rows[arm],[u for u in ids if u in rows[a] and u in rows[ARM]],truth,len(ids)) for arm in [a,ARM]} for a in roots if a!=ARM},
        prepared_inputs=len(audits),totals={k:sum(v.get(k,v.get('field_extraction_model_calls',0) if k=='field_extraction_source_requests' else 0) for v in audits) for k in keys},
        execution_versions=dict(__import__('collections').Counter(v.get('execution_version','serial') for v in audits)),
        cache_totals={k:sum(v['cache'].get(k,0) for v in audits) for k in ['cache_hits','missing_fields','status_value','status_checked_absent','status_uncertain','status_failed']},
        source_coverage=coverage,
        warm_read_checks_passed=sum(v['warm_read_identical'] for v in audits),measured_seconds_by_stage=costs,
        prior_development_seconds=development_cost,measured_seconds_including_development=sum(costs.values())+development_cost,prior_interrupted_unlogged_wall_estimate_seconds=interrupted,
        database_bytes=sum(p.stat().st_size for p in (out/'database').glob('h_script.sqlite3*')),
        scope='H task-relevant open-state projection. Repeated reads are mechanism checks, not extra answers.',
        timing_policy='Shared GPU observations; not a controlled speed comparison.',development_cohort=True)
    io.write(out/'report.json',summary);return summary


def queue(out):
    verify(out);policy=io.read(out/'experiment.json')['resource_policy']
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('H worker already attempted; inspect retained state')
        stable=0
        while True:
            text=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.free','--format=csv,noheader,nounits'],text=True)
            rows={parts[0]:(parts[1],int(parts[2])) for line in text.splitlines() if (parts:=[s.strip() for s in line.split(',')])}
            uuid,free=rows[policy['gpu_index']]
            stable=stable+1 if free>=policy['minimum_free_mib'] else 0
            if stable>=policy.get('stable_observations',3):break
            io.write(out/'queue_status.json',dict(state='waiting_for_initial_gpu_memory',free_mib=free,updated_at=io.now()));time.sleep(10)
        with (Path('/tmp')/f'videoagent-H-{os.getuid()}-{uuid}.lock').open('a') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            env=dict(os.environ,CUDA_VISIBLE_DEVICES=uuid,CUDA_DEVICE_ORDER='PCI_BUS_ID',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
                TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
            command=[sys.executable,'-u',str(Path(__file__).resolve()),'worker','--out',str(out)]
            (out/'logs').mkdir(exist_ok=True)
            with (out/'logs/worker.log').open('a') as log:
                proc=subprocess.Popen(command,cwd=out,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
            launch=dict(controller_pid=os.getpid(),worker_pid=proc.pid,gpu_index=policy['gpu_index'],gpu_uuid=uuid,command=command,started_at=io.now())
            io.write(out/'launch.json',launch)
            while proc.poll() is None:
                report(out);io.write(out/'queue_status.json',dict(launch,state='running',updated_at=io.now()));time.sleep(5)
            summary=report(out);state='complete' if not proc.returncode and summary['complete'] else 'failed'
            io.write(out/'queue_status.json',dict(launch,state=state,returncode=proc.returncode,updated_at=io.now()));return proc.returncode or (0 if summary['complete'] else 2)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    for flag in ['out','reference','G']:parser.add_argument('--'+flag,type=Path,required=flag=='out')
    parser.add_argument('--prior-attempt',type=Path)
    parser.add_argument('--gpu-index',default='3')
    parser.add_argument('--batch-size',type=int,choices=[1,2,4],default=2)
    args=parser.parse_args();out=args.out.resolve()
    try:
        if args.mode=='prepare':
            if args.reference is None:parser.error('prepare requires --reference pointing to frozen F')
            prepare(args.reference.resolve(),out,args.G.resolve() if args.G else None,
                args.prior_attempt.resolve() if args.prior_attempt else None,args.gpu_index,args.batch_size)
        elif args.mode=='queue':return queue(out)
        elif args.mode=='worker':return work(out)
        else:print(json.dumps(report(out),ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        import traceback
        if out.exists():io.write(out/'failures'/f'{args.mode}.json',dict(type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc(),updated_at=io.now()))
        raise


if __name__=='__main__':raise SystemExit(main())
