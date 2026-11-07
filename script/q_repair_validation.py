"""Independent synthetic semantic gates; no benchmark examples or labels."""
from script.scale_live_parallel_pool import write, now
from script_core import q_repair as r
from script_core.budget import BudgetLedger, Meter


def source(sid,text,t):
    return dict(id=sid,text=text,source_intervals=[[t,t+1]],source_caption_ids=['C'+sid[1:]])


def validate_live(backend,cfg,folder):
    from script.run_q_repair_experiment import ask, run_case
    folder.mkdir(parents=True,exist_ok=True)
    ledger=BudgetLedger(600,answer_reserve=90,calls_path=folder/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,folder/'raw_calls');checks=[]
    try:
        examples=[
            ('components','Describe how the measuring instrument is assembled.',
             ['N seats a rubber gasket in the casing.','N fastens the casing screws.','A radio in another building plays music.'],
             ['keep','keep','drop']),
            ('partial_stages','Describe the preparation and installation stages of this work.',
             ['N unwraps the protective packaging.','N positions the sensor in its bracket.','N connects the sensor cable.'],
             ['keep','keep','keep']),
            ('process_context','What role does the solvent have in the tool maintenance?',
             ['N dips the used scraper into solvent.','N wipes residue from the scraper.','N returns the scraper to its rack.'],
             ['keep','keep',None]),
            ('negative_and_modal','What happens to the latch?',
             ['N does not release the latch.','N may rotate the latch.','N presses the latch lever.'],
             ['keep','keep','keep']),
        ]
        for name,question,texts,expected in examples:
            ss=[source(f'S{i}',t,i) for i,t in enumerate(texts)]
            req=r.windows(question,ss)[0]
            value=ask(meter,'q3-gate-'+name,'q3_filter',[req])[0]
            # The effect on the work object is essential context. Subsequent
            # storage is optional for this purpose question; do not require
            # blanket retention of every adjacent same-object action.
            actual=[r.decision(value[s['id']]) for s in ss]
            checks.append(dict(name=name,passed=all(e is None or a==e for a,e in zip(actual,expected)),
                request=req,verdict=value,expected=expected,
                expectation_note='null means no mandated label; literal output remains archived'))
        question='What sequence of operations does N perform?'
        options=['N opens the case and then calibrates a sensor.','N opens the case and then discards a sensor.']
        req=dict(question=question,options=options,option_index=0,
                 observations=r.views([source('S0','N opens the case.',0)]))
        value=ask(meter,'q3-gate-partial-option','q3_option_audit',[req])[0]
        checks.append(dict(name='candidate_partial_not_supported',passed=value['status']=='unresolved',request=req,verdict=value))
        for i in range(2):
            req=dict(question='Which tray receives the sensor?',options=['N puts the sensor in the red tray.','N puts the sensor in the blue tray.'],
                     option_index=i,observations=r.views([source('S0','N puts the sensor in the red tray.',0)]))
            value=ask(meter,'q3-gate-explicit-slot','q3_option_audit',[req])[0]
            checks.append(dict(name='explicit_slot_'+str(i),passed=value['status']==('supported' if i==0 else 'contradicted'),request=req,verdict=value))
        req=dict(question='Compare the first half and second half of the work.',options=['Preparation then calibration.','Calibration then preparation.'],
                 observations=r.views([source('S0','N opens a storage case.',0)]),
                 regions=[dict(id='first_half',interval=[0,2]),dict(id='second_half',interval=[2,4])])
        value=ask(meter,'q3-gate-time-gap','q3_coverage',[req])[0]
        checks.append(dict(name='missing_second_half',passed=value['second_half']['status']=='unresolved',request=req,verdict=value))
        # Four independent windows of the same question test actual tensor batching.
        requests=[r.windows('Which parts are being installed?', [source('S'+str(i),'N installs a sensor in bracket '+str(i)+'.',i)])[0]
                  for i in range(4)]
        values=ask(meter,'q3-gate-batch','q3_filter',requests)
        checks.append(dict(name='batch4',passed=all(r.decision(v['S'+str(i)])=='keep' for i,v in enumerate(values)),verdicts=values))
        if not all(c['passed'] for c in checks):
            raise ValueError('Q3 semantic gate failed; formal cohort not started')
    finally:
        write(folder/'functional.json',dict(passed=len(checks)==9 and all(c['passed'] for c in checks),checks=checks,
              cost_summary=ledger.summary(),at=now(),scope='Independent synthetic probes; no QA improvement claim'))
        meter.close()
    question=dict(question_id='q3-gate-retrieval',question='What happens in the first half and second half?',
        options=['N opens a case, then puts a sensor in a red tray.','N opens a case, then puts a sensor in a blue tray.'])
    archive=dict(scope=[0,4],inherited_N_budgeted_seconds=0,
        captions=[dict(id='C0',text='N opens a case.',interval=[0,1]),dict(id='C1',text='N puts a sensor in a red tray.',interval=[3,4])],
        scenes=[dict(id='T0',interval=[0,2],caption_ids=['C0'],description='Opening a case.'),
                dict(id='T1',interval=[2,4],caption_ids=['C1'],description='Placing a sensor into a tray.')])
    original=dict(query_id=question['question_id'],head='Choose the option supported by the observations.',
        prompts=['  Description: old evidence\n\n###\n\n Questions: '+question['question']+' \n Options: \n A: '+question['options'][0]+
                 ' \n B: '+question['options'][1]+' \n\n###\n\n The prediction is:'])
    result=run_case(backend,cfg,question,[source('S0','N opens a case.',0)],original,folder/'reader_pair',archive,'coverage_repair')
    passed=(result['complete'] and len(result['retrievals'])==1 and result['retrievals'][0]['scene_id']=='T1'
            and all(str(a['answer']['selected_option'])=='0' for a in result['arms'].values()))
    write(folder/'reader_gate.json',dict(passed=passed,result=result,at=now()))
    if not passed:raise ValueError('Q3 source expansion / reader gate failed')
