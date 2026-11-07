"""Generic regression and held-out contract probes. Never read benchmark labels or case artifacts."""
from script import controlled_memory_io as io
from script_core import p_sentence_filter as p,o_sentence_filter as o
from script_core.budget import BudgetLedger,Meter


def units(**fields):
    return [dict(id=f'U{i}',kind='action_field',group='actions_0',field=k,value=v) for i,(k,v) in enumerate(fields.items())]


def run(out,backend,cfg):
    from script.run_p_sentence_experiment import batch
    target=out/'repair_validation';ledger=BudgetLedger(600,answer_reserve=0,calls_path=target/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,target/'raw_calls')
    gate=dict(passed=False,technical_passed=False,checks=[],error=None,scope='Synthetic probes absent from prompts; no benchmark labels; not broad semantic validation')
    sources=[dict(id=f'S{i}',text=t) for i,t in enumerate([
        'Technician Q7 slides a tray toward a cabinet.',
        'Courier R4 does not open the parcel.',
        'Worker T2 leans backward.',
        'Visitor V8 waits beside the doorway.',
        'Mechanic H6 fastens a latch with a wrench.',
        'Operator J5 does not lift the carton.',
        'Runner K8 steps sideways.',
        'Visitor L3 pauses near the gate.',
        'Painter S2 marks a board with a pencil.',
        'Pupil W3 does not shut the window.',
        'Pilot Y9 turns slowly.',
        'Guard Z4 stands behind the desk.',
        'Nurse B6 waits outside the clinic.',
        'Driver C8 moves diagonally.',
        'Assistant D2 carries a basket to the counter.',
        'Engineer F3 does not loosen the bolt with a spanner.',
        'Performer G1 bows gracefully.',
        'Clerk H2 waits inside the office.',
        'Porter J7 pushes a cart toward the elevator.',
        'Artisan K4 cuts the fabric with scissors.',
        'Dancer M5 pivots clockwise.',
        'Resident P8 sits beyond the fence.',
        'Attendant R6 carries the suitcase towards the exit.',
        'Gardener S9 trims a branch using shears.',
        'Rider U3 sways gently.',
        'Passenger V2 rests beneath the canopy.',
        'Handler W8 rolls a drum towards the ramp.',
        'Tailor X6 measures the sleeve with a tape.',
        '#OBS The scientist R21 measures a crystal.',
        '#TAG A surveyor M34 maps the valley.',
        '#META The courier J19 does not open the case.',
        '#INFO The gardener P28 waters the shrub.',
        'The scientist R21 measures a crystal.',
        'A surveyor M34 maps the valley.',
        'The courier J19 does not open the case.',
        'The gardener P28 waters the shrub.'])]
    specs=[dict(identity='Q7',roles=dict(patient='tray',destination='cabinet')),
        dict(identity='R4',negated=True),dict(identity='T2',direction='backward'),dict(identity='V8',roles=dict(location='doorway')),
        dict(identity='H6',roles=dict(patient='latch',instrument='wrench')),dict(identity='J5',negated=True),
        dict(identity='K8',direction='sideways'),dict(identity='L3',roles=dict(location='gate')),
        dict(identity='S2',roles=dict(patient='board',instrument='pencil')),dict(identity='W3',negated=True),
        dict(identity='Y9',roles=dict(manner='slowly')),dict(identity='Z4',roles=dict(location='desk')),
        dict(identity='B6',roles=dict(location='clinic')),dict(identity='C8',direction='diagonally'),
        dict(identity='D2',roles=dict(patient='basket',destination='counter')),
        dict(identity='F3',roles=dict(patient='bolt',instrument='spanner'),negated=True),
        dict(identity='G1',roles=dict(manner='gracefully')),dict(identity='H2',roles=dict(location='office')),
        dict(identity='J7',roles=dict(patient='cart',destination='elevator')),dict(identity='K4',roles=dict(patient='fabric',instrument='scissors')),
        dict(identity='M5',roles=dict(manner='clockwise')),dict(identity='P8',roles=dict(location='fence')),
        dict(identity='R6',roles=dict(patient='suitcase',destination='exit')),dict(identity='S9',roles=dict(patient='branch',instrument='shears')),
        dict(identity='U3',roles=dict(manner='gently')),dict(identity='V2',roles=dict(location='canopy')),
        dict(identity='W8',roles=dict(patient='drum',destination='ramp')),dict(identity='X6',roles=dict(patient='sleeve',instrument='tape'))]
    specs.extend([dict(identity=identity,actor=actor,verb=verb) for _ in range(2)
        for identity,actor,verb in [('R21','The scientist R21','measures'),('M34','A surveyor M34','maps'),
                                  ('J19','The courier J19','does not open'),('P28','The gardener P28','waters')]])
    try:
        replay_path=out/'validation/extraction_probe_replay.json'
        if replay_path.exists():
            from script_core.o_batch_generation import messages_for
            replay=io.read(replay_path)
            if replay['sources']!=sources:raise ValueError('Probe replay sources changed')
            rows=replay['rows']
            if len(rows)!=len(sources):raise ValueError('Probe replay row count changed')
            for source,row in zip(sources,rows):
                if row['prompt']!=messages_for(p.EXTRACT,p.extraction_input(source))[0]:raise ValueError('Probe replay extraction messages changed')
                if row.get('parse_error') or row['data']!=p.normalize_extraction(row['wire_data']):raise ValueError('Invalid probe replay')
            ledger.dependency_cost=replay['extraction_seconds']
            gate['extraction_replay']=dict(parent=replay['parent'],source_only_rows=len(rows),identical_prompts_and_mapping=True,inherited_seconds=replay['extraction_seconds'])
        else:
            rows=[]
            for start in range(0,len(sources),4):
                rows.extend(batch(meter,'P_generic_extract_probe',p.EXTRACT,[(s['id'],p.extraction_input(s)) for s in sources[start:start+4]]))
        for i,(s,row) in enumerate(zip(sources,rows)):
            if row.get('parse_error') or row.get('data') is None:
                gate['checks'].append(dict(name='source_roles_'+str(i),held_out_new=i>=24,passed=False,input=s,output=row.get('data'),raw=row.get('raw'),parse_error=row.get('parse_error') or 'missing data'))
                continue
            o.validate_output(row['data'],p.extraction_schema());acts=row['data']['actions']
            actors=[a['actor'] or '' for a in acts];verbs=[a['verb'].lower() for a in acts]
            spec=specs[i];expected_id=spec['identity']
            identity=any(expected_id in a for a in actors)
            verbatim=all(a.casefold() in s['text'].casefold() for a in actors if a) and all(v.casefold() in s['text'].casefold() for v in verbs)
            predicates=all(v.strip() not in ['toward','beside','backward','sideways','near','in','on','with'] for v in verbs)
            title_not_patient=not any((a.get('actor') and a['roles'].get('patient') in [a['actor'],'Technician','Courier','Worker','Visitor','Mechanic','Operator','Runner']) for a in acts)
            specific=bool(acts)
            if spec.get('roles'):
                specific=any(all(value in (a['roles'].get(key) or '') for key,value in spec['roles'].items()) for a in acts)
            if spec.get('negated'):specific=specific and any('not' in v for v in verbs)
            if spec.get('actor'):specific=specific and any(a['actor']==spec['actor'] and a['verb']==spec['verb'] for a in acts)
            if spec.get('direction'):
                specific=any(spec['direction'] in (a['roles'].get('manner') or '') for a in acts) and all(spec['direction'] not in (a['roles'].get('body_part') or '') for a in acts)
            # Intransitive probes also forbid invented objects/body parts; old checks retained.
            absent_ok=not any(a['roles'].get(k) is not None for a in acts for k in ('patient','body_part','destination')) if i in (2,3,6,7,10,11,12,13,16,17,20,21,24,25) else True
            gate['checks'].append(dict(name='source_roles_'+str(i),held_out_new=i>=24,absent_roles=absent_ok,verbatim_subject_predicate=verbatim,passed=verbatim and identity and predicates and title_not_patient and specific and absent_ok,input=s,output=row['data'],identity=identity,predicates=predicates,title_not_patient=title_not_patient,roles=specific))
        q=dict(question='Which actions does Technician Q7 perform while moving a tray?')
        checks=[
            ('Technician Q7 slides a tray toward a cabinet.',units(actor='Technician Q7',verb='slides',patient='a tray',destination='a cabinet'),'keep'),
            ('Courier R4 opens a parcel.',units(actor='Courier R4',verb='opens',patient='a parcel'),'drop'),
            ('Technician Q7 slides a tray backward.',units(actor='Technician Q7',verb='slides',patient='a tray',body_part='backward'),'invalid_role'),
            ('The isolated sign is oval.',[dict(id='U0',kind='attributes',group='attributes_0',record=dict(entity='sign',property='shape',value='oval'))],'drop')]
        requests=[(f'S{i}',p.filter_input(q,dict(text=text),u)) for i,(text,u,_) in enumerate(checks)]
        rows=batch(meter,'P_generic_filter_probe',p.FILTER,requests)
        for i,((_,req),row,(_,_,expect)) in enumerate(zip(requests,rows,checks)):
            judgments,labels,selected=p.selection(req,row['data'])
            if expect=='keep':passed=all(labels.values())
            elif expect=='drop':passed=not any(labels.values())
            else:passed=judgments['U3']=='drop_unfaithful' and all(labels[u]==1 for u in ['U0','U1','U2'])
            gate['checks'].append(dict(name='filter_decision_'+str(i),passed=passed,input=req,output=row['data'],labels=labels))
        extra_q=dict(question='What actions and spatial details are observed?')
        extra=[
            ('An observer walks toward an arch.',units(actor='An observer',verb='walks',destination='an arch'),'keep'),
            ('A pilot waits under an awning.',units(actor='A pilot',verb='waits',location='an awning'),'keep'),
            ('A technician turns a knob with a key.',units(actor='A technician',verb='turns',patient='a knob',instrument='a key'),'keep'),
            ('A traveler moves diagonally.',units(actor='A traveler',verb='moves',manner='diagonally',body_part='diagonally'),'invalid_role')]
        extra_requests=[(f'S{i}',p.filter_input(extra_q,dict(text=text),u)) for i,(text,u,_) in enumerate(extra)]
        extra_rows=batch(meter,'P_new_generic_filter_probe',p.FILTER,extra_requests)
        for i,((_,req),row,(_,_,expect)) in enumerate(zip(extra_requests,extra_rows,extra)):
            judgments,labels,_=p.selection(req,row['data'])
            passed=all(labels.values()) if expect=='keep' else judgments['U3']=='drop_unfaithful' and all(labels[u] for u in ['U0','U1','U2'])
            gate['checks'].append(dict(name='new_filter_decision_'+str(i),held_out_new=True,passed=passed,input=req,output=row['data'],labels=labels))
        identity_checks=[
            ('The scientist R21 measures a crystal.',units(actor='The scientist',verb='measures',patient='a crystal'),'bad_actor'),
            ('The scientist R21 measures a crystal.',units(actor='The scientist R21',verb='measures',patient='a crystal'),'keep'),
            ('#TAG A surveyor M34 maps the valley.',units(actor='M34 M34',verb='maps',patient='the valley'),'bad_actor'),
            ('#TAG A surveyor M34 maps the valley.',units(actor='A surveyor M34',verb='maps',patient='the valley'),'keep')]
        identity_requests=[(f'S{i}',p.filter_input(extra_q,dict(text=text),u)) for i,(text,u,_) in enumerate(identity_checks)]
        identity_rows=batch(meter,'P_identity_integrity_probe',p.FILTER,identity_requests)
        for i,((_,req),row,(_,_,expect)) in enumerate(zip(identity_requests,identity_rows,identity_checks)):
            judgments,labels,_=p.selection(req,row['data'])
            passed=all(labels.values()) if expect=='keep' else judgments['U0']=='drop_unfaithful' and all(labels[u] for u in ['U1','U2'])
            gate['checks'].append(dict(name='identity_filter_'+str(i),held_out_new=True,passed=passed,input=req,output=row['data']))
        witness_checks=[
            ('The curator T47 lifts a vessel.',units(actor='The curator T47',verb='lifts',patient='a vessel'),'keep'),
            ('The geologist N63 examines a pebble.',units(actor='The geologist',verb='examines',patient='a pebble'),'bad_actor'),
            ('The analyst H52 carries a folder.',units(actor='H52 H52',verb='carries',patient='a folder'),'bad_actor'),
            ('A rover Y26 moves sideways.',units(actor='A rover Y26',verb='moves',manner='sideways',body_part='sideways'),'bad_role')]
        witness_requests=[(f'S{i}',p.filter_input(extra_q,dict(text=text),u)) for i,(text,u,_) in enumerate(witness_checks)]
        witness_rows=batch(meter,'P_new_blind_witness_probe',p.FILTER,witness_requests)
        for i,((_,req),row,(_,_,expect)) in enumerate(zip(witness_requests,witness_rows,witness_checks)):
            judgments,labels,_=p.selection(req,row['data'])
            if expect=='keep':passed=all(labels.values())
            else:
                bad='U0' if expect=='bad_actor' else 'U3'
                passed=judgments[bad]=='drop_unfaithful' and all(v for k,v in labels.items() if k!=bad)
            gate['checks'].append(dict(name='blind_witness_filter_'+str(i),held_out_new=True,passed=passed,input=req,output=row['data']))
        name_checks=[
            ('Professor Ada examines a map.',units(actor='Professor Ada',verb='examines',patient='a map'),'keep'),
            ('Professor Ada examines a map.',units(actor='Professor',verb='examines',patient='a map'),'bad_actor'),
            ('Captain Noor steers a vessel.',units(actor='Noor Noor',verb='steers',patient='a vessel'),'bad_actor'),
            ('Captain Noor steers a vessel.',units(actor='Captain Noor',verb='steers',patient='a vessel'),'keep')]
        name_requests=[(f'S{i}',p.filter_input(extra_q,dict(text=text),u)) for i,(text,u,_) in enumerate(name_checks)]
        name_rows=batch(meter,'P_new_nominal_names_probe',p.FILTER,name_requests)
        for i,((_,req),row,(_,_,expect)) in enumerate(zip(name_requests,name_rows,name_checks)):
            judgments,labels,_=p.selection(req,row['data'])
            passed=all(labels.values()) if expect=='keep' else judgments['U0']=='drop_unfaithful' and all(labels[k] for k in ['U1','U2'])
            gate['checks'].append(dict(name='nominal_name_filter_'+str(i),held_out_new=True,passed=passed,input=req,output=row['data']))
        # Reader regressions use fabricated evidence/answers, never benchmark labels.
        reader_probes=[
            (dict(question='What overall work is being performed?',options=[
                'Packing boxes for shipping.','Painting the equipment.','Testing an electrical circuit.',
                'Preparing a meal.','Repairing a bicycle.']),[
                dict(actor='Operator L8',verb='counts',patient='boxes'),
                dict(actor='Operator L8',verb='measures',patient='circuit resistance',instrument='a meter'),
                dict(actor='Operator L8',verb='records',patient='circuit voltage')],2),
            (dict(question='What does Observer L8 do?',options=[
                'Opens a drawer.','Writes on a pad.','Closes a window.','Inspects a specimen.','Switches off a lamp.']),[
                dict(actor='Observer Q6',verb='opens',patient='a drawer'),
                dict(actor='Observer L8',verb='inspects',patient='a specimen')],3)]
        for i,(question,facts,expected) in enumerate(reader_probes):
            observations=[dict(source_id=f'S{j}',intervals=[[j,j+1]],information=[dict(kind='action',event_id='actions_0',**fact)]) for j,fact in enumerate(facts)]
            req=p.review_input(observations,question)
            row=batch(meter,'P_reader_probe_'+str(i),p.REVIEW,[('reader',req)])[0]
            final=p.final_input({},observations,question,review=row['data'])
            # This gate has no reserved final-answer phase; both synthetic reader
            # calls are ordinary diagnostic costs within its shared 600s budget.
            answer,_=meter.call(p.ANSWER,dict(inputs=final,options=question['options']))
            gate['checks'].append(dict(name='reader_evidence_'+str(i),held_out_new=True,
                passed=str(answer['data']['selected_option'])==str(expected),input=req,review=row['data'],
                answer=answer['data'],expected_synthetic_option=expected))
        gate['technical_passed']=not any(c.get('parse_error') for c in gate['checks']);gate['passed']=all(c['passed'] for c in gate['checks'])
        if not gate['passed']:raise ValueError('Generic P semantic probes failed; records retained, no automatic retry')
    except Exception as exc:
        gate['error']=dict(type=type(exc).__name__,message=str(exc));raise
    finally:
        gate['cost_summary']=ledger.summary();gate['at']=io.now()
        io.write(out/'validation/repair_functional.json',gate);meter.close()
