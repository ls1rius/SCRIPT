"""New measured N batch2/batch4 and empty-string decoder execution gates."""
from types import SimpleNamespace
from script import controlled_memory_io as io


def run(folder,backend,cfg,control):
    from script_core.budget import BudgetLedger,Meter
    from script_core import n_source_extraction as memory
    from script_core import backend as backend_module,context,prompts,schemas
    from script_core.batch_extraction import inspect_output,digest
    from script_core import m_scene_memory
    runtime=SimpleNamespace(memory=memory,backend=backend_module,context=context,
                            prompts=prompts,schemas=schemas)
    texts=['A technician opens a red toolbox.', 'A gardener cuts a green stem with scissors.',
           'A courier puts a parcel on a shelf.', 'A musician lifts a black case with both hands.']
    requests=[memory.extraction_inputs(dict(id=f'Probe{i}',text=text),
        ['actions.base','actions.instrument','actions.body_part','actions.location']) for i,text in enumerate(texts)]
    directory=folder/'validation'
    ledger=BudgetLedger(600,answer_reserve=0,calls_path=directory/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,directory/'raw_calls');checks=[];rows={};seconds={}
    try:
        for size in [2,4]:
            all_rows=[];began=ledger.actual
            for offset in range(0,len(requests),size):
                inputs=dict(requests=requests[offset:offset+size])
                output,_=meter.call('n_extract_batch',inputs)
                if len(output['items'])!=len(inputs['requests']):raise ValueError('Batch row count changed')
                for request, result in zip(inputs['requests'],output['items']):
                    if result.get('id_map')!={'ID0':request['unit']['id']}:
                        raise ValueError('Batch source mapping changed')
                    inspection=inspect_output(request,result,runtime)
                    if inspection['error']:raise ValueError(inspection['error'])
                    all_rows.append(dict(request=request,input_hash=digest(request),
                                         output=result,inspection=inspection))
            rows[str(size)]=all_rows;seconds[str(size)]=ledger.actual-began
            checks.append(dict(case=f'batch{size}_four_sources_schema_identity',passed=True,rows=len(all_rows)))
        exact=sum(a['inspection']['data']==b['inspection']['data'] for a,b in zip(rows['2'],rows['4']))
        same_tokens=all(a['output']['usage']['input_tokens']==b['output']['usage']['input_tokens']
                       for a,b in zip(rows['2'],rows['4']))
        if not same_tokens:raise ValueError('Batch size changed per-row input tokens')
        checks.append(dict(case='same_inputs_and_input_tokens',passed=True))
        saved=io.read(control/'validation/m_empty_gap_false_eos.json')
        result,_=meter.json('m_transition_next',saved['inputs'],())
        schemas.validate_output(result,schemas.output_schema('m_transition_next',saved['inputs']))
        m_scene_memory.validate_step(result,saved['inputs'])
        checks.append(dict(case='saved_empty_string_navigation',passed=True,result=result,
                           source='M failed call; a shared decoder regression, not an N benchmark answer'))
        io.write(directory/'result.json',dict(passed=True,checks=checks,rows=rows,seconds=seconds,
            exact_output_matches=exact,compared_sources=4,speed_ratio_batch2_over_batch4=seconds['2']/seconds['4'],
            performance_scope='Co-resident, uncontrolled timing; batch arithmetic may change model outputs; no QA improvement claim',
            inherited_gate_scope='Original full N gate retained as historical; only these execution gates are new'))
    except Exception as exc:
        io.write(directory/'result.json',dict(passed=False,checks=checks,rows=rows,seconds=seconds,error=str(exc)))
        raise
    finally:
        io.write(directory/'cost_summary.json',ledger.summary());meter.close()
