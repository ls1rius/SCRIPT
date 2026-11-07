"""Preserve completed O/O+ pairs and follow the explicitly repaired N run."""
import argparse
from pathlib import Path
import shutil
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script.run_mn_decoder_resume import tree_hashes


def prepare(old,out,parent):
    from script.run_runtime_repair_resume import replace_function
    from script.run_o_sentence_experiment import verify
    from script.o_composition_context_eval import report
    verify(old)
    if out.exists() or io.read(old/'queue_status.json')['state']!='failed':raise ValueError('Stopped old run and fresh output required')
    if list((old/'runs').rglob('*.pending.json')):raise ValueError('Pending calls require explicit accounting')
    before=tree_hashes(old);out.mkdir(parents=True)
    for name in ['qa_config.json','qa_expected_artifacts.json','evaluation_only.json','cohort.json','cohost_queue.py','cohost_authorization.json']:
        shutil.copy2(old/name,out/name)
    for name in ['runtime','questions','cases','runs']:
        shutil.copytree(old/name,out/name,ignore=shutil.ignore_patterns('__pycache__'))
    ids=[]
    for f in sorted((out/'runs').glob('*/result.json')):
        d=io.read(f)
        if not d.get('complete') or d.get('error'):raise ValueError('Only complete pairs can be inherited')
        ids.append(f.parent.name)
    policy=io.read(old/'experiment.json');policy.update(parent_N=str(parent),previous_run=str(old),created_at=io.now(),
        answers_reused='Both O and O+ complete prefix; no regeneration',runtime_repair='Shared scalar JSON guard and complete input limit; O method unchanged',
        scope='Same 300-case cohort, prior completed O/O+ pairs preserved, follows repaired N sources')
    io.write(out/'experiment.json',policy)
    cfg=io.read(out/'qa_config.json');cfg['models']['max_input_tokens']=32768;io.write(out/'qa_config.json',cfg)
    target=out/'runtime/script_core/backend.py'
    target.write_text(replace_function(target.read_text(),(ROOT/'script_core/backend.py').read_text(),'constrained_prefix'))
    for name in ['script_core/json_unicode_guard.py','script_core/o_generation.py','script_core/m_generation.py']:
        shutil.copy2(ROOT/name,out/'runtime'/name)
    arc=out/'archived_attempt';arc.mkdir()
    for name in ['setup.json','failure.json','queue_status.json','report.json','experiment.json']:
        if (old/name).exists():shutil.copy2(old/name,arc/name)
    shutil.copytree(old/'validation',arc/'validation')
    io.write(out/'validation/functional.json',io.read(old/'validation/functional.json'))
    if tree_hashes(old)!=before:raise ValueError('Old O changed')
    io.write(out/'continuation.json',dict(prior_hashes=before,completed_pairs=ids,prior_unchanged=True,old_setup=io.read(old/'setup.json'),
        historical_gate_costs={f.name:io.read(f).get('cost_summary') for f in (old/'validation').glob('*functional.json')},
        inherited_costs='Successful calls counted once from copied runs; old setup/gates archived separately',new_parent_N=str(parent)))
    shutil.copy2(ROOT/'script/resume_o_runtime.py',out/'runtime/script/resume_o_runtime.py')
    io.write(out/'frozen_hashes.json',tree_hashes(out));verify(out);report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    print('Prepared O/O+ continuation:',len(ids),'pairs',flush=True)


def _follow(out):
    import os,runpy
    parent=Path(io.read(out/'experiment.json')['parent_N'])
    for uid in io.read(out/'experiment.json')['case_ids']:
        if not (out/'runs'/uid/'result.json').exists():break
    else:raise ValueError('All O pairs already complete')
    while True:
        path=parent/'runs'/uid/'result.json'
        if path.exists():
            result=io.read(path)
            if result.get('error'):raise ValueError('Repaired N failed at next O source')
            if result.get('answer'):break
        state=io.read(parent/'queue_status.json')
        if state['state']=='failed':raise ValueError('Repaired N stopped before next source')
        io.write(out/'queue_status.json',dict(state='waiting_for_N_source',controller_pid=os.getpid(),question_id=uid,parent=str(parent),model_loaded=False,updated_at=io.now()))
        time.sleep(30)
    # The scheduler entry selects any GPU with sufficient shared-memory capacity.
    runpy.run_path(str(out/'cohost_queue.py'),run_name='__main__')


def follow(out):
    try:_follow(out)
    except Exception as exc:
        import os
        io.write(out/'queue_status.json',dict(state='failed',controller_pid=os.getpid(),updated_at=io.now(),error=str(exc)))
        raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','follow']);p.add_argument('--out',type=Path,required=True);p.add_argument('--old',type=Path);p.add_argument('--parent',type=Path)
    a=p.parse_args()
    if a.mode=='prepare':prepare(a.old.resolve(),a.out.resolve(),a.parent.resolve())
    else:follow(a.out.resolve())
