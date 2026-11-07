"""Paired fixed-source O/O+ diagnostic derived from completed N navigation."""
import argparse
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script_core import o_sentence_filter as core

CASES=['98427b0b-20ed-45bb-b772-91573ce90b5a','6dd07731-937c-4b63-b296-0a947ffe6996']


def verify(out):
    for name,sha in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=sha:raise ValueError('Frozen O artifact changed: '+name)


def extraction_filter_costs(result):
    """Resolve original costs across explicit replay chains without charging twice."""
    inherited=result.get('inherited_extraction_filter_costs',{})
    own=result['cost_summary']['by_operation']
    return {k:own.get(k,inherited.get(k,0.)) for k in (core.EXTRACT,core.FILTER,core.FILTER_PLAN)}


def execution_failures(out,rows):
    runtime=io.read(out/'failure.json') if (out/'failure.json').exists() else None
    gate=io.read(out/'validation/functional.json') if (out/'validation/functional.json').exists() else None
    count=sum(bool(r.get('error')) for r in rows)
    return dict(errors=count or int(bool(runtime or (gate and gate.get('error')))),runtime_failure=runtime)


def diagnostic_cohort(parent,count):
    if count not in (2,10):raise ValueError('Supported fixed-source diagnostic sizes: 2 or 10')
    if count==2:return list(CASES),[]
    extra=[u for u in io.read(parent/'cohort.json') if u not in CASES][:8]
    if len(extra)!=8:raise ValueError('Need eight additional cases in original cohort order')
    return extra+list(CASES),extra


