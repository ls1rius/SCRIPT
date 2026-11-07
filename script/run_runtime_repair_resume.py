"""Explicit M/N continuation for scalar JSON and complete planner context repairs."""
import argparse
import ast
import json
from pathlib import Path
import shutil
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script import run_m_scene_experiment as common
from script.run_mn_decoder_resume import tree_hashes


def replace_function(old,new,name):
    def span(s):
        node=next(n for n in ast.parse(s).body if isinstance(n,ast.FunctionDef) and n.name==name)
        return node.lineno-1,node.end_lineno
    a,b=span(old);c,d=span(new)
    return ''.join(old.splitlines(True)[:a]+new.splitlines(True)[c:d]+old.splitlines(True)[b:])


def prepare(prior,out):
    common.verify(prior)
    if out.exists() or io.read(prior/'queue_status.json')['state']!='failed':raise ValueError('Fresh directory and stopped failed parent required')
    for f,k in [('launch.json','worker_pid'),('queue_status.json','controller_pid')]:
        pid=io.read(prior/f).get(k)
        if pid and Path(f'/proc/{pid}').exists():raise ValueError('Parent still alive')
    policy=io.read(prior/'experiment.json');variant=policy.get('experiment_label','M')
    ids=io.read(prior/'cohort.json');success=[]
    for uid in ids:
        folder=prior/'runs'/uid;path=folder/'result.json'
        d=io.read(path) if path.exists() else {}
        if d.get('answer') and not d.get('error'):success.append(uid)
        else:break
    failed=ids[len(success)]
    if not (prior/'runs'/failed/'failure.json').exists():raise ValueError('Missing explicit failed query')
    before=tree_hashes(prior);started=time.monotonic();out.mkdir(parents=True)
    for name in ['runtime','inputs','selection_validation']:
        shutil.copytree(prior/name,out/name,ignore=shutil.ignore_patterns('__pycache__'))
    for name in ['cohort.json','evaluation_only.json','qa_expected_artifacts.json','qa_config.json','historical_cost_snapshot.json']:
        if (prior/name).exists():shutil.copy2(prior/name,out/name)
    target=out/'runtime/script_core/backend.py';s=target.read_text()
    s=replace_function(s,(ROOT/'script_core/backend.py').read_text(),'constrained_prefix')
    s=s.replace('MN-navigation-candidate-complete-unicode-v2','MN-navigation-unicode-scalar-v3')
    target.write_text(s)
    # These small modules are shared implementation fixes, not method prompts.
    for name in ['script_core/json_unicode_guard.py','script_core/m_generation.py','script/run_runtime_repair_resume.py','script/gpu_queue.py','script/gpu_shared_queue.py']:
        shutil.copy2(ROOT/name,out/'runtime'/name)
    cfg=io.read(out/'qa_config.json');cfg['models']['max_input_tokens']=32768;io.write(out/'qa_config.json',cfg)
    for uid in success:shutil.copytree(prior/'runs'/uid,out/'runs'/uid)
    arc=out/'archived_attempt';arc.mkdir()
    shutil.copytree(prior/'runs'/failed,arc/'failed_query')
    for name in ['experiment.json','report.json','setup.json','queue_status.json','frozen_hashes.json']:
        shutil.copy2(prior/name,arc/name)
    for name in ['functional','validation']:shutil.copytree(prior/name,arc/name)
    io.write(out/'validation/saved_unicode_call.json',io.read(ROOT/'tests/fixtures/m_sufficiency_invalid_saved_call.json'))
    if variant=='N':io.write(out/'validation/saved_overflow_call.json',io.read(ROOT/'tests/fixtures/n_planner_overflow_saved_call.json'))
    failed_seconds=sum(c['actual_cost'] for c in common.leaf_calls(prior/'runs'/failed))
    overhead=io.read(prior/'setup.json')['seconds']+io.read(prior/'functional/cost_summary.json')['seconds']
    policy.update(created_at=io.now(),prior_attempt=str(prior),runner_entry='script/run_runtime_repair_resume.py',
        navigation_decoder='MN-navigation-unicode-scalar-v3',failed_decoder_operation='m_sufficiency_check' if variant=='M' else 'n_evidence_plan',
        resume_successful_ids=success,resume_question_id=failed,reused_benchmark_answers=len(success),answers_reused=True,
        prior_preparation_seconds=policy.get('prior_preparation_seconds',0.)+policy.get('cpu_preparation_seconds',0.),
        cpu_preparation_seconds=time.monotonic()-started,
        prior_runtime_seconds=policy.get('prior_runtime_seconds',0.)+overhead+failed_seconds,
        prior_failed_query_seconds=policy.get('prior_failed_query_seconds',0.)+failed_seconds,
        continuation_reason='user_requested_shared_runtime_repair',max_input_tokens=32768,
        repair_scope='Scalar-safe JSON escapes and configured whole-prompt capacity, with native context plus output reserve check; no source truncation or semantic prompt changes.',
        functional_gate_policy='Saved Unicode failure; N additionally saved overflow planner; original complete M/N gate; then restart failed query from its beginning')
    policy['resource_policy'].update(admission_mode='memory_shared',preference=['0','1','2','3'],maximum_utilization=100)
    io.write(out/'experiment.json',policy)
    old_runtime=tree_hashes(prior/'runtime')
    changed=[k for k,v in tree_hashes(out/'runtime').items() if old_runtime.get(k)!=v]
    if set(changed)-{'script_core/backend.py','script_core/json_unicode_guard.py','script_core/m_generation.py','script/run_runtime_repair_resume.py','script/gpu_queue.py','script/gpu_shared_queue.py'}:raise ValueError('Unexpected runtime change')
    if before!=tree_hashes(prior):raise ValueError('Parent changed')
    io.write(out/'validation/continuation.json',dict(passed=True,prior_hashes=before,successful_ids=success,restart_question=failed,
        inherited_run_hashes={u:tree_hashes(out/'runs'/u) for u in success},runtime_changed=changed,prior_unchanged=True,
        current_failed_seconds=failed_seconds,old_setup_gate_seconds=overhead,scope='Inherited success counted once; failures/setup archived separately'))
    io.write(out/'frozen_hashes.json',tree_hashes(out));common.verify(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()));common.report(out)
    print(json.dumps(dict(prepared=str(out),variant=variant,inherited=len(success),restart=failed)),flush=True)


