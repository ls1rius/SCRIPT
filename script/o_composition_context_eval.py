"""2x2 O/O+ and optional-options experiment with frozen per-sentence extractions."""
import json
from script import controlled_memory_io as io
from script_core import o_sentence_filter as core
from script_core.budget import BudgetLedger, Meter

CONDITIONS={'question_only':False,'question_options':True}
ARMS=[f'{kind}_{condition}' for condition in CONDITIONS for kind in ['O','Oplus']]

def conditions_for(policy):
    names=policy.get('conditions',list(CONDITIONS))
    if not names or len(set(names))!=len(names) or any(n not in CONDITIONS for n in names):
        raise ValueError('Invalid O condition selection')
    if policy.get('full_cohort') and names!=['question_only']:
        raise ValueError('Full O/O+ run only permits question-only conditioning')
    return {name:CONDITIONS[name] for name in names}

def report(out):
    from script.run_o_sentence_experiment import execution_failures
    policy=io.read(out/'experiment.json');rows=[]
    for uid in policy['case_ids']:
        path=out/'runs'/uid/'result.json'
        if path.exists():rows.append(io.read(path))
    arms=[f'{kind}_{condition}' for condition in conditions_for(policy) for kind in ['O','Oplus']]
    result=dict(updated_at=io.now(),scope=policy.get('scope','2x2 O/O+ and optional options; frozen extraction, fresh per-condition filters, composition and answers.'),
        completed=sum(r.get('complete',False) for r in rows),planned=len(policy['case_ids']),
        complete=len(rows)==len(policy['case_ids']) and all(r.get('complete') for r in rows),
        errors=sum(bool(r.get('error')) for r in rows),cases=rows,arms={})
    for arm in arms:
        values=[r['arms'][arm] for r in rows if arm in r.get('arms',{})]
        result['arms'][arm]=dict(completed=len(values),correct=sum(a['correct'] for a in values),
            mean_input_tokens=sum(a['input_tokens'] for a in values)/len(values) if values else None,
            mean_qa_seconds=sum(a['answer_seconds'] for a in values)/len(values) if values else None)
    result['subgroups']={}
    for label,key in [('additional','additional_case_ids'),('known_controls','known_case_ids')]:
        ids=policy.get(key,[]);subset=[r for r in rows if r['question_id'] in ids]
        result['subgroups'][label]=dict(planned=len(ids),completed=sum(r.get('complete',False) for r in subset),arms={})
        for arm in arms:
            values=[r['arms'][arm] for r in subset if arm in r.get('arms',{})]
            result['subgroups'][label]['arms'][arm]=dict(completed=len(values),correct=sum(a['correct'] for a in values))
    p=out/'validation/functional.json'
    if p.exists():
        result['functional']=io.read(p)
        result['semantic_probe_passed']=result['functional']['passed']
    result['plus_version']=policy.get('plus_version',core.PLUS_VERSION)
    if policy.get('composition_source_bound'):
        result['composition_source_bound']=True
        gate=out/'validation/source_bound_functional.json'
        result['source_bound_gate_passed']=io.read(gate)['passed'] if gate.exists() else None
    result['diagnostic_only']=not policy.get('full_cohort',False)
    result['new_navigation']=False
    result['timing_policy']='Shared GPU measurements, not a controlled speed comparison.'
    result['costs']=dict(new_extraction_seconds=sum(r.get('shared_extraction_cost_summary',{}).get('total',0) for r in rows),
        reused_extraction_seconds=sum(r.get('shared_extraction_seconds',0) for r in rows if not r.get('new_extraction',False)),
        condition_seconds=sum(c.get('cost_summary',{}).get('total',0) for r in rows for c in r.get('conditions',{}).values()))
    upgrade=out/'upgrade_audit.json'
    if upgrade.exists():
        result['costs']['legacy_Oplus_seconds_excluded']=io.read(upgrade)['legacy_composition_and_answer_seconds']
        result['costs']['legacy_run']=policy['previous_run']
    if policy.get('full_cohort'):
        result['parent_N']=policy['parent_N']
        result['ready_source_cases']=sum((out/'cases'/uid/'source_hashes.json').exists() for uid in policy['case_ids'])
    result.update(execution_failures(out,rows))
    io.write(out/'report.json',result)
    return result