def prepare(out,*,functional_only=False,selection_replay_from=None,composition_context_eval=False,semantic_diagnostic=False,case_count=2):
    from script.run_m_scene_experiment import verify as verify_n
    from transformers import AutoTokenizer
    parent=(ROOT/'workdirs/scene-planned-N-L-300-20261103-r2').resolve()
    case_ids,additional_ids=diagnostic_cohort(parent,case_count)
    if case_count!=2 and not composition_context_eval:raise ValueError('Expanded cohort requires four-arm matrix')
    if selection_replay_from:
        selection_replay_from=selection_replay_from.resolve();verify(selection_replay_from)
        if not io.read(selection_replay_from/'report.json')['complete']:raise ValueError('Replay requires completed source run')
    if composition_context_eval and (not selection_replay_from or functional_only):
        raise ValueError('Composition context comparison requires completed selection replay')
    if semantic_diagnostic and not composition_context_eval:
        raise ValueError('Nonblocking semantic probes are limited to the fixed-source matrix diagnostic')
    verify_n(parent)
    if out.exists():raise ValueError('Use a fresh diagnostic directory')
    out.mkdir(parents=True)
    cfg=io.read(parent/'qa_config.json')
    assert cfg['query']['json_repair_retries']==0 and cfg['query']['tool_transient_retries']==0
    io.write(out/'qa_config.json',cfg)
    shutil.copy2(parent/'qa_expected_artifacts.json',out/'qa_expected_artifacts.json')
    labels=io.read(parent/'evaluation_only.json');io.write(out/'evaluation_only.json',{u:labels[u] for u in case_ids})
    parent_hashes={}
    maximum=0;tokenizer=AutoTokenizer.from_pretrained(cfg['models']['vlm'],local_files_only=True)
    for uid in case_ids:
        replay_case=bool(selection_replay_from and uid in CASES)
        src=parent/'runs'/uid;target=out/'cases'/uid
        result=io.read(src/'result.json')
        if result.get('error') or not result.get('answer'):raise ValueError('Need completed N sources')
        files=['directory.json','query.json','result.json','partial_search','directory_calls',
               'partial_temporal/semantic_sources.json','partial_temporal/field_plan.json',
               'partial_temporal/N/final_input.json','partial_temporal/N/plan.json',
               'partial_temporal/N/proposed_plan.json','partial_temporal/N/source_preservation.json',
               'partial_temporal/N/pool.json','partial_temporal/calls.jsonl','partial_temporal/raw_calls']
        for name in files:
            old=src/name;new=target/'parent_N'/name;new.parent.mkdir(parents=True,exist_ok=True)
            if old.is_dir():shutil.copytree(old,new)
            else:shutil.copy2(old,new)
            for f in old.rglob('*') if old.is_dir() else [old]:
                if f.is_file():parent_hashes[str(f)]=io.sha(f)
        packet=io.read(parent/'inputs'/f'{uid}.json')
        io.write(target/'question.json',packet['question'])
        bundle=io.read(src/'partial_search/evidence_bundle.json')
        io.write(target/'original_qa.json',bundle['qa_input'])
        source=io.read(src/'partial_temporal/semantic_sources.json')
        sentences=core.sentence_sources(source['units'])
        io.write(target/'sentences.json',sentences)
        io.write(target/'extraction_policy.json',dict(input='source sentence only',
            question_field_plan_used=False,available_fields=list(core.fields_core.FIELDS)))
        # Prefix size is measured; candidate-dependent selection/rewrite and
        # final QA are checked against real tokenizer budgets at execution time.
        sizes=[]
        for sentence in sentences:
            request=core.extraction_input(sentence)
            messages=[dict(role='system',content=core.EXTRACT_PROMPT),dict(role='user',content=json.dumps(request,ensure_ascii=False))]
            n=len(tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=True))
            if n>cfg['models']['max_input_tokens']:raise ValueError('Single-sentence extraction input too long')
            sizes.append(n);maximum=max(maximum,n)
        io.write(target/'source_audit.json',dict(parent=str(src),new_navigation=False,
            selected_seconds=bundle['selected_seconds'],opened_scenes=bundle['opened_scenes'],
            unit_count=len(source['units']),sentence_count=len(sentences),
            structural_memory_archived=source['structural_memory'],
            structural_memory_policy='Archived for tracing; no unfiltered structural-memory suffix enters QA.',
            original_field_plan_reused=False,new_extraction=not replay_case,
            new_model_filtering=composition_context_eval or not replay_case,source_validation=False,
            max_extraction_input_tokens=max(sizes,default=0),historical_navigation_preserved=True))
        if replay_case:
            previous=selection_replay_from/'runs'/uid
            shutil.copytree(previous,target/'selection_replay')
            for f in previous.rglob('*'):
                if f.is_file():parent_hashes[str(f)]=io.sha(f)
            for name in ['sentences.json','question.json']:
                if io.read(target/name)!=io.read(selection_replay_from/'cases'/uid/name):raise ValueError('Replay sources differ')
    # Freeze the exact earlier filter inputs to isolate the selection change
    # from extraction changes. Expectations concern evidence, never QA labels.
    probes=[]
    for run,uid,sids in [
        ('r4',CASES[1],['S5','S59','S93']),
        ('r3',CASES[0],['S0','S7','S8'])]:
        for sid in sids:
            source_dir=ROOT/f'workdirs/O-Oplus-N-sentence-diagnostic-20261103-{run}'
            base=(source_dir/'functional/source_first_probes'/sid if run=='r4' else source_dir/'runs'/uid/'sentences'/sid)
            req=io.read(base/'filter_input.json');previous=io.read(base/'selection.json')
            required=[u['id'] for u in req['candidates'] if u.get('field') in ('actor','verb','patient','destination','instrument')]
            # S7 has a spurious destination among valid fields; this probe tests
            # preserving the valid ironing action despite unrelated bad fields.
            if run=='r3' and sid=='S7':required=[u['id'] for u in req['candidates'] if u.get('field') in ('actor','verb','patient','instrument')]
            probes.append(dict(id=run+'_'+sid,request=req,previous_labels=previous['labels'],required_keep=required,
                input_origin=str(base/'filter_input.json')))
            parent_hashes[str(base/'filter_input.json')]=io.sha(base/'filter_input.json')
    held=dict(question='Which repeated action characterizes the repair?',options=['Repeatedly tightens screws.','Repeatedly paints a wall.','Repeatedly cleans a window.'])
    units=[dict(id='U0',kind='action_field',group='actions_0',field='actor',value='D'),
        dict(id='U1',kind='action_field',group='actions_0',field='verb',value='tightens'),
        dict(id='U2',kind='action_field',group='actions_0',field='patient',value='a screw')]
    probes.append(dict(id='held_partial_repetition',request=core.filter_input(held,dict(text='D tightens a screw.'),units),required_keep=['U0','U1','U2']))
    probes.append(dict(id='held_unrelated_color',request=core.filter_input(held,dict(text='The window is blue.'),
        [dict(id='U0',kind='attributes',group='attributes_0',record=dict(entity='window',property='color',value='blue'))]),required_keep=[],all_zero=True))
    io.write(out/'filter_repair_probes.json',probes)
    for name in ['script_core','script']:
        shutil.copytree(ROOT/name,out/'runtime'/name,ignore=shutil.ignore_patterns('__pycache__'))
    for p in ROOT.glob('*.py'):shutil.copy2(p,out/'runtime'/p.name)
    io.write(out/'experiment.json',dict(created_at=io.now(),variant=['O','O+'],version=core.VERSION,
        plus_version=core.PLUS_VERSION,parent_N=str(parent),case_ids=case_ids,planned=0 if functional_only else len(case_ids),
        known_case_ids=list(CASES),additional_case_ids=additional_ids,
        runner_entry='script/run_o_sentence_experiment.py',
        functional_only=functional_only,
        composition_context_eval=composition_context_eval,
        semantic_diagnostic=semantic_diagnostic,
        composition_question_required=True,composition_options_default=False,
        scope=('2x2: O/O+ versus question-only/question-plus-options; same frozen extractions, new filtering/composition/answers.' if composition_context_eval else
            'Source-first extraction/filter probes only; no case QA evaluation.' if functional_only else
            'Two known fixed-source diagnostics with frozen extraction/filter replay and fresh composition/answers.' if selection_replay_from else
            'Two known fixed-source diagnostics, not full navigation evaluation or training.'),
        question_field_plan_used=False,extraction_input='source sentence only',
        selection_replay_from=str(selection_replay_from) if selection_replay_from else None,
        action_core_policy='A retained verb retains its already-extracted actor and patient. Original model labels and adjustments are separate.',
        case_choice=('Eight additional cases in original N cohort order, excluding the two known cases; then the two known controls. Selection does not inspect outcomes.' if additional_ids else 'Previously inspected ironing and packing cases; not a random accuracy sample.'),
        same_extraction_and_binary_selection=True,composition='Fresh model call per retained sentence; may return empty.',
        new_navigation=False,source_value_guards=False,post_extraction_source_validation=False,
        final_global_planner=False,forced_nonempty=False,forced_source_preservation=False,
        extraction_failure_policy='Malformed/unavailable/empty semantic extraction falls back once to original sentence; infrastructure failures stop.',
        maximum_measured_extraction_input_tokens=maximum,case_budget_seconds=3600,
        original_parent_hashes=parent_hashes,training=False,adapter=None,default_modes_unchanged=True,
        no_automatic_retries=True,
        resource_policy=dict(preference=['0','1','2','3'],minimum_free_mib=32768,maximum_utilization=100,
            stable_observations=3,selection='most_free_memory')))
    prior=[]
    for previous in sorted((ROOT/'workdirs').glob('O-Oplus-N-sentence-diagnostic-20261103-r*')):
        if previous.resolve()!=out and (previous/'validation/functional.json').exists():
            prior.append(dict(directory=str(previous.resolve()),functional=io.read(previous/'validation/functional.json'),
                setup=io.read(previous/'setup.json')))
    if prior:io.write(out/'prior_attempts.json',dict(attempts=prior,
        policy='Preserved previous attempts; costs excluded from current case results and reported separately.'))
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file()})
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    verify(out)


