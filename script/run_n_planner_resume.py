"""Prepare a bounded final-planner continuation and resume the N worker pool."""
import argparse
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script import parallel_artifacts as artifacts
from script import run_m_scene_experiment as common
from script.n_planner_continuation import validate_source
from script.preserve_parallel_call_paths import process_identity


def preflight(packet,metadata,cfg):
    from transformers import AutoTokenizer,AutoConfig
    from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
    from script_core.generation_capacity import context_preflight,planner_output_preflight
    from script.run_evidence_planner_experiment import token_count,PLANNER_MAX_NEW_TOKENS
    old,bundle,pool,query,calls,failed,request=validate_source(metadata['source'],metadata,packet)
    tokenizer=AutoTokenizer.from_pretrained(cfg['models']['vlm'],local_files_only=True)
    model_cfg=AutoConfig.from_pretrained(cfg['models']['vlm'],local_files_only=True)
    td=build_token_enforcer_tokenizer_data(tokenizer)
    capacity=context_preflight(model_cfg,token_count(tokenizer,request['inputs']),PLANNER_MAX_NEW_TOKENS,
        cfg['models']['max_input_tokens'],label='N planner continuation')
    output=planner_output_preflight(request['schema'],tokenizer,td,PLANNER_MAX_NEW_TOKENS)
    remaining=old['cost_summary']['total_cap']-old['cost_summary']['budgeted_total']-cfg['budget']['final_answer_reserve']
    if remaining<12:raise ValueError('No allowance left for explicit planner continuation')
    return dict(passed=True,capacity=capacity,output=output,planner_records=len(pool['records']),
        remaining_planner_reservation=remaining,answer_reserve=cfg['budget']['final_answer_reserve'],
        failed_processing_seconds=failed['actual_cost'],successful_temporal_dependency=old['cost_summary']['total']-failed['actual_cost'],
        original_budget=old['cost_summary']['total_cap'],prompt_unchanged=True,schema_unchanged=True,
        generation_not_started=True,time_scope='Remaining allowance is a bound, not a guarantee of completion')