def probes(out,backend,cfg):
    from script.run_o_sentence_experiment import decoded_call
    ledger=BudgetLedger(600,answer_reserve=0,calls_path=out/'functional/calls.jsonl')
    meter=Meter(backend,ledger,cfg,out/'functional/raw_calls')
    gate=dict(passed=False,checks=[],error=None)
    def units(values):
        return [dict(id=f'U{i}',kind='action_field',group=g,field=f,value=v) for i,(g,f,v) in enumerate(values)]
    cases=[
        dict(id='actor_patient',question='What does D do with the cup?',options=['D gives the cup to E.','D breaks the cup.'],
            values=[('a','actor','D'),('a','verb','gives'),('a','patient','a cup'),('a','recipient','E')],required=['D','cup','E']),
        dict(id='question_cannot_supply_action',question='How does C build a brick wall?',options=['C places bricks on the wall.','C paints the wall.'],
            values=[('a','actor','C')],forbidden=['brick','wall','build','paint','place','acts']),
        dict(id='options_cannot_supply_fact',question='Where does D put the cup?',options=['D puts it in a red bag.','D puts it on a wooden table.'],
            values=[('a','actor','D'),('a','verb','puts'),('a','patient','a cup')],required=['D','cup'],forbidden=['red','bag','wood','table']),
        dict(id='partial_repeated_evidence',question='Which repeated action characterizes the repair?',options=['Repeatedly tightens screws.','Repeatedly paints walls.'],
            values=[('a','actor','D'),('a','verb','tightens'),('a','patient','a screw')],required=['D','screw']),
        dict(id='subject_identity_question',question='Which people are identified?',options=['D and E.','F and G.'],
            values=[('a','actor','D'),('b','actor','E')],required=['D','E']),
        dict(id='uninformative_placeholder',question='Who performs the action?',options=['D.','E.'],
            values=[('a','actor','someone')],empty=True),
        dict(id='independent_events',question='What do D and E do?',options=['D opens a drawer and E reads a book.','E opens a drawer and D reads a book.'],
            values=[('a','actor','D'),('a','verb','opens'),('a','patient','drawer'),('b','actor','E'),('b','verb','reads'),('b','patient','book')],required=['D','E','drawer','book'])]
    try:
        # Exercise the runtime repair with the actual model before case work.
        for actor in ['繫','D"E']:
            request=core.compose_input(dict(question='Who is explicitly identified?'),units([('a','actor',actor)]))
            response=decoded_call(meter,core.COMPOSE,request)
            if response['groups']['a']['text'] not in ['',f'Actor: {actor}.']:
                raise ValueError('Identity decoder failed to restore the canonical value')
            gate.setdefault('technical_checks',[]).append(dict(case='identity_alias',actor=actor,passed=True,output=response))
        for case in cases:
            chosen=units(case['values'])
            for arm,options in CONDITIONS.items():
                request=core.compose_input(case,chosen,include_options=options)
                folder=out/'functional'/case['id']/arm
                io.write(folder/'input.json',request)
                response=decoded_call(meter,core.COMPOSE,request);io.write(folder/'output.json',response)
                txt=response['text'];passed=all(w in txt for w in case.get('required',[]))
                passed=passed and not any(w in txt.lower() for w in case.get('forbidden',[]))
                if case.get('empty'):passed=passed and not txt.strip()
                if case['id']=='independent_events':
                    a=response['groups']['a']['text'];b=response['groups']['b']['text']
                    passed=passed and 'D' in a and 'drawer' in a and 'E' not in a and 'book' not in a
                    passed=passed and 'E' in b and 'book' in b and 'D' not in b and 'drawer' not in b
                gate['checks'].append(dict(case=case['id'],arm=arm,passed=passed,output=response))
        from script.run_o_sentence_experiment import planned_filter_question
        for condition,include_options in CONDITIONS.items():
            q=dict(question='Which repeated action characterizes the repair?')
            if include_options:q['options']=['Repeatedly tightens screws.','Repeatedly paints walls.']
            q=planned_filter_question(meter,q)
            chosen=units([('a','actor','D'),('a','verb','tightens'),('a','patient','a screw')])
            req=core.filter_input(q,dict(text='D tightens a screw.'),chosen)
            labels=decoded_call(meter,core.FILTER,req,audit_path=out/'functional'/condition/'partial_filter.json')
            gate['checks'].append(dict(case='partial_filter',arm=condition,passed=all(labels.values()),labels=labels))
            negative=[dict(id='U0',kind='attributes',group='attribute',record=dict(entity='window',property='color',value='blue'))]
            req=core.filter_input(q,dict(text='The window is blue.'),negative)
            labels=decoded_call(meter,core.FILTER,req,audit_path=out/'functional'/condition/'irrelevant_filter.json')
            gate['checks'].append(dict(case='irrelevant_filter',arm=condition,passed=labels=={'U0':0},labels=labels))
        gate['passed']=all(c['passed'] for c in gate['checks'])
        gate['execution_allowed_for_diagnostic']=bool(io.read(out/'experiment.json').get('semantic_diagnostic'))
        if not gate['passed'] and not gate['execution_allowed_for_diagnostic']:
            raise ValueError('Composition context gate failed')
    except Exception as exc:
        gate['error']=dict(type=type(exc).__name__,message=str(exc));raise
    finally:
        gate['cost_summary']=ledger.summary();gate['updated_at']=io.now()
        io.write(out/'validation/functional.json',gate);meter.close()


