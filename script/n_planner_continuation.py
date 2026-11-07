"""Explicit, hash-bound continuation after a failed final evidence planner.

Successful navigation and extraction are copied byte-for-byte. Failed planner
time remains a separate dependency charged against the original query budget.
No model call is retried automatically and no partial planner output is accepted.
"""
from copy import deepcopy
import json
from pathlib import Path
import time

from script import controlled_memory_io as io
from script.parallel_artifacts import tree_hashes,copy_tree_verified
from script_core.budget import BudgetLedger,Meter


class ContinuationLedger(BudgetLedger):
    def __init__(self,total,successful,failed,answer_reserve,calls_path):
        super().__init__(total,initialization_cost=successful,answer_reserve=answer_reserve,calls_path=calls_path)
        self.dependency_cost=failed
        self.failed_dependency=failed
    def summary(self):
        result=super().summary()
        result.update(shared_dependency_seconds=0,
            successful_preprocessing_dependency_seconds=self.initialization_cost,
            historical_failed_dependency_seconds=self.failed_dependency,
            new_processing_seconds=self.actual,
            accounting_scope='Successful total excludes historical failure; budgeted_total includes it once')
        return result


def validate_source(source,metadata,packet):
    from script_core import n_scene_memory as n
    from script_core.config import digest
    source=Path(source)
    if tree_hashes(source)!=metadata['hashes']:raise ValueError('Continuation source changed')
    result=io.read(source/'partial_temporal/result.json')
    if not result.get('error') or result.get('answer'):raise ValueError('Require failed unfinished temporal stage')
    if io.read(source/'query.json')['id']!=packet['question']['question_id']:
        raise ValueError('Continuation question mismatch')
    bundle=io.read(source/'partial_search/evidence_bundle.json')
    if bundle['packet_hash']!=digest(packet):raise ValueError('Continuation packet mismatch')
    pool=io.read(source/'partial_temporal/N/pool.json')
    query=dict(packet['question'],id=packet['question']['question_id'])
    inputs=n.inherited.parent.planner_input(query,pool,query['id'])
    if inputs!=io.read(source/'partial_temporal/N/planner_input.json'):
        raise ValueError('Continuation planner input differs from frozen source')
    calls=[json.loads(line) for line in (source/'partial_temporal/calls.jsonl').read_text().splitlines() if line]
    failed=[row for row in calls if row.get('error')]
    if len(failed)!=1 or failed[0]['operation']!='n_evidence_plan':
        raise ValueError('Only a single final failed planner may be continued')
    if any(not row.get('parameters',{}).get('in_process') for row in calls[calls.index(failed[0])+1:]):
        raise ValueError('A model call followed the failed planner')
    request=dict(inputs=inputs,schema=n.inherited.parent.plan_schema(pool))
    if failed[0]['input_hash']!=digest(request):raise ValueError('Failed planner request hash differs')
    if list(source.rglob('*.pending.json')):raise ValueError('Pending call requires explicit resolution')
    return result,bundle,pool,query,calls,failed[0],request


