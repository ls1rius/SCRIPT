"""Synthetic V4 probes require actual readiness, not just correct final answers."""
from script.scale_live_parallel_pool import write,now
from script_core import q_v4 as v4
from script_core.budget import BudgetLedger,Meter
from script.q_repair_validation import source


def probe_requests():
    cases=[
      ('literal_choice_C','What does M do with the tile?',
       ['M paints the tile.','M breaks the tile.','M slides the tile into a slot.'],
       ['M slides the green tile into a slot.'],True,'C'),
      ('grounded_function','Why is the clamp used during the work?',
       ['To hold the boards in position while drilling.','To paint the boards.','To produce a sound.'],
       ['M aligns two boards.','M secures the aligned boards with a clamp.','M drills a hole through the clamped boards.'],True,'A'),
      ('missing_later_action','Which sequence does M perform?',
       ['M closes a box and then weighs it.','M closes a box and then wraps it.'],
       ['M closes the box.'],False,None),
      ('wrong_actor','How does N assist M in cleaning?',
       ['N wipes the desk alongside M.','N hands M a cloth.'],
       ['M wipes the desk.','N stands beside a window.'],False,None),
      ('modal_not_fact','What happens to the hatch?',
       ['M unlocks the hatch.','M keeps the hatch locked.'],
       ['M may unlock the hatch.'],False,None),
      ('specific_motive_missing','What is the intended destination of the labelled tube?',
       ['It is being sent to an overseas buyer.','It is being sent to a local laboratory.'],
       ['M sticks a label on a sealed tube.'],False,None),
      ('negation','What does M do at the panel?',
       ['M opens the hatch.','M checks the indicator without opening the hatch.'],
       ['M does not open the hatch.','M checks the indicator light.'],True,'B'),
      ('paraphrase','What activity does M perform on the lens?',
       ['M cleans the lens.','M replaces the lens.'],
       ['M wipes dust off the lens.'],True,'A'),
      ('question_premise','How do M and N communicate during the work?',
       ['They never communicate.','They discuss their plan.'],
       ['M carries a tray.','N folds a cloth.'],False,None),
      ('empty','Which operation is performed?', ['Folding a sheet.','Cutting a sheet.'],[],False,None),
      ('qualified_option','Which possible action is mentioned?',
       ['M may lower the barrier.','M may ring the chime.'],['M may lower the barrier.'],True,'A'),
      ('conditional_not_fact','Which action has happened?',
       ['M closes the shutter.','M raises the flag.'],['If the wind rises, M closes the shutter.'],False,None),
    ]
    result=[]
    for name,question,options,texts,ready,choice in cases:
        request=dict(question=question,options=options,observations=v4.views([source('S'+str(i),t,i) for i,t in enumerate(texts)]),regions=[])
        result.append((name,request,ready,choice))
    options=['M opens a tin, then paints a marker red.','M paints a marker red, then opens a tin.']
    question='What happens in the first half and second half?'
    regions=[dict(id='first_half',interval=[0,2]),dict(id='second_half',interval=[2,4])]
    for full in [False,True]:
        ss=[source('S0','M opens a tin.',0)]+([source('S1','M paints a marker red.',3)] if full else [])
        result.append(('two_halves_complete' if full else 'two_halves_missing',
            dict(question=question,options=options,observations=v4.views(ss),regions=regions),full,'A' if full else None))
    return result


def synthetic_case():
    question=dict(question_id='q4-synthetic-retrieval',question='What happens in the first half and second half?',
        options=['M opens a tin, then paints a marker red.','M opens a tin, then paints a marker blue.'])
    archive=dict(scope=[0,4],inherited_N_budgeted_seconds=0,
        captions=[dict(id='C0',text='M opens a tin.',interval=[0,1]),dict(id='C1',text='M paints a marker red.',interval=[3,4]),
                  dict(id='C2',text='A distant flag moves in the wind.',interval=[3,4])],
        scenes=[dict(id='T0',interval=[0,2],caption_ids=['C0'],description='Opening a tin.'),
                dict(id='T1',interval=[2,4],caption_ids=['C1'],description='Painting a marker.'),
                dict(id='T2',interval=[2,4],caption_ids=['C2'],description='A distant flag in the wind.')])
    original=dict(query_id=question['question_id'],head='Choose the option supported by the observations.',
        prompts=['  Description: old evidence\n\n###\n\n Questions: '+question['question']+' \n Options: \n A: '+question['options'][0]+
                 ' \n B: '+question['options'][1]+' \n\n###\n\n The prediction is:'])
    return question,archive,original,[source('S0','M opens a tin.',0)]


