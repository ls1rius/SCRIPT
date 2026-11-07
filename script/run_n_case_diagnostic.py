"""One fresh N extraction/selection/answer on a frozen historical source set."""
import argparse
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import controlled_memory_io as io

CASE = '951ccf40-1a99-4138-a441-4edf16af5446'


def verify(out):
    for name, sha in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name) != sha:
            raise ValueError('Frozen diagnostic changed: '+name)


def prepare(out, baseline=False):
    from script_core.config import load_config
    from script_core import n_scene_memory, n_source_extraction
    if out.exists():
        raise ValueError('Use a new output directory; no reuse of attempted calls')
    h = ROOT/'workdirs/generalized-lazy-H-300-20261101-r5'
    l = ROOT/'workdirs/evidence-planner-L-I-300-20261102-r1'
    out.mkdir(parents=True)
    sources = {
        'packet.json':h/'packets'/f'{CASE}.json',
        'source_bundle.json':h/'normalization_sources'/f'{CASE}.json',
        'field_plan.json':h/'materialized'/CASE/'plan.json',
        'original_I_input.json':l/'I_control'/f'{CASE}.json',
        'qa_expected_artifacts.json':l/'qa_expected_artifacts.json',
        'old_pool.json':l/'pool'/f'{CASE}.json',
        'old_L_plan.json':l/'runs'/CASE/'plan.json',
        'old_L_result.json':l/'runs'/CASE/'L_evidence_planner/result.json',
        'old_all_result.json':l/'runs'/CASE/'L_all_evidence/result.json',
    }
    for name, source in sources.items():
        shutil.copy2(source, out/name)
    packet = io.read(out/'packet.json')
    units = io.read(out/'source_bundle.json')['units']
    by_cid = {cid:u for u in units for cid in u['source_caption_ids']}
    rows = []
    for t in packet['selected_seconds']:
        c = packet['packet']['captions'][t]
        rows.append(dict(caption_id=c['id'],frame_index=t,sample_interval=c['interval'],
            source_id=by_cid[c['id']]['id'],original_text=c['text']))
    assert len(rows)==135 and len(units)==64 and 107 in packet['selected_seconds']
    io.write(out/'source_rows.json',rows)
    io.write(out/'question.json',packet['packet']['question'])
    io.write(out/'evaluation_only.json',{CASE:io.read(l/'evaluation_only.json')[CASE]})
    cfg = load_config(ROOT/('configs/script_M.yaml' if baseline else 'configs/script_N.yaml'))
    for name in ['vlm','visual_encoder','detector','text_embedding']:
        cfg['models'][name] = str((ROOT/cfg['models'][name]).resolve())
    io.write(out/'qa_config.json',cfg)
    io.write(out/'navigation_trace.json',dict(new_navigation=False,
        policy='Fixed original selected captions and field request to inspect extraction repair; not a full N navigation run.',
        selected_seconds=packet['selected_seconds'],all_original_captions=packet['packet']['captions'],
        historical_scene_summaries_and_round_reasons='Historical records unavailable; not reconstructed or invented.',
        source_files={name:dict(path=str(p.resolve()),sha256=io.sha(p)) for name,p in sources.items()}))
    runtime = out/'runtime'
    runtime_source = ROOT
    inherited_hashes = {}
    if baseline:
        frozen = ROOT/'workdirs/scene-planned-M-L-300-20261102-r13'
        for name, sha in io.read(frozen/'frozen_hashes.json').items():
            if io.sha(frozen/name) != sha:
                raise ValueError('M frozen source changed: '+name)
        runtime_source = frozen/'runtime'
    for name in ['script_core','script']:
        shutil.copytree(runtime_source/name,runtime/name,ignore=shutil.ignore_patterns('__pycache__'))
    for source in runtime_source.glob('*.py'):
        shutil.copy2(source,runtime/source.name)
    if baseline:
        inherited_hashes = {str(p.relative_to(runtime)):io.sha(p) for p in runtime.rglob('*') if p.is_file()}
        # The new driver orchestrates existing frozen methods; it is not an M method edit.
        shutil.copy2(Path(__file__),runtime/'script/run_n_case_diagnostic.py')
    io.write(out/'experiment.json',dict(created_at=io.now(),case_id=CASE,
        variant='unmodified_M_parent_control' if baseline else 'N',
        artifact_directory='control' if baseline else 'N',
        version=n_scene_memory.PARENT_VERSION if baseline else n_scene_memory.VERSION,
        extractor=n_source_extraction.parent.VERSION if baseline else n_source_extraction.VERSION,
        inherited_runtime=dict(source=str(runtime_source.resolve()),files=inherited_hashes),
        scope='Single fixed-source diagnostic',
        fresh_extraction=True,fresh_evidence_plan=True,fresh_answer=True,new_navigation=False,
        fixed_field_request=True,new_field_planning=False,labels_in_model_input=False,
        prior_attempts=[dict(path=str(p.parent.resolve()),report_sha256=io.sha(p),
            cost_summary=io.read(p).get('cost_summary'))
            for p in sorted(out.parent.glob('N-brick-wall-diagnostic-*/report.json'))],
        no_automatic_retries=True,original_M_untouched=True,
        resource_policy=dict(preference=['1','2','3'],minimum_free_mib=32768,
            maximum_utilization=100,stable_observations=3,selection='most_free_memory')))
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file()})
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))