def decoded_call(meter,operation,request,*,audit_path=None):
    if operation==core.FILTER and 'evidence_task' not in request:
        request=deepcopy(request);request['evidence_task']=planned_filter_question(meter,request)['evidence_task']
    if audit_path:io.write(audit_path.with_name(audit_path.stem+'_input.json'),request)
    output,_=meter.call(operation,request)
    if output.get('parse_error') or output.get('data') is None:
        raise ValueError(f'{operation}: '+str(output.get('parse_error')))
    core.validate_output(output['data'],core.schema(operation,request))
    if audit_path:io.write(audit_path,output['data'])
    if operation==core.COMPOSE:
        groups=output['data']['groups']
        return dict(groups=groups,text=' '.join(groups[gid]['text'].strip() for gid in request['groups'] if groups[gid]['text'].strip()))
    return output['data']['labels'] if operation==core.FILTER else output['data']


def planned_filter_question(meter,question):
    """One question-wide brief for filtering; never an extraction field plan."""
    if 'evidence_task' in question:return question
    request={k:deepcopy(question[k]) for k in ('question','options') if k in question}
    key=json.dumps(request,ensure_ascii=False,sort_keys=True)
    if not hasattr(meter,'_o_filter_briefs'):meter._o_filter_briefs={}
    if key not in meter._o_filter_briefs:
        meter._o_filter_briefs[key]=decoded_call(meter,core.FILTER_PLAN,request)
    return dict(question,evidence_task=deepcopy(meter._o_filter_briefs[key]))


