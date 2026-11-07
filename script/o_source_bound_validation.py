"""Real-model source-bound composition gate; QA labels are never read."""
from script import controlled_memory_io as io
from script.o_batched_matrix import batch_call,decode
from script_core import o_sentence_filter as core
from script_core.budget import BudgetLedger,Meter
from script_core.o_batch_generation import messages_for


def run(out,backend,cfg):
    target=out/'source_bound_functional';ledger=BudgetLedger(600,answer_reserve=0,calls_path=target/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,target/'raw_calls');gate=dict(passed=False,checks=[],error=None,version=core.PLUS_VERSION)
    cases=[('7dcf85fb-1217-4eb7-b9c8-cc2ab06507fe',['S18']),
           ('5d15c716-f179-462c-84a6-fa94e9d14e94',['S80','S111']),
           ('98427b0b-20ed-45bb-b772-91573ce90b5a',['S7','S8'])]
    try:
        for uid,sids in cases:
            q=io.read(out/'cases'/uid/'question.json');requests=[]
            for sid in sids:
                selected=io.read(out/'runs'/uid/'question_only/sentences'/sid/'selection.json')['selected']
                requests.append((sid,core.compose_input(q,selected)))
            rows=batch_call(meter,uid,'question_only',core.COMPOSE,requests)
            for (sid,request),row in zip(requests,rows):
                composed=decode(core.COMPOSE,request,row)
                if row['prompt']!=messages_for(core.COMPOSE,request)[0]:raise ValueError('Contaminated composition prompt')
                if not composed['text']:raise ValueError('Meaningful diagnostic evidence was emptied')
                # Enforce the same boundary used before final QA.
                selected=[u for group in request['groups'].values() for u in group]
                core.present([dict(id=sid,source_intervals=[])],{sid:dict(selected=selected)},composed={sid:composed})
                gate['checks'].append(dict(question_id=uid,source_id=sid,passed=True,request=request,output=composed))
        def units(fields):
            return [dict(id=f'U{i}',kind='action_field',group='a',field=k,value=v) for i,(k,v) in enumerate(fields.items())]
        chosen=[units(dict(actor='D',verb='may not give',patient='cup',recipient='E',location='room')),
                units(dict(actor='繫"甲',verb='does not put',patient='木板',destination='箱子')),
                [dict(id='U0',kind='raw_sentence',group='a',text='D did not enter the garage.')],
                [dict(id='U0',kind='changes',group='a',record=dict(entity='door',before='closed',after='possibly open'))]]
        requests=[(f'S{i}',core.compose_input(dict(question='What is explicitly observed?'),c)) for i,c in enumerate(chosen)]
        for (sid,req),row in zip(requests,batch_call(meter,'source_bound_probe','question_only',core.COMPOSE,requests)):
            composed=decode(core.COMPOSE,req,row)
            if not composed['text']:raise ValueError('Meaningful unseen probe emptied')
            gate['checks'].append(dict(question_id='source_bound_probe',source_id=sid,passed=True,request=req,output=composed))
        gate['passed']=True
    except Exception as exc:
        gate['error']=dict(type=type(exc).__name__,message=str(exc));raise
    finally:
        gate['cost_summary']=ledger.summary();gate['updated_at']=io.now()
        io.write(out/'validation/source_bound_functional.json',gate);meter.close()