def functional(out,backend,cfg):
    from script_core.budget import BudgetLedger,Meter
    from script_core.schemas import output_schema,validate_output
    variant=io.read(out/'experiment.json').get('experiment_label','M')
    ledger=BudgetLedger(600,answer_reserve=0,calls_path=out/'functional/decoder/calls.jsonl')
    meter=Meter(backend,ledger,cfg,out/'functional/decoder/raw_calls');checks=[]
    try:
        saved=io.read(out/'validation/saved_unicode_call.json');req=saved['inputs']
        result,_=meter.json('m_sufficiency_check',req,())
        validate_output(result,output_schema('m_sufficiency_check',req))
        json.dumps(result,ensure_ascii=False).encode('utf8')
        checks.append(dict(operation='m_sufficiency_check',passed=True,result=result))
        if variant=='N':
            req=io.read(out/'validation/saved_overflow_call.json')['inputs']
            result,_=meter.call('n_evidence_plan',req)
            parsed=json.loads(result['raw']);validate_output(parsed,req['schema'])
            checks.append(dict(operation='n_evidence_plan',passed=True,input_tokens=result['usage']['input_tokens'],decisions=len(parsed)))
        io.write(out/'functional/decoder/result.json',dict(passed=True,checks=checks))
    except Exception as exc:
        io.write(out/'validation/functional.json',dict(passed=False,checks=checks,error=str(exc)));raise
    finally:
        io.write(out/'functional/decoder/cost_summary.json',ledger.summary());meter.close()
    if variant=='N':
        from script.run_n_scene_experiment import functional as full
    else:full=common.functional
    gate=full(out,backend,cfg);gate['shared_runtime_repair_checks']=checks
    io.write(out/'validation/functional.json',gate);return gate


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','queue','worker','report']);p.add_argument('--out',type=Path,required=True);p.add_argument('--prior',type=Path)
    a=p.parse_args();out=a.out.resolve()
    if a.mode=='prepare':prepare(a.prior.resolve(),out)
    elif a.mode=='queue':common.queue(out)
    elif a.mode=='worker':common.worker(out,functional_gate=functional)
    else:common.report(out)