def extract_source(source,meter,folder):
    folder.mkdir(parents=True,exist_ok=True)
    request=core.extraction_input(source)
    output,_=meter.call(core.EXTRACT,request)
    fallback=None
    try:
        if output.get('parse_error') or output.get('data') is None:raise ValueError(str(output.get('parse_error')))
        units=core.candidates(source,output['data'])
        if units[0]['kind']=='raw_sentence':fallback=output['data']['extraction_status']+': no extracted information'
    except (ValueError,KeyError,TypeError) as exc:
        fallback='extraction_format_failure: '+str(exc)
        units=[dict(id='U0',kind='raw_sentence',group=source['id'],text=source['text'])]
    # Only format/unavailable failures fall back; model transport/deadline errors
    # propagate above and stop. No semantic source validator is called here.
    io.write(folder/'extraction.json',dict(request=request,output=output,fallback=fallback))
    return dict(source=source,candidates=units,fallback=fallback,source_validation_performed=False)


def process_source(question,source,meter,folder):
    extracted=extract_source(source,meter,folder)
    units=extracted['candidates'];fallback=extracted['fallback']
    request=core.filter_input(planned_filter_question(meter,question),source,units)
    io.write(folder/'filter_input.json',request)
    model_labels=decoded_call(meter,core.FILTER,request,audit_path=folder/'filter_decision.json')
    labels,overrides=core.preserve_action_core(units,model_labels)
    chosen=core.select(units,labels)
    result=dict(source=source,candidates=units,model_labels=model_labels,labels=labels,selected=chosen,fallback=fallback,
        source_validation_performed=False,program_overrides=overrides)
    io.write(folder/'selection.json',result)
    return result