def run(out,backend,cfg):
    from copy import deepcopy
    from script.run_o_sentence_experiment import decoded_call,planned_filter_question,extraction_filter_costs,extract_source
    probes(out,backend,cfg)
    policy=io.read(out/'experiment.json');gold=io.read(out/'evaluation_only.json')
    for index,uid in enumerate(policy['case_ids'],1):
        inputs=out/'cases'/uid;folder=out/'runs'/uid
        question=io.read(inputs/'question.json');sources=io.read(inputs/'sentences.json')
        replay=(inputs/'selection_replay/selection.json').exists()
        frozen=io.read(inputs/'selection_replay/selection.json') if replay else {}
        previous=io.read(inputs/'selection_replay/result.json') if replay else {}
        original=io.read(inputs/'original_qa.json')
        extraction_cost=extraction_filter_costs(previous)[core.EXTRACT] if replay else 0.
        result=dict(question_id=uid,complete=False,error=None,arms={},conditions={},
            archived_controls=previous.get('arms',{}),frozen_extraction_reference=str(inputs/'selection_replay') if replay else None,
            shared_extraction_seconds=extraction_cost,new_extraction=not replay,new_filtering=True)
        try:
            if not replay:
                target=folder/'shared_extraction'
                ledger=BudgetLedger(policy['case_budget_seconds'],answer_reserve=4*cfg['budget']['final_answer_reserve'],calls_path=target/'calls.jsonl')
                meter=Meter(backend,ledger,cfg,target/'raw_calls')
                try:
                    for number,source in enumerate(sources,1):
                        io.write(out/'progress.json',dict(stage='shared_extraction',case=index,question_id=uid,
                            sentence=number,total_sentences=len(sources),updated_at=io.now()))
                        frozen[source['id']]=extract_source(source,meter,target/'sentences'/source['id'])
                        io.write(target/'candidates.json',frozen)
                finally:
                    result['shared_extraction_cost_summary']=ledger.summary()
                    extraction_cost=ledger.summary()['total'];result['shared_extraction_seconds']=extraction_cost
                    io.write(target/'cost_summary.json',ledger.summary());meter.close()
            for condition,include_options in CONDITIONS.items():
                target=folder/condition
                ledger=BudgetLedger(policy['case_budget_seconds'],answer_reserve=2*cfg['budget']['final_answer_reserve'],calls_path=target/'calls.jsonl')
                ledger.dependency_cost=extraction_cost+sum(d['cost_summary']['total'] for d in result['conditions'].values())
                meter=Meter(backend,ledger,cfg,target/'raw_calls');compositions={};decisions={}
                result['conditions'][condition]=dict(complete=False,error=None)
                try:
                    q=dict(question=question['question'])
                    if include_options:q['options']=deepcopy(question['options'])
                    q=planned_filter_question(meter,q)
                    for number,source in enumerate(sources,1):
                        io.write(out/'progress.json',dict(stage='matrix_filter',case=index,question_id=uid,condition=condition,
                            sentence=number,total_sentences=len(sources),updated_at=io.now()))
                        d=deepcopy(frozen[source['id']]);assert d['source']==source
                        sf=target/'sentences'/source['id'];request=core.filter_input(q,source,d['candidates'])
                        raw=decoded_call(meter,core.FILTER,request,audit_path=sf/'filter_decision.json')
                        labels,adjustments=core.preserve_action_core(d['candidates'],raw)
                        d.update(model_labels=raw,labels=labels,program_overrides=adjustments,selected=core.select(d['candidates'],labels))
                        decisions[source['id']]=d;io.write(sf/'selection.json',d)
                    io.write(target/'selection.json',decisions)
                    for number,source in enumerate(sources,1):
                        io.write(out/'progress.json',dict(stage='matrix_compose',case=index,question_id=uid,condition=condition,
                            sentence=number,total_sentences=len(sources),updated_at=io.now()))
                        chosen=decisions[source['id']]['selected'];sf=target/'sentences'/source['id']
                        if chosen:
                            request=core.compose_input(question,chosen,include_options=include_options)
                            io.write(sf/'composition_input.json',request)
                            composed=decoded_call(meter,core.COMPOSE,request)
                        else:composed=dict(groups={},text='')
                        compositions[source['id']]=composed;io.write(sf/'composition.json',composed)
                    for kind,operation in [('O','o_answer'),('Oplus','oplus_answer')]:
                        arm=f'{kind}_{condition}';af=folder/arm
                        rows=core.present(sources,decisions,composed=compositions if kind=='Oplus' else None)
                        final=core.final_input(original,rows)
                        io.write(af/'retained.json',rows);io.write(af/'final_input.json',final)
                        answer,call=meter.call(operation,dict(inputs=final,options=question['options']),final=True)
                        io.write(af/'answer.json',answer)
                        result['arms'][arm]=dict(answer=answer['data'],correct=str(answer['data']['selected_option'])==str(gold[uid]),
                            input_tokens=answer['usage']['input_tokens'],retained_sentences=len(rows),answer_seconds=call['actual_cost'],
                            shared_selection_hash=io.sha(target/'selection.json'))
                        io.write(folder/'result.json',result)
                    result['conditions'][condition].update(complete=True,
                        retained_units=sum(len(d['selected']) for d in decisions.values()),
                        all_zero_sentences=sum(not d['selected'] for d in decisions.values()),
                        composition_empty_sentences=sum(bool(decisions[s]['selected']) and not c['text'].strip() for s,c in compositions.items()))
                except Exception as exc:
                    result['conditions'][condition]['error']=dict(type=type(exc).__name__,message=str(exc))
                    raise
                finally:
                    result['conditions'][condition]['cost_summary']=ledger.summary()
                    io.write(target/'cost_summary.json',ledger.summary())
                    io.write(folder/'result.json',result);meter.close()
                io.write(folder/'result.json',result)
            result['complete']=True
        except Exception as exc:
            result['error']=dict(type=type(exc).__name__,message=str(exc));raise
        finally:
            result['updated_at']=io.now();io.write(folder/'result.json',result);report(out)