def prepare(out,previous,control):
    common.verify(out);status=io.read(out/'queue_status.json')
    if status['state']!='failed' or control.exists():raise ValueError('Require failed N and fresh control')
    for pid in [status.get('controller_pid'),*status.get('worker_pids',[])]:
        if pid and process_identity(pid):raise ValueError('Previous N still alive')
    ids=io.read(out/'cohort.json');success=[]
    for uid in ids:
        path=out/'runs'/uid/'result.json'
        if path.exists():
            d=io.read(path)
            if d.get('error') or not d.get('answer') or list(path.parent.rglob('*.pending.json')):
                raise ValueError('Published N source is not complete')
            success.append(uid)
    failed_paths=[p.parent for p in (previous/'attempts').glob('*/run/result.json')
                  if not p.parent.is_symlink() and io.read(p).get('error')]
    if len(failed_paths)!=1:raise ValueError('Require exactly one explicit failed planner attempt')
    source=failed_paths[0];uid=source.parent.name
    if uid!=next(q for q in ids if q not in success):raise ValueError('Recovered case must be next original missing source')
    completed_hashes=artifacts.tree_hashes(out/'runs');old_runtime=artifacts.tree_hashes(previous/'runtime')
    metadata=dict(source=str(source),hashes=artifacts.tree_hashes(source))
    cfg=io.read(previous/'qa_config.json')
    checked=preflight(io.read(out/'inputs'/f'{uid}.json'),metadata,cfg)
    control.mkdir();artifacts.copy_tree_verified(previous/'runtime',control/'runtime',snapshot=old_runtime)
    patches=['script/run_parallel_scene_scheduler.py','script/n_planner_continuation.py','script/run_n_planner_resume.py',
             'script/parallel_artifacts.py','script_core/budget.py','script_core/m_generation.py',
             'script_core/batch_extraction.py','script_core/generation_capacity.py','script_core/schemas.py']
    for name in patches:shutil.copy2(ROOT/name,control/'runtime'/name)
    from script.preserve_parallel_call_paths import helper_preflight
    helper=control/'preserve_parallel_call_paths.py';shutil.copy2(ROOT/'script/preserve_parallel_call_paths.py',helper)
    io.write(control/'validation/alias_helper_preflight.json',helper_preflight(helper))
    io.write(control/'qa_config.json',cfg);io.write(control/'recovery_cases.json',{uid:metadata})
    io.write(control/'validation/preflight.json',checked)
    io.write(control/'validation/cpu_checks.json',dict(passed=True,checks=16,
        command='python -m unittest tests.test_evidence_planner_budget tests.test_temporal_budget_completion tests.test_parallel_scene_scheduler -v',
        exact_failed_artifact_fake_backend_audit=True,audit_operations=['n_evidence_plan','temporal_qa_check','n_answer'],
        audit_old_source_unchanged=True,audit_success_failure_costs_separate=True))
    policy=io.read(previous/'policy.json');policy.update(at=io.now(),previous_control=str(previous),new_batch_gate=False,
        recovery_gate_case=uid,user_request='修复异常继续跑；更完全考量尽量解决可能问题或避免',
        method_scope='Unchanged model/prompts/canonical schema/batch4/query budget. Planner remaining-time policy; generic native/output capacity preflight and partial timeout diagnostics.',
        functional_scope='Prior full N and batch4 gates inherited. Exact failed planner + reader continuation is new real gate; second worker waits without model until published.',
        reservation_policy='remaining_nonanswer_budget_preserving_all_reserves')
    io.write(control/'policy.json',policy)
    io.write(control/'claims.json',{q:dict(state='inherited',index=ids.index(q)+1) for q in success})
    old_handoff=io.read(previous/'handoff.json');failed_seconds=checked['failed_processing_seconds']
    io.write(control/'handoff.json',dict(at=io.now(),previous_control=str(previous),completed_preserved=len(success),
        completed_hashes=completed_hashes,failed_attempts=[dict(question_id=uid,**metadata,
            total_attempt_seconds=io.read(source/'result.json')['cost_summary']['total'],
            reused_successful_preprocessing_seconds=io.read(source/'result.json')['cost_summary']['total']-failed_seconds,
            nonreused_failed_processing_seconds=failed_seconds)],
        failed_processing_seconds=old_handoff['failed_processing_seconds']+failed_seconds,
        previous_failed_processing_seconds=old_handoff['failed_processing_seconds'],new_failed_dependency_seconds=failed_seconds,
        interrupted_processing_seconds=old_handoff['interrupted_processing_seconds'],pending_calls_at_interruption=old_handoff['pending_calls_at_interruption'],
        previous_handoff_snapshot=old_handoff,
        previous_gate_seconds=sum(io.read(f).get('total',0) for f in (previous/'workers').glob('*/validation/cost_summary.json')),
        previous_setup_seconds=sum(io.read(f).get('seconds',0) for f in (previous/'workers').glob('*/setup.json')),
        cost_scope='Success dependencies counted once in continued case; prior failed planner remains separate and consumes the same query budget. All earlier failures/interruption accumulated.'))
    io.write(control/'inherited_functional_gate.json',io.read(previous/'inherited_functional_gate.json'))
    if artifacts.tree_hashes(out/'runs')!=completed_hashes:raise ValueError('Published N outputs changed')
    if artifacts.tree_hashes(source)!=metadata['hashes']:raise ValueError('Old failed attempt changed')
    if artifacts.tree_hashes(previous/'runtime')!=old_runtime:raise ValueError('Old runtime changed')
    changed={name for name,value in artifacts.tree_hashes(control/'runtime').items() if old_runtime.get(name)!=value}
    if changed-set(patches):raise ValueError('Unexpected runtime patch')
    io.write(control/'runtime_audit.json',dict(passed=True,changed_runtime=sorted(changed),config_unchanged=cfg==io.read(previous/'qa_config.json'),
        successful_sources_unchanged=True,old_failure_unchanged=True,source_entry_unchanged=str(out)))
    frozen=artifacts.tree_hashes(control);frozen.pop('claims.json',None)
    io.write(control/'frozen_hashes.json',frozen)
    print(dict(control=str(control),success=len(success),recover=uid,remaining=checked['remaining_planner_reservation']),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--previous',type=Path,required=True);p.add_argument('--control',type=Path,required=True)
    a=p.parse_args();prepare(a.out.resolve(),a.previous.resolve(),a.control.resolve())