def functional(out,backend,cfg):
    from script_core.budget import BudgetLedger,Meter
    ledger=BudgetLedger(600,answer_reserve=90,calls_path=out/'functional/calls.jsonl')
    meter=Meter(backend,ledger,cfg,out/'functional/raw_calls')
    gate=dict(passed=False,checks=[],semantic_diagnostics=[],error=None)
    q=dict(question='Where does C place the brick?',options=['on the brick wall','on the floor','inside a box','on a chair','in a bag'])
    source=dict(id='S0',text='C places a brick on the brick wall with his left hand.',source_intervals=[[107,108]])
    try:
        result=process_source(q,source,meter,out/'functional/extraction_filter')
        gate['checks'].append(dict(case='fresh_extraction_and_binary_filter',passed=result['fallback'] is None))
        rewrite=decoded_call(meter,core.COMPOSE,core.compose_input(q,result['selected'])) if result['selected'] else dict(text='')
        io.write(out/'functional/extraction_filter/composition.json',rewrite)
        kept=result['selected']
        gate['semantic_diagnostics'].append(dict(case='location_vs_hand',
            location_kept=any(u.get('field')=='location' for u in kept),
            body_part_dropped=not any(u.get('field')=='body_part' for u in kept),text=rewrite['text']))
        irrelevant=[dict(id='U0',kind='raw_sentence',group='S1',text='An unused chair is blue.')]
        labels=decoded_call(meter,core.FILTER,core.filter_input(q,dict(text=irrelevant[0]['text']),irrelevant))
        gate['checks'].append(dict(case='all_zero_sentence',passed=labels=={'U0':0},labels=labels))
        subject=[dict(id='U0',kind='action_field',group='actions_0',field='actor',value='someone')]
        composed=decoded_call(meter,core.COMPOSE,core.compose_input(dict(question='Who is explicitly identified?',options=['C','D','E']),subject))
        gate['checks'].append(dict(case='uninformative_placeholder_can_return_empty',passed=not composed['text'].strip(),output=composed))
        subject=[dict(id='U0',kind='action_field',group='actions_0',field='actor',value='C')]
        composed=decoded_call(meter,core.COMPOSE,core.compose_input(dict(question='Who is explicitly identified?',options=['C','D','E']),subject))
        gate['checks'].append(dict(case='selected_identity_alone_is_preserved',passed='C' in composed['text'],output=composed))
        subjects=[dict(id='U0',kind='action_field',group='actions_0',field='actor',value='D'),
                  dict(id='U1',kind='action_field',group='actions_1',field='actor',value='E')]
        composed=decoded_call(meter,core.COMPOSE,core.compose_input(dict(question='Which people are identified?',options=['D and E','F and G']),subjects))
        gate['checks'].append(dict(case='multiple_selected_identities_preserved',passed='D' in composed['text'] and 'E' in composed['text'],output=composed))
        for verb in ['moves','walks']:
            chosen=[dict(id='U0',kind='action_field',group='actions_0',field='verb',value=verb)]
            composed=decoded_call(meter,core.COMPOSE,core.compose_input(dict(question='Which movement is observed?',options=['moves','walks','runs']),chosen))
            gate['checks'].append(dict(case='preserve_isolated_'+verb,passed=verb in composed['text'].lower(),output=composed))
        chosen=[dict(id='U1',kind='action_field',group='actions_0',field='verb',value='walks'),
            dict(id='U4',kind='action_field',group='actions_3',field='verb',value='room')]
        composed=decoded_call(meter,core.COMPOSE,core.compose_input(dict(question='Which movement is observed?',options=['moves','walks','runs']),chosen))
        gate['checks'].append(dict(case='preserve_valid_verb_beside_malformed_field',passed='walks' in composed['text'].lower() and 'walks room' not in composed['text'].lower(),output=composed))
        for probe in io.read(out/'filter_repair_probes.json'):
            labels=decoded_call(meter,core.FILTER,probe['request'],audit_path=out/'functional/fixed_filter_probes'/f"{probe['id']}_decision.json")
            passed=all(labels[i]==1 for i in probe['required_keep']) and (not probe.get('all_zero') or not any(labels.values()))
            observation=dict(probe,labels=labels,passed=passed)
            io.write(out/'functional/fixed_filter_probes'/f"{probe['id']}.json",observation)
            gate['checks'].append(dict(case='fixed_filter_'+probe['id'],passed=passed,labels=labels))
        fallback=[dict(id='U0',kind='raw_sentence',group='S2',text='C places a brick on the brick wall.')]
        labels=decoded_call(meter,core.FILTER,core.filter_input(q,dict(text=fallback[0]['text']),fallback))
        gate['checks'].append(dict(case='raw_fallback_can_be_kept',passed=labels=={'U0':1},labels=labels))
        # Saved real source sentences, now extracted without their parent's
        # actions.base-only plan. Semantic outputs are reported, not overwritten.
        inputs=out/'cases'/CASES[1]
        packing=io.read(inputs/'question.json');sentences=io.read(inputs/'sentences.json')
        for sid in ['S5','S59','S93']:
            source_probe=next(s for s in sentences if s['id']==sid)
            probe=process_source(packing,source_probe,meter,out/'functional/source_first_probes'/sid)
            gate['semantic_diagnostics'].append(dict(case='source_first_'+sid,
                source=source_probe['text'],candidates=probe['candidates'],labels=probe['labels'],fallback=probe['fallback']))
            actors=[u for u in probe['candidates'] if u.get('field')=='actor' and u.get('value')=='C']
            gate['checks'].append(dict(case='extract_and_keep_explicit_actor_'+sid,
                passed=bool(actors) and all(probe['labels'][u['id']]==1 for u in actors)))
        people_q=dict(question='Who gives the cup to whom?',options=['D gives it to E.','E gives it to D.','Nobody gives a cup.'])
        people_source=dict(id='multi',text='#D D gives a cup to E.',source_intervals=[[1,2]])
        people=process_source(people_q,people_source,meter,out/'functional/multiple_participants')
        chosen=people['selected'];by_field={u.get('field'):u.get('value') for u in chosen}
        passed=by_field.get('actor')=='D' and by_field.get('recipient')=='E' and bool(by_field.get('verb')) and bool(by_field.get('patient'))
        gate['checks'].append(dict(case='preserve_actor_object_recipient',passed=passed,selected=chosen))
        composed=decoded_call(meter,core.COMPOSE,core.compose_input(people_q,chosen)) if chosen else dict(text='')
        io.write(out/'functional/multiple_participants/composition.json',composed)
        gate['checks'].append(dict(case='compose_explicit_participants',passed='D' in composed['text'] and 'E' in composed['text'],output=composed))
        # Full strict-letter reader paths for both representations.
        original=dict(head='Answer the multiple-choice question using the provided observations.',
            prompts=['  Description: placeholder\n\n###\n\n Questions: Where does C place the brick?\nA. on the brick wall\nB. on the floor\nC. inside a box\nD. on a chair\nE. in a bag'])
        decisions={'S0':result};compositions={'S0':rewrite}
        for arm,operation in [('O','o_answer'),('O+','oplus_answer')]:
            rows=core.present([source],decisions,composed=compositions if arm=='O+' else None)
            final=core.final_input(original,rows);io.write(out/'functional'/arm/'final_input.json',final)
            answer,_=meter.call(operation,dict(inputs=final,options=q['options']),final=True)
            io.write(out/'functional'/arm/'answer.json',answer)
            gate['checks'].append(dict(case=arm+'_strict_answer',passed=True))
            gate['semantic_diagnostics'].append(dict(case=arm+'_answer',correct=answer['data']['selected_option']=='0',answer=answer['data']))
        gate['passed']=all(c['passed'] for c in gate['checks'])
        if not gate['passed']:raise ValueError('O functional gate failed: '+str(gate['checks']))
    except Exception as exc:
        gate['error']=dict(type=type(exc).__name__,message=str(exc));raise
    finally:
        gate['cost_summary']=ledger.summary();gate['updated_at']=io.now()
        io.write(out/'validation/functional.json',gate);meter.close()
    return gate


