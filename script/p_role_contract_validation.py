"""Fresh format/role-boundary checks for P's complete role-slot contract."""
from script import controlled_memory_io as io
from script_core import p_sentence_filter as p
from script_core.budget import BudgetLedger,Meter


def run(out,backend,cfg):
    from script.run_p_sentence_experiment import batch
    policy=io.read(out/'experiment.json');target=out/'role_contract_validation'
    ledger=BudgetLedger(600,answer_reserve=0,calls_path=target/'calls.jsonl');meter=Meter(backend,ledger,cfg,target/'raw_calls')
    gate=dict(passed=False,checks=[],scope='Format prevention and source-role diagnostics, not QA accuracy or broad generalization',error=None)
    actual=io.read(out/'validation/failed_extraction_input.json')['request']
    sources=[('original_failed_input',actual['sentence']),
        ('four_events','Observer A1 waits. Operator B2 bows. Visitor C3 smiles. Courier D4 rests.'),
        ('no_arguments','Visitor F8 waits.'),
        ('tool_and_body','Technician R8 turns a bolt using a wrench with the left hand.'),
        ('coordinated_object','Assistant K9 carries a jar and a lid.'),
        ('agentless_passive','A parcel is moved toward a doorway.'),
        ('unicode_quotes','Observer Ω7 carries a box labelled "研究".'),
        ('negation_direction','Operator T5 does not push a trolley backward.')]
    try:
        rows=[]
        for start in range(0,len(sources),4):
            rows.extend(batch(meter,'P_role_contract_probe',p.EXTRACT,[(sid,dict(sentence=text)) for sid,text in sources[start:start+4]]))
        for (sid,text),row in zip(sources,rows):
            ok=not row.get('parse_error') and row.get('ended_normally') and row.get('data') is not None
            if ok:
                normalized=p.normalize_extraction(row['wire_data']);ok=normalized==row['data']
                if sid=='four_events':ok=ok and len(normalized['actions'])==4
                if sid=='no_arguments':ok=ok and all(not a['roles'] for a in normalized['actions'])
            gate['checks'].append(dict(name=sid,passed=bool(ok),input=text,output=row.get('data'),parse_error=row.get('parse_error'),usage=row.get('usage')))
        # Wrong role assignments must still pass through blind source witnessing.
        source='A mechanic V9 tightens a bolt with a spanner using the right hand.'
        question=dict(question='What actions does the mechanic perform?')
        units=[dict(id='U0',kind='action_field',group='actions_0',field='verb',value='tightens'),
               dict(id='U1',kind='action_field',group='actions_0',field='instrument',value='the right hand'),
               dict(id='U2',kind='action_field',group='actions_0',field='instrument',value='a spanner')]
        req=p.filter_input(question,dict(text=source),units)
        result=batch(meter,'P_role_boundary_probe',p.FILTER,[('S0',req)])[0]
        judgments,labels,_=p.selection(req,result['data'])
        gate['checks'].append(dict(name='mistyped_anatomy_not_restored',passed=judgments['U1']=='drop_unfaithful' and labels['U2']==1,input=req,output=result['data']))
        if not all(c['passed'] for c in gate['checks']):raise ValueError('P complete-role contract validation failed')
        gate['passed']=True
    except Exception as exc:
        gate['error']=dict(type=type(exc).__name__,message=str(exc));raise
    finally:
        gate['cost_summary']=ledger.summary();gate['at']=io.now();io.write(out/'validation/role_contract_functional.json',gate);meter.close()