def worker(out):
    verify(out)
    from script.run_evidence_planner_experiment import verify_model
    experiment = io.read(out/'experiment.json')
    baseline = experiment.get('variant') == 'unmodified_M_parent_control'
    if baseline:
        from script_core import generalized_lazy_memory as core, m_scene_memory as n
    else:
        from script_core import n_source_extraction as core, n_scene_memory as n
    from script_core import merged_lazy_memory as merged
    artifact = experiment.get('artifact_directory', 'N')
    from script_core.budget import ModelWorker, BudgetLedger, Meter
    from script.generalized_lazy_execution import SourceExtractor, ensure_batched
    from script.run_generalized_lazy_experiment import extract_source
    from script_core.entity_evidence_qa import is_infrastructure
    if (out/'setup.json').exists():
        raise ValueError('Diagnostic already attempted')
    io.write(out/'setup.json',dict(state='verifying_model',started_at=io.now()))
    verify_model(out)
    cfg=io.read(out/'qa_config.json');backend=ModelWorker(cfg)
    ledger=BudgetLedger(1500,answer_reserve=45,calls_path=out/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,out/'raw_calls')
    report=dict(case_id=CASE,complete=False,error=None,new_navigation=False,
        variant=experiment.get('variant','N'),version=experiment['version'])
    try:
        start=time.monotonic();prepared=backend.call('prepare_vlm',{},600)
        io.write(out/'setup.json',dict(state='ready',seconds=time.monotonic()-start,details=prepared))
        units=io.read(out/'source_bundle.json')['units'];rows=io.read(out/'source_rows.json')
        fields=io.read(out/'field_plan.json')['fields'];question=io.read(out/'question.json')
        class LoggedExtractor(SourceExtractor):
            def many(self,jobs):
                result=super().many(jobs)
                for unit,_ in jobs:
                    value=result[unit['id']]
                    io.write(out/'extraction_results'/f'{unit["id"]}.json',dict(source=unit,result=value))
                    if value.get('error'):
                        raise ValueError('Source extraction failed: '+str(value['error']))
                return result
        extractor=LoggedExtractor(meter,core,SimpleNamespace(is_infrastructure=is_infrastructure),extract_source,2)
        def progress(index,unit,missing):
            io.write(out/'progress.json',dict(stage='extraction',index=index,total=len(units),
                source_id=unit['id'],missing_fields=missing,updated_at=io.now()))
        with core.ScriptDB(out/'memory_cache'/artifact/'I/h_script.sqlite3',core.VERSION) as store:
            results,stats=ensure_batched(store,CASE,units,rows,fields,extractor,progress)
            description,payload,audit=store.view(CASE,question,rows,results,merged.compact_description)
        io.write(out/'memory.json',payload);io.write(out/'extraction_audit.json',dict(audit,cache=stats))
        qa=deepcopy(io.read(out/'original_I_input.json'))
        prefix,_,suffix=io.parts(qa);qa['prompts'][0]=prefix+description+suffix
        io.write(out/'new_I_input.json',qa)
        io.write(out/'progress.json',dict(stage='evidence_planning',updated_at=io.now()))
        final,planning=n.project_evidence(qa,payload,question,meter,out/artifact)
        io.write(out/'progress.json',dict(stage='answer',updated_at=io.now()))
        answer,_=meter.call('m_answer' if baseline else 'n_answer',dict(inputs=final,options=question['options']),final=True)
        io.write(out/'answer.json',answer)
        pool=io.read(out/artifact/'pool.json');plan=io.read(out/artifact/'plan.json')
        decisions={rid:kind for kind in ('keep','skip','uncertain') for rid in plan[kind]}
        for group in plan['merge_groups']:
            for rid in group:decisions[rid]='group'
        c107=next(row for row in payload['observations'] if row['caption_id']=='C107')
        related=[dict(record=r,decision=decisions[r['id']]) for r in pool['records']
            if any(o.get('caption_id')=='C107' for o in r['occurrences'])]
        evidence=io.parts(final)[1]
        (out/'final_description.txt').write_text(evidence)
        gold=io.read(out/'evaluation_only.json')[CASE]
        report.update(complete=True,answer=answer['data'],gold=str(gold),
            correct=str(answer['data']['selected_option'])==str(gold),
            frame107=c107,frame107_candidates=related,
            wall_in_accepted_fields=any(v=='brick wall' for a in c107['evidence'].get('actions',[]) for v in a.values()),
            left_hand_in_accepted_fields=any(a.get('body_part')=='left hand' for a in c107['evidence'].get('actions',[])),
            wall_in_final_description='brick wall' in evidence,
            left_hand_in_frame107_final=any('left hand' in line and 'brick wall' in line for line in evidence.splitlines()),
            source_preservation=io.read(out/artifact/'source_preservation.json') if not baseline else
                dict(enabled=False,overrides=[],reason='Unmodified frozen M parent; no N source-preservation rule.'),
            final_wall_lines=[line for line in evidence.splitlines() if 'brick wall' in line],
            pool_records=len(pool['records']),retained_records=planning['audit']['retained_records'],
            old_L=io.read(out/'old_L_result.json'),old_all=io.read(out/'old_all_result.json'))
    except Exception as exc:
        report['error']=dict(type=type(exc).__name__,message=str(exc))
        raise
    finally:
        report['cost_summary']=ledger.summary();report['updated_at']=io.now()
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
            with acquire(io.read(out/'experiment.json')['resource_policy'],
                         lambda info:status('waiting_for_gpu',**info)) as (gpu,fds):
                env=dict(os.environ,CUDA_VISIBLE_DEVICES=gpu['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                    OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
                command=[sys.executable,'-u',str(out/'runtime/script/run_n_case_diagnostic.py'),'worker','--out',str(out)]
                with (out/'worker.log').open('a') as log:
                    child=subprocess.Popen(command,cwd=out/'runtime',env=env,stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,pass_fds=fds)
                    launch=dict(worker_pid=child.pid,gpu=gpu['index'],gpu_uuid=gpu['uuid'],command=command,started_at=io.now())
                    io.write(out/'launch.json',launch)
                    while child.poll() is None:
                        status('running',**launch);time.sleep(5)
                if child.returncode or not io.read(out/'report.json')['complete']:
                    raise RuntimeError('N diagnostic failed; see saved calls, no retry')
                status('complete',**launch)
        except Exception as exc:
            status('failed',error=dict(type=type(exc).__name__,message=str(exc)));raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','queue','worker'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--unmodified-parent',action='store_true',help='Prepare a control using the frozen M-r13 runtime without N changes')
    args=parser.parse_args()
    if args.unmodified_parent and args.action != 'prepare':
        parser.error('The runtime variant is frozen during prepare')
    if args.action == 'prepare':
        prepare(args.out.resolve(),baseline=args.unmodified_parent)
    else:
        globals()[args.action](args.out.resolve())