def report(out):
    experiment=io.read(out/'experiment.json');ids=experiment['case_ids'];gold=io.read(out/'evaluation_only.json')
    if experiment.get('composition_context_eval'):
        from script.o_composition_context_eval import report as context_report
        return context_report(out)
    if experiment.get('functional_only'):
        gate=io.read(out/'validation/functional.json') if (out/'validation/functional.json').exists() else None
        result=dict(updated_at=io.now(),scope=experiment['scope'],planned=0,completed=0,
            complete=bool(gate and gate['passed']),errors=int(bool(gate and gate['error'])),functional=gate)
        result.update(execution_failures(out,[]))
        io.write(out/'report.json',result);return result
    rows=[]
    for uid in ids:
        p=out/'runs'/uid/'result.json'
        if p.exists():rows.append(io.read(p))
    result=dict(updated_at=io.now(),planned=len(ids),completed=sum(r.get('complete',False) for r in rows),
        complete=len(rows)==len(ids) and all(r.get('complete') for r in rows),errors=sum(bool(r.get('error')) for r in rows),
        cases=rows,arms={})
    for arm in ['O','O+']:
        answers=[r['arms'][arm] for r in rows if arm in r.get('arms',{})]
        result['arms'][arm]=dict(successful=len(answers),correct=sum(a['correct'] for a in answers),
            mean_input_tokens=sum(a['input_tokens'] for a in answers)/len(answers) if answers else None,
            mean_qa_seconds=sum(a['seconds'] for a in answers)/len(answers) if answers else None)
    if (out/'validation/functional.json').exists():result['functional']=io.read(out/'validation/functional.json')
    result.update(execution_failures(out,rows))
    io.write(out/'report.json',result);return result


