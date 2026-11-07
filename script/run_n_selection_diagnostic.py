"""Fresh N selection calls on archived failures and an unrelated negative control."""
import argparse
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io


def verify(out):
    for name,sha in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=sha:
            raise ValueError('Frozen selection diagnostic changed: '+name)


def prepare(out):
    from script_core.config import load_config
    from script_core import m_candidate_selection as selection, n_scene_memory as n
    if out.exists():raise ValueError('New output directory required; no retries')
    out.mkdir(parents=True)
    parent=ROOT/'workdirs/scene-planned-M-L-300-20261102-r13'
    cases=[dict(id='unrelated',kind='synthetic_negative',inputs=dict(
        question='Which tool does C use to open the sealed jar?',
        options=['A knife.','A spoon.','A can opener.','Scissors.','Bare hands.'],
        directory=[dict(id='S0',interval=[0,10],description='Clouds drift across the sky above a distant mountain.'),
                   dict(id='S1',interval=[10,20],description='An empty road and parked cars; no person or object handling is described.')],
        source_type='caption'),expectation=dict(required_candidates=[],empty=True))]
    for qid,required in [('6dd07731-937c-4b63-b296-0a947ffe6996',['S3','S4']),
                         ('98427b0b-20ed-45bb-b772-91573ce90b5a',['S3','S7'])]:
        root=parent/'runs'/qid/'partial_search'
        calls=[json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
        call=next(c for c in calls if c['operation']=='m_select_scene')
        source=Path(call['output_ref']);raw=io.read(source)
        cases.append(dict(id=qid,kind='archived_failure',inputs=deepcopy(raw['inputs']),
            expectation=dict(required_candidates=required,empty=False,
                higher_priority_pairs=[['S3','S0']] if qid.startswith('6dd') else [['S7','S1']]),
            source=dict(path=str(source),sha256=io.sha(source)),old_output=raw['output']['data']))
        (out/'historical_navigation').mkdir(exist_ok=True)
        shutil.copy2(root/'navigation_trace.json',out/'historical_navigation'/f'{qid}.json')
    io.write(out/'cases.json',cases)
    cfg=load_config(ROOT/'configs/script_N.yaml')
    for key in ['vlm','visual_encoder','detector','text_embedding']:
        cfg['models'][key]=str((ROOT/cfg['models'][key]).resolve())
    io.write(out/'qa_config.json',cfg)
    shutil.copy2(parent/'qa_expected_artifacts.json',out/'qa_expected_artifacts.json')
    for name in ['script_core','script']:
        shutil.copytree(ROOT/name,out/'runtime'/name,ignore=shutil.ignore_patterns('__pycache__'))
    for path in ROOT.glob('*.py'):shutil.copy2(path,out/'runtime'/path.name)
    io.write(out/'experiment.json',dict(created_at=io.now(),version=n.VERSION,selection_version=selection.VERSION,
        scope='Selection-stage diagnostic only; archived summaries/options unchanged, no new scene reading or QA',
        expectations_not_in_model_inputs=True,no_option_permutation=True,no_retries=True,
        prior_attempts=[dict(path=str(p.parent.resolve()),report_sha256=io.sha(p),cost_summary=io.read(p).get('cost_summary'))
            for p in sorted([*out.parent.glob('N-option-scene-selection-*/report.json'),*out.parent.glob('MN-joint-selection-*/report.json')])],
        call_budget_seconds=cfg['budget']['tool_call_cap'],resource_policy=dict(preference=['1','2','3'],
            minimum_free_mib=32768,maximum_utilization=100,stable_observations=3,selection='most_free_memory')))
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file()})
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))