def validate_live(backend,cfg,folder):
    from script.run_q_v4_experiment import ask,run_case,assess
    folder.mkdir(parents=True,exist_ok=True)
    ledger=BudgetLedger(900,answer_reserve=90,calls_path=folder/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,folder/'raw_calls');checks=[]
    try:
        for name,request,expected,choice in probe_requests():
            cert=assess(meter,'q4-gate-'+name,request)
            value=cert['assessment']
            passed=cert['sufficient']==expected and (choice is None or value['proposal']['choice']==choice)
            forced_pair=None
            if not expected and request['observations']:
                forced_pair=ask(meter,'q4-gate-'+name,'q4_pair',[dict(request,candidate_index=0,alternative_index=1)],batch_size=1)[0]
                passed=passed and (forced_pair['choice']=='U' or v4.uncertainty_block(
                    dict(request,candidate_index=0,alternative_index=1),forced_pair))
            checks.append(dict(name=name,passed=passed,forced_pair=forced_pair,expected_ready=expected,expected_choice=choice,request=request,assessment=value,certificate=cert))
        request=probe_requests()[0][1]
        reverse=ask(meter,'q4-reverse-proposal','q4_pair',[dict(request,candidate_index=0,alternative_index=2)],batch_size=1)[0]
        checks.append(dict(name='reject_wrong_proposal',passed=reverse['choice']=='B',verdict=reverse))
        # The unchanged V3 filter must still keep concrete process evidence.
        req=v4.windows('Why is the clamp used during drilling?',[
            source('S0','M aligns two boards.',0),source('S1','M clamps the boards.',1),
            source('S2','M drills through the boards.',2),source('S3','A distant radio plays music.',3)])[0]
        value=ask(meter,'q4-filter-context','q3_filter',[req])[0]
        checks.append(dict(name='filter_context',passed=all(v4.decision(value[x])=='keep' for x in ['S1','S2']) and v4.decision(value['S3'])=='drop',verdict=value,
            limitation='S0 alignment classification reported but not a required label; clamp use and subsequent drilling must keep. V3 filter is unchanged.'))
        requests=[v4.windows('Which parts are installed?',[source('S'+str(i),'M installs a latch on panel '+str(i)+'.',i)])[0] for i in range(4)]
        values=ask(meter,'q4-filter-batch4','q3_filter',requests)
        checks.append(dict(name='batch4',passed=all(v4.decision(v['S'+str(i)])=='keep' for i,v in enumerate(values))))
        if not all(c['passed'] for c in checks):raise ValueError('V4 semantic readiness gate failed; formal cohort not started')
    finally:
        write(folder/'functional.json',dict(passed=len(checks)==17 and all(c['passed'] for c in checks),checks=checks,
            cost_summary=ledger.summary(),at=now(),scope='Synthetic development gates, not held-out QA accuracy'))
        meter.close()
    question,archive,original,sources=synthetic_case()
    result=run_case(backend,cfg,question,sources,original,folder/'reader_pair',archive,'adaptive')
    passed=(result['complete'] and result['stop_reason']=='audited_sufficient' and result['final_certificate']['sufficient']
            and [x['scene_id'] for x in result['retrievals']]==['T1'] and all(str(a['answer']['selected_option'])=='0' for a in result['arms'].values()))
    write(folder/'reader_gate.json',dict(passed=passed,result=result,at=now()))
    if not passed:raise ValueError('V4 temporal repair must stop after the useful scene and answer correctly')