def worker(out):
    from script_core.budget import ModelWorker,BudgetLedger,Meter
    from script.run_evidence_planner_experiment import verify_model
    verify(out);verify_model(out)
    if (out/'setup.json').exists():raise ValueError('Already attempted; no automatic retries')
    cfg=io.read(out/'qa_config.json');backend=ModelWorker(cfg)
    try:
        start=time.monotonic();io.write(out/'setup.json',dict(state='loading',started_at=io.now()))
        details=backend.call('prepare_vlm',{},600)
        io.write(out/'setup.json',dict(state='ready',seconds=time.monotonic()-start,details=details))
        if io.read(out/'experiment.json').get('composition_context_eval'):
            if io.read(out/'experiment.json').get('independent_sentence_batch_size'):
                from script.o_batch_validation import run as batch_validation
                from script.o_batched_matrix import run as batched_run
                if io.read(out/'experiment.json').get('composition_source_bound'):
                    from script.o_source_bound_validation import run as source_bound_validation
                    source_bound_validation(out,backend,cfg)
                batch_validation(out,backend,cfg)
                if io.read(out/'experiment.json').get('full_cohort'):
                    from script.run_o_full_experiment import run_live
                    run_live(out,backend,cfg)
                    return
                # The serial semantic probes remain recorded independently; no
                # prompt/schema changes are made to chase their semantic results.
                batched_run(out,backend,cfg)
                return
            from script.o_composition_context_eval import run
            run(out,backend,cfg)
            return
        functional(out,backend,cfg)
        print('O/O+ REAL FUNCTIONAL GATE PASSED',flush=True)
        policy=io.read(out/'experiment.json')
        if policy.get('functional_only'):
            io.write(out/'progress.json',dict(stage='functional_only_complete',updated_at=io.now()))
            return
        for index,uid in enumerate(policy['case_ids'],1):
            inputs=out/'cases'/uid;folder=out/'runs'/uid
            ledger=BudgetLedger(policy['case_budget_seconds'],answer_reserve=90,calls_path=folder/'calls.jsonl')
            meter=Meter(backend,ledger,cfg,folder/'raw_calls')
            result=dict(question_id=uid,complete=False,error=None,arms={},new_navigation=False)
            try:
                q=io.read(inputs/'question.json');sentences=io.read(inputs/'sentences.json')
                decisions={};compositions={}
                replay=None
                if policy.get('selection_replay_from'):
                    replay=io.read(inputs/'selection_replay/selection.json')
                    inherited=extraction_filter_costs(io.read(inputs/'selection_replay/result.json'))
                    ledger.dependency_cost=sum(inherited.values())
                    result['inherited_extraction_filter_costs']=inherited
                    result['selection_replay_from']=policy['selection_replay_from']
                for number,source in enumerate(sentences,1):
                    io.write(out/'progress.json',dict(stage='replay_selection' if replay is not None else 'extract_filter',case=index,question_id=uid,
                        sentence=number,total_sentences=len(sentences),updated_at=io.now()))
                    if replay is None:
                        decisions[source['id']]=process_source(q,source,meter,folder/'sentences'/source['id'])
                    else:
                        d=deepcopy(replay[source['id']]);assert d['source']==source
                        model_labels=d.get('model_labels',d['labels'])
                        labels,overrides=core.preserve_action_core(d['candidates'],model_labels)
                        d.update(model_labels=model_labels,labels=labels,program_overrides=overrides,
                            selected=core.select(d['candidates'],labels))
                        dst=folder/'sentences'/source['id'];dst.mkdir(parents=True,exist_ok=True)
                        for name in ['extraction.json','filter_input.json','filter_decision.json']:
                            src=inputs/'selection_replay/sentences'/source['id']/name
                            if src.exists():shutil.copy2(src,dst/name)
                        io.write(dst/'selection.json',d);decisions[source['id']]=d
                io.write(folder/'selection.json',decisions)
                original=io.read(inputs/'original_qa.json')
                # O's input is independent of the later composition calls.
                rows=core.present(sentences,decisions);final=core.final_input(original,rows)
                io.write(folder/'O/retained.json',rows);io.write(folder/'O/final_input.json',final)
                structured_final=final
                for number,source in enumerate(sentences,1):
                    chosen=decisions[source['id']]['selected']
                    io.write(out/'progress.json',dict(stage='compose',case=index,question_id=uid,
                        sentence=number,total_sentences=len(sentences),updated_at=io.now()))
                    if chosen:
                        request=core.compose_input(q,chosen)
                        io.write(folder/'sentences'/source['id']/'composition_input.json',request)
                        composed=decoded_call(meter,core.COMPOSE,request)
                    else:composed=dict(text='')
                    compositions[source['id']]=composed
                    io.write(folder/'sentences'/source['id']/'composition.json',dict(composed,
                        model_called=bool(chosen),emptied_after_selection=bool(chosen) and not composed['text'].strip()))
                rows=core.present(sentences,decisions,composed=compositions);final=core.final_input(original,rows)
                io.write(folder/'Oplus/retained.json',rows);io.write(folder/'Oplus/final_input.json',final)
                # Both final calls follow composition, so its cost does not
                # consume the ledger's held final-generation reserve.
                answer,call=meter.call('o_answer',dict(inputs=structured_final,options=q['options']),final=True)
                io.write(folder/'O/answer.json',answer)
                gold=io.read(out/'evaluation_only.json')[uid]
                result['arms']['O']=dict(answer=answer['data'],correct=str(answer['data']['selected_option'])==str(gold),
                    input_tokens=answer['usage']['input_tokens'],seconds=call['actual_cost'])
                io.write(folder/'result.json',result)
                answer,call=meter.call('oplus_answer',dict(inputs=final,options=q['options']),final=True)
                io.write(folder/'Oplus/answer.json',answer)
                result['arms']['O+']=dict(answer=answer['data'],correct=str(answer['data']['selected_option'])==str(gold),
                    input_tokens=answer['usage']['input_tokens'],seconds=call['actual_cost'])
                result.update(complete=True,gold=str(gold),sentences=len(sentences),
                    candidates=sum(len(d['candidates']) for d in decisions.values()),
                    retained_units=sum(len(d['selected']) for d in decisions.values()),
                    action_core_adjustments=sum(len(d['program_overrides']) for d in decisions.values()),
                    all_zero_sentences=sum(not d['selected'] for d in decisions.values()),
                    fallback_sentences=sum(bool(d['fallback']) for d in decisions.values()),
                    composition_empty_sentences=sum(bool(decisions[s]['selected']) and not c['text'].strip() for s,c in compositions.items()),
                    baseline_N=io.read(inputs/'parent_N/result.json')['answer'],
                    shared_selection_hash=io.sha(folder/'selection.json'))
            except Exception as exc:
                result['error']=dict(type=type(exc).__name__,message=str(exc));raise
            finally:
                result['cost_summary']=ledger.summary();result['updated_at']=io.now()
                io.write(folder/'result.json',result);meter.close();report(out)
            print('PAIRED CASE COMPLETE',index,uid,result['arms'],flush=True)
    except Exception as exc:
        io.write(out/'failure.json',dict(type=type(exc).__name__,message=str(exc),updated_at=io.now(),
            progress=io.read(out/'progress.json') if (out/'progress.json').exists() else None))
        raise
    finally:
        backend.close(graceful=True);report(out)