def run(out,control,backend,cfg,uid,attempt):
    from script_core import n_scene_memory as n,partial_temporal as temporal
    from script_core.temporal_pipeline import qa_context_inputs
    from script.run_followup_validation import direct_input
    began=time.monotonic();cpu_began=time.process_time()
    metadata=io.read(control/'recovery_cases.json')[uid]
    packet=io.read(out/'inputs'/f'{uid}.json');source=Path(metadata['source'])
    old,bundle,pool,query,calls,failed,request=validate_source(source,metadata,packet)
    failed_seconds=failed['actual_cost']
    successful=old['cost_summary']['total']-failed_seconds
    total=old['cost_summary']['total_cap']
    if total!=cfg['budget']['online_cap']:raise ValueError('Continuation original query cap changed')
    copy_tree_verified(source,attempt,snapshot=metadata['hashes'])
    target=attempt/'partial_temporal';planner_target=target/'N'
    io.write(attempt/'inherited_attempt_result.json',io.read(attempt/'result.json'))
    io.write(target/'inherited_attempt_result.json',old)
    io.write(attempt/'continuation.json',dict(source=str(source),question_id=uid,
        successful_preprocessing_seconds=successful,historical_failed_planner_seconds=failed_seconds,
        original_temporal_budget=total,answer_reserve=cfg['budget']['final_answer_reserve'],
        unchanged_source_hashes=metadata['hashes'],failed_call=failed,
        policy='Only incomplete planner/reader rerun; original failed call charged against same budget'))
    (target/'calls.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in calls if not row.get('error')))
    ledger=ContinuationLedger(total,successful,failed_seconds,cfg['budget']['final_answer_reserve'],target/'calls.jsonl')
    ledger.record('continuation_source_validation_copy',dict(in_process=True),ledger.remaining,began,
        output=dict(usage=dict(cpu_seconds=time.process_time()-cpu_began,gpu_resident_seconds=0)))
    meter=Meter(backend,ledger,cfg,target/'raw_calls')
    from script_core.accounting import cpu_phase
    phase=cpu_phase('n_planner_continuation_materialization');phase.__enter__()
    result=deepcopy(old);result.update(answer=None,error=None)
    try:
        memory=io.read(target/'network.json')
        view=temporal.context(memory,bundle['memory'],query)
        baseline=qa_context_inputs(query,packet['captions'],bundle['selected_seconds'],view,
            visual_observations=bundle['supplemental_visual']['observations'])
        if direct_input(baseline)!=direct_input(bundle['qa_input']):raise ValueError('Recovered original QA changed')
        # Token/output/context preflight is performed by the same live generator
        # before generate(); the request is exactly the failed frozen request.
        output,_=meter.call('n_evidence_plan',request)
        io.write(planner_target/'planner_output.json',output)
        proposed=n.inherited.parent.decode_plan(output['raw'],pool)
        plan,overrides=n.preserve_incomplete_sources(pool,proposed)
        description,audit=n.inherited.render(pool,plan)
        audit['source_preservation_overrides']=overrides
        qa=n.inherited.parent.final_input(baseline,description,io.parts)
        io.write(planner_target/'proposed_plan.json',proposed)
        io.write(planner_target/'source_preservation.json',dict(overrides=overrides,
            rule='Retained partial H facts require their available original source; all-skipped sources stay skipped.'))
        io.write(planner_target/'plan.json',plan);io.write(planner_target/'audit.json',audit)
        io.write(planner_target/'final_input.json',qa)
        entity_audit=io.read(target/'entity_evidence_audit.json')
        entity_audit.update(variant='N',parent_memory_variant='I',evidence_planner=dict(
            version=n.VERSION,parent='M',parent_version=n.PARENT_VERSION,pool_records=len(pool['records']),audit=deepcopy(audit)))
        io.write(target/'entity_evidence_audit.json',entity_audit)
        checked,_=meter.call('temporal_qa_check',qa,final=True)
        io.write(target/'qa_context.json',dict(context=view,input_tokens=checked['input_tokens'],
            selected_seconds=bundle['selected_seconds'],evidence_selection=entity_audit,
            final_memory=io.read(target/'generalized_lazy_memory.json')))
        answer,_=meter.call('n_answer',dict(inputs=qa,options=packet['question']['options']),final=True)
        result.update(answer=answer['data'],qa_input_tokens=checked['input_tokens'],memory_variant='N',
            qa_evidence={key:value for key,value in entity_audit.items() if key!='quotes'})
        if entity_audit['failed_source_fields']:
            result['incomplete'].append(dict(stage='memory_extraction',reason='Failed fields retain complete original sources',
                                           failed_source_fields=entity_audit['failed_source_fields']))
    except Exception as exc:
        result['error']=dict(type=type(exc).__name__,message=str(exc))
    finally:
        phase.__exit__(None,None,None)
        result.update(cost_summary=ledger.summary(),incremental_processing_seconds=successful+ledger.actual-bundle['preparation_seconds'],
            continuation_source=str(source),historical_failed_processing_seconds=failed_seconds)
        io.write(target/'result.json',result);meter.close()
    # Reuse the normal scheme wrapper for root provenance and directory cost,
    # without invoking navigation, extraction or any other model call again.
    from script_core.scheme4 import answer as wrap
    return wrap(packet,[],cfg,backend,attempt,resume=True)