def worker(out):
    verify(out)
    from script.run_evidence_planner_experiment import verify_model
    from script_core import m_candidate_selection as selection,m_scene_memory as parent
    from script_core.budget import ModelWorker,BudgetLedger,Meter
    if (out/'setup.json').exists():raise ValueError('Already attempted; no retry')
    io.write(out/'setup.json',dict(state='verifying_model',started_at=io.now()))
    verify_model(out)
    cfg=io.read(out/'qa_config.json');backend=ModelWorker(cfg)
    ledger=BudgetLedger(1500,answer_reserve=0,calls_path=out/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,out/'raw_calls')
    report=dict(complete=False,passed=False,error=None,cases=[],scope='selection_only')
    try:
        start=time.monotonic();prepared=backend.call('prepare_vlm',{},600)
        io.write(out/'setup.json',dict(state='ready',seconds=time.monotonic()-start,details=prepared))
        for case in io.read(out/'cases.json'):
            cid=case['id'];target=out/'cases'/cid
            io.write(out/'progress.json',dict(case_id=cid,stage='selection',updated_at=io.now()))
            io.write(target/'input.json',case['inputs'])
            model_input=selection.prepare_inputs(case['inputs'])
            io.write(target/'model_input.json',model_input)
            scene_calls=[]
            def on_review(sid,request,output,call):
                oid='review'
                io.write(target/'scene_reviews'/sid/f'{oid}.json',dict(inputs=request,output=output,call=call))
                scene_calls.append(call)
                io.write(out/'progress.json',dict(case_id=cid,stage='selection',last_scene=sid,last_option=oid,updated_at=io.now()))
            raw=selection.review_scenes(model_input,meter,on_review)
            io.write(target/'model_output.json',raw)
            derived=selection.normalize_selection(raw,case['inputs'])
            queue=parent.validate_candidates(derived,case['inputs'])
            expected=case['expectation']
            missing=sorted(set(expected['required_candidates'])-set(queue))
            bad_rank_pairs=[pair for pair in expected.get('higher_priority_pairs',[])
                if derived['scene_decisions'][pair[0]]['priority'] <= derived['scene_decisions'][pair[1]]['priority']]
            passed=not missing and not bad_rank_pairs and (not queue if expected['empty'] else bool(queue))
            result=dict(id=cid,kind=case['kind'],passed=passed,candidate_order=queue,
                missing_required_candidates=missing,expected_empty=expected['empty'],
                failed_priority_pairs=bad_rank_pairs,
                derived_selection=derived,model_output=raw,
                seconds=sum(c['actual_cost'] for c in scene_calls),input_tokens=sum(c.get('input_tokens',0) for c in scene_calls),
                output_tokens=sum(c.get('output_tokens',0) for c in scene_calls),model_calls=len(scene_calls))
            io.write(target/'result.json',result)
            io.write(target/'navigation_trace.json',dict(new_scene_reads=False,scene_catalog=case['inputs']['directory'],
                question=case['inputs']['question'],options=case['inputs']['options'],model_proposal=raw,
                actual_selection=derived,candidate_order=queue,opened_scenes=[],
                rule='Joint all-option ranking; descending priority; directory-order ties',
                stop_reason='selection_only_diagnostic'))
            report['cases'].append(result)
            io.write(out/'report.json',dict(report,cost_summary=ledger.summary()))
        report.update(complete=True,passed=all(c['passed'] for c in report['cases']))
    except Exception as exc:
        report['error']=dict(type=type(exc).__name__,message=str(exc))
        raise
    finally:
        report.update(updated_at=io.now(),cost_summary=ledger.summary())
        io.write(out/'report.json',report);meter.close();backend.close(graceful=True)


def queue(out):
    verify(out)
    from script.gpu_queue import acquire
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Already launched; no retry')
        def status(state,**extra):
            io.write(out/'queue_status.json',dict(state=state,controller_pid=os.getpid(),updated_at=io.now(),**extra))
        try:
            with acquire(io.read(out/'experiment.json')['resource_policy'],lambda info:status('waiting_for_gpu',**info)) as (gpu,fds):
                env=dict(os.environ,CUDA_VISIBLE_DEVICES=gpu['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                    OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
                command=[sys.executable,'-u',str(out/'runtime/script/run_n_selection_diagnostic.py'),'worker','--out',str(out)]
                with (out/'worker.log').open('a') as log:
                    child=subprocess.Popen(command,cwd=out/'runtime',env=env,stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,pass_fds=fds)
                    launch=dict(worker_pid=child.pid,gpu=gpu['index'],gpu_uuid=gpu['uuid'],command=command,started_at=io.now())
                    io.write(out/'launch.json',launch)
                    while child.poll() is None:status('running',**launch);time.sleep(5)
                result=io.read(out/'report.json')
                if child.returncode or not result['complete']:raise RuntimeError('Diagnostic runtime failed; no retry')
                status('complete' if result['passed'] else 'semantic_failed',**launch)
        except Exception as exc:
            status('failed',error=dict(type=type(exc).__name__,message=str(exc)));raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','queue','worker'])
    parser.add_argument('--out',type=Path,required=True);args=parser.parse_args()
    globals()[args.action](args.out.resolve())