def queue(out):
    from script.gpu_queue import acquire
    verify(out)
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('No automatic retry')
        def status(state,**extra):io.write(out/'queue_status.json',dict(state=state,controller_pid=os.getpid(),updated_at=io.now(),**extra))
        try:
            with acquire(io.read(out/'experiment.json')['resource_policy'],lambda d:status('waiting_for_gpu',**d)) as (gpu,fds):
                env=dict(os.environ,VIDEOAGENT_GPU_LEASE=gpu['reservation_id'],CUDA_VISIBLE_DEVICES=gpu['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',HF_HUB_OFFLINE='1',
                    TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
                command=[sys.executable,'-u',str(out/'runtime/script/run_o_sentence_experiment.py'),'worker','--out',str(out)]
                with (out/'worker.log').open('a') as log:
                    child=subprocess.Popen(command,cwd=out/'runtime',env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,pass_fds=fds)
                launch=dict(worker_pid=child.pid,gpu=gpu['index'],command=command,started_at=io.now());io.write(out/'launch.json',launch)
                while child.poll() is None:status('running',**launch);time.sleep(5)
                if child.returncode or not report(out)['complete']:raise RuntimeError('O/O+ diagnostic failed; records retained')
                status('complete',**launch)
        except Exception as exc:status('failed',error=dict(type=type(exc).__name__,message=str(exc)));raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','queue','worker','report']);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--functional-only',action='store_true',help='Prepare source-first probes without case QA evaluation')
    p.add_argument('--selection-replay-from',type=Path,help='Freeze completed extraction/filter results; apply current action-core policy and regenerate both presentations/answers')
    p.add_argument('--composition-context-eval',action='store_true',help='2x2 O/O+ and question-only/question-plus-options filtering/composition on frozen extractions')
    p.add_argument('--semantic-diagnostic',action='store_true',help='Retain semantic probe failures as measured outcomes in the two-case matrix; parsing/runtime failures still stop')
    p.add_argument('--case-count',type=int,choices=[2,10],default=2,help='Known two controls, or eight additional cases plus those controls')
    args=p.parse_args();out=args.out.resolve()
    if args.mode=='report':print(json.dumps(report(out),ensure_ascii=False))
    elif args.mode=='prepare':prepare(out,functional_only=args.functional_only,selection_replay_from=args.selection_replay_from,composition_context_eval=args.composition_context_eval,semantic_diagnostic=args.semantic_diagnostic,case_count=args.case_count)
    else:globals()[args.mode](out)
