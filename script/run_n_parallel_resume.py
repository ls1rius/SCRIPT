"""Explicit N restart with four independent source rows and preserved failures."""
import argparse
from copy import deepcopy
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script import parallel_artifacts as artifacts
from script import run_m_scene_experiment as common
from script.preserve_parallel_call_paths import process_identity


def prepare(out,previous,control):
    common.verify(out)
    status=io.read(out/'queue_status.json')
    if status['state']!='failed' or control.exists():raise ValueError('Require failed N and new control directory')
    for pid in [status.get('controller_pid'),*status.get('worker_pids',[])]:
        if pid and process_identity(pid):raise ValueError('Previous N process still alive')
    ids=io.read(out/'cohort.json');success=[]
    for uid in ids:
        path=out/'runs'/uid/'result.json'
        if not path.exists():continue
        result=io.read(path)
        if result.get('error') or not result.get('answer'):raise ValueError('Published N output is incomplete')
        if list(path.parent.rglob('*.pending.json')):raise ValueError('Published N output has pending calls')
        success.append(uid)
    completed_hashes=artifacts.tree_hashes(out/'runs')
    old_runtime=artifacts.tree_hashes(previous/'runtime')
    failed=[];failed_seconds=0
    for path in (previous/'attempts').glob('*/run/result.json'):
        if path.parent.is_symlink():continue
        result=io.read(path)
        if result.get('error'):
            calls=common.leaf_calls(path.parent)
            seconds=sum(c['actual_cost'] for c in calls);failed_seconds+=seconds
            failed.append(dict(question_id=path.parent.parent.name,path=str(path.parent),
                hashes=artifacts.tree_hashes(path.parent),error=result['error'],
                measured_processing_seconds=seconds,pending_calls=len(list(path.parent.rglob('*.pending.json')))))
    if not failed:raise ValueError('Require retained explicit failed attempt')
    control.mkdir()
    artifacts.copy_tree_verified(previous/'runtime',control/'runtime',snapshot=old_runtime)
    patches=['script/run_parallel_scene_scheduler.py','script/run_n_parallel_validation.py',
             'script/run_n_parallel_resume.py','script/parallel_artifacts.py','script_core/schemas.py']
    for name in patches:shutil.copy2(ROOT/name,control/'runtime'/name)
    from script.preserve_parallel_call_paths import helper_preflight
    helper=control/'preserve_parallel_call_paths.py';shutil.copy2(ROOT/'script/preserve_parallel_call_paths.py',helper)
    io.write(control/'alias_helper_preflight.json',helper_preflight(helper))
    shutil.copy2(ROOT/'tests/fixtures/m_empty_gap_false_eos.json',control/'empty_gap_fixture.json')
    (control/'validation').mkdir()
    shutil.move(control/'empty_gap_fixture.json',control/'validation/m_empty_gap_false_eos.json')
    cfg=io.read(out/'qa_config.json');new_cfg=deepcopy(cfg)
    new_cfg['query']['field_extraction_batch_size']=4
    io.write(control/'qa_config.json',new_cfg)
    policy=io.read(previous/'policy.json')
    policy.update(at=io.now(),previous_control=str(previous),field_extraction_batch_size=4,
        new_batch_gate=True,user_request='修复一下继续跑；尽量减少串行预处理',
        execution_change='Per-question source extraction rows increase 2 to 4, still independent tensors and source-only prompts; two independent question workers',
        method_scope='Canonical schemas/prompts/navigation/budgets unchanged; equivalent empty-string decoder normalization shared with M repair',
        functional_scope='Original full N gate historical; new batch2/batch4 isolation and saved empty-string call gates on each worker before new questions')
    io.write(control/'policy.json',policy)
    io.write(control/'claims.json',{uid:dict(state='inherited',index=ids.index(uid)+1) for uid in success})
    old_handoff=io.read(previous/'handoff.json')
    io.write(control/'handoff.json',dict(at=io.now(),previous_control=str(previous),
        completed_preserved=len(success),completed_hashes=completed_hashes,failed_attempts=failed,
        failed_processing_seconds=failed_seconds,
        interrupted_processing_seconds=old_handoff['interrupted_processing_seconds'],
        pending_calls_at_interruption=old_handoff['pending_calls_at_interruption'],
        cost_scope='Successful published calls counted once; old failure and scheduling interruption separately retained, new failed question restarted explicitly under user authorization',
        previous_setup_seconds=sum(io.read(f).get('seconds',0) for f in (previous/'workers').glob('*/setup.json'))))
    io.write(control/'inherited_functional_gate.json',io.read(previous/'inherited_functional_gate.json'))
    changed={name for name,value in artifacts.tree_hashes(control/'runtime').items() if old_runtime.get(name)!=value}
    if changed-set(patches):raise ValueError('Unexpected runtime patch')
    if artifacts.tree_hashes(out/'runs')!=completed_hashes:raise ValueError('Published results changed')
    if artifacts.tree_hashes(previous/'runtime')!=old_runtime:raise ValueError('Old runtime changed')
    for row in failed:
        if artifacts.tree_hashes(row['path'])!=row['hashes']:raise ValueError('Old failed attempt changed')
    common.verify(out)
    io.write(control/'runtime_audit.json',dict(passed=True,changed_runtime=sorted(changed),
        original_configuration_sha256=io.sha(out/'qa_config.json'),new_configuration_sha256=io.sha(control/'qa_config.json'),
        only_config_change={'query.field_extraction_batch_size':[cfg['query']['field_extraction_batch_size'],4]},
        published_results_unchanged=True,old_failure_unchanged=True,source_entry_unchanged=str(out)))
    frozen=artifacts.tree_hashes(control)
    frozen.pop('claims.json',None)
    io.write(control/'frozen_hashes.json',frozen)
    print(dict(control=str(control),success=len(success),failed_processing_seconds=failed_seconds),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--previous',type=Path,required=True);parser.add_argument('--control',type=Path,required=True)
    args=parser.parse_args();prepare(args.out.resolve(),args.previous.resolve(),args.control.resolve())
