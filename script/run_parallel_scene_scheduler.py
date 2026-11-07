"""Question-isolated M/N workers over an unchanged frozen experiment runtime.

Completed outputs are published atomically into the existing source directory,
so downstream O/P consumers continue without changing their frozen inputs.
"""
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script import run_m_scene_experiment as common


@contextmanager
def locked(control):
    with (control/'claims.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        yield


def verify(control):
    for name,sha in io.read(control/'frozen_hashes.json').items():
        if io.sha(control/name)!=sha:raise ValueError('Scheduler artifact changed: '+name)


def worker_config(out,control):
    """Permit only a frozen, explicitly audited execution-batch override."""
    original=io.read(out/'qa_config.json')
    path=control/'qa_config.json'
    if not path.exists():return original
    cfg=io.read(path)
    from copy import deepcopy
    expected=deepcopy(original)
    expected['query']['field_extraction_batch_size']=4
    if cfg!=expected:raise ValueError('Parallel configuration changes exceed batch-size override')
    if io.read(control/'policy.json').get('field_extraction_batch_size')!=4:
        raise ValueError('Parallel batch-size override lacks policy declaration')
    return cfg


def claim(out,control,worker_id,gpu):
    with locked(control):
        if (control/'failure.json').exists():return None
        state=io.read(control/'claims.json')
        for index,uid in enumerate(io.read(out/'cohort.json'),1):
            if uid in state:continue
            # Only the sealed cutover prefix may be inherited, not arbitrary outputs.
            if (out/'runs'/uid).exists():raise ValueError('Unclaimed output already exists: '+uid)
            state[uid]=dict(state='running',index=index,worker_id=worker_id,pid=os.getpid(),
                gpu=gpu['index'],started_at=io.now())
            io.write(control/'claims.json',state)
            return index,uid
        return None


def publish(out,control,uid,attempt):
    with locked(control):
        result=io.read(attempt/'result.json')
        if result.get('error') or not result.get('answer'):raise ValueError('Cannot publish failed result')
        if list(attempt.rglob('*.pending.json')):raise ValueError('Cannot publish pending calls')
        if (out/'runs'/uid).exists():raise ValueError('Duplicate publication: '+uid)
        attempt.rename(out/'runs'/uid)
        state=io.read(control/'claims.json');state[uid].update(state='complete',completed_at=io.now())
        io.write(control/'claims.json',state)
        # Active attempts stay outside runs/, so reports only read stable outputs.
        refresh_report(out,control)


def refresh_report(out,control):
    report=common.report(out)
    handoff=io.read(control/'handoff.json')
    report['parallel_scheduler']=str(control)
    report['parallel_worker_count']=io.read(control/'policy.json')['workers']
    report['scheduler_interrupted_processing_seconds']=handoff['interrupted_processing_seconds']
    report['scheduler_pending_calls_at_interruption']=handoff['pending_calls_at_interruption']
    report['parallel_setup_seconds']=sum(io.read(f).get('seconds',0) for f in (control/'workers').glob('*/setup.json'))
    report['scheduler_failure']=io.read(control/'failure.json') if (control/'failure.json').exists() else None
    report['scheduler_historical_failed_processing_seconds']=handoff.get('failed_processing_seconds',0)
    report['parallel_field_extraction_batch_size']=io.read(control/'policy.json').get('field_extraction_batch_size')
    report['parallel_new_gate_seconds']=sum(io.read(f).get('total',0) for f in (control/'workers').glob('*/validation/cost_summary.json'))
    io.write(out/'report.json',report)
    return report


def run_question(out,control,backend,cfg,uid):
    import torch
    from script_core.scheme4 import answer
    packet=io.read(out/'inputs'/f'{uid}.json')
    if packet['question']['question_id']!=uid:raise ValueError('Question identity mismatch')
    if io.sha(Path(packet['feature_uri']))!=packet['feature_hash']:raise ValueError('Features changed')
    for frame in packet['frames']:
        if io.sha(Path(frame['path']))!=frame['sha256']:raise ValueError('Frame changed')
    features=torch.load(packet['feature_uri'],map_location='cpu',weights_only=True).tolist()
    # scheme4 places its cache beside output: a per-question parent prevents
    # simultaneous questions from sharing a mutable cache directory.
    attempt=control/'attempts'/uid/'run'
    if attempt.exists():raise ValueError('No automatic retry of an existing attempt')
    variant=io.read(out/'experiment.json').get('experiment_label','M')
    if (control/'recovery_cases.json').exists() and uid in io.read(control/'recovery_cases.json'):
        from script.n_planner_continuation import run as resume_planner
        result=resume_planner(out,control,backend,cfg,uid,attempt)
    else:
        result=answer(packet,features,cfg,backend,attempt)
    if result.get('error') or not result.get('answer'):raise ValueError('Query failed: '+str(result.get('error')))
    if io.read(attempt/'partial_temporal/result.json').get('memory_variant')!=variant:
        raise ValueError('Wrong memory variant')
    search=io.read(attempt/'partial_search/result.json')
    order=search['retrieval']['candidate_order'];opened=search['opened_scenes']
    if opened!=order[:len(opened)] or not (attempt/'partial_search/navigation_trace.json').exists():
        raise ValueError('Candidate order or audit violated')
    publish(out,control,uid,attempt)
    if io.read(control/'policy.json').get('recovery_gate_case')==uid:
        io.write(control/'recovery_gate.json',dict(passed=True,question_id=uid,at=io.now(),
            scope='Original failed planner and reader completed once as the actual continued benchmark case',
            result_path=str(out/'runs'/uid/'result.json'),cost_summary=result['cost_summary']))


def worker(out,control,worker_id):
    from script.gpu_queue import acquire
    from script_core.budget import ModelWorker
    from script.run_evidence_planner_experiment import verify_model
    verify(control);common.verify(out);verify_model(out)
    cfg=worker_config(out,control);backend=ModelWorker(cfg)
    folder=control/'workers'/str(worker_id);folder.mkdir(parents=True,exist_ok=True)
    if (folder/'setup.json').exists():raise ValueError('Worker already attempted')
    def status(stage,**extra):io.write(folder/'status.json',dict(stage=stage,pid=os.getpid(),at=io.now(),**extra))
    def waiting(info):
        if (control/'failure.json').exists():raise RuntimeError('Pool halted after failure')
        status('waiting_for_gpu',**info)
    try:
        if io.read(control/'policy.json').get('recovery_gate_case') and worker_id!=0:
            status('waiting_for_recovery_gate',model_loaded=False)
            while not (control/'recovery_gate.json').exists():
                if (control/'failure.json').exists():raise RuntimeError('Pool halted after recovery failure')
                time.sleep(5)
            if not io.read(control/'recovery_gate.json')['passed']:raise ValueError('Recovery gate failed')
        with acquire(io.read(control/'policy.json')['resource_policy'],waiting) as (gpu,_):
            os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid']
            start=time.monotonic();details=backend.call('prepare_vlm',{},600)
            io.write(folder/'setup.json',dict(seconds=time.monotonic()-start,details=details,gpu=gpu,
                functional_gate=('Prior full/batch gates historical; exact failed planner continuation is the new real recovery gate'
                    if io.read(control/'policy.json').get('recovery_gate_case') else
                    'Original full gate inherited historically; new batch/decoder gates required before queries'
                    if io.read(control/'policy.json').get('new_batch_gate') else
                    'Inherited verified real gate for identical method/config/runtime; no new semantic claims')))
            if io.read(control/'policy.json').get('new_batch_gate'):
                status('batch_validation',gpu=gpu['index'])
                from script.run_n_parallel_validation import run as validate
                validate(folder,backend,cfg,control)
            while True:
                item=claim(out,control,worker_id,gpu)
                if item is None:break
                index,uid=item;status('query',index=index,question_id=uid,gpu=gpu['index'])
                run_question(out,control,backend,cfg,uid)
                print('PARALLEL COMPLETE',worker_id,index,uid,flush=True)
            status('complete',gpu=gpu['index'])
            backend.close(graceful=True)
    except Exception as exc:
        with locked(control):
            failure=dict(worker_id=worker_id,type=type(exc).__name__,message=str(exc),at=io.now())
            io.write(folder/'failure.json',failure)
            if not (control/'failure.json').exists():io.write(control/'failure.json',failure)
        status('failed',error=str(exc));raise
    finally:backend.close(graceful=True)


def supervise(out,control):
    verify(control);common.verify(out)
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (control/'launch.json').exists():raise ValueError('Parallel scheduler already launched')
        children=[]
        def status(state):
            workers={str(i):io.read(control/'workers'/str(i)/'status.json')
                     for i in range(len(children)) if (control/'workers'/str(i)/'status.json').exists()}
            io.write(out/'queue_status.json',dict(state=state,controller_pid=os.getpid(),
                worker_pid=children[0].pid if children else None,worker_pids=[c.pid for c in children],
                scheduler=str(control),workers=workers,updated_at=io.now()))
            active=[v for v in workers.values() if v['stage']=='query']
            if active:
                first=min(active,key=lambda v:v['index'])
                io.write(out/'progress.json',dict(stage='parallel_queries',index=first['index'],
                    question_id=first['question_id'],active_queries=active,updated_at=io.now()))
        status('running')
        env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
            CUDA_DEVICE_ORDER='PCI_BUS_ID',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
        for i in range(io.read(control/'policy.json')['workers']):
            folder=control/'workers'/str(i);folder.mkdir(parents=True,exist_ok=True)
            cmd=[sys.executable,'-u',str(control/'runtime/script/run_parallel_scene_scheduler.py'),
                'worker','--out',str(out),'--control',str(control),'--worker-id',str(i)]
            with (folder/'worker.log').open('a') as log:
                children.append(subprocess.Popen(cmd,cwd=control/'runtime',env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT))
        io.write(control/'launch.json',dict(controller_pid=os.getpid(),worker_pids=[c.pid for c in children],at=io.now()))
        while any(c.poll() is None for c in children):
            for i,c in enumerate(children):
                if c.poll() not in (None,0) and not (control/'failure.json').exists():
                    with locked(control):io.write(control/'failure.json',dict(worker_id=i,returncode=c.returncode,at=io.now()))
            status('running');time.sleep(5)
        with locked(control):result=refresh_report(out,control)
        status('complete' if result['complete'] and not (control/'failure.json').exists() else 'failed')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['supervise','worker'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--control',type=Path,required=True)
    parser.add_argument('--worker-id',type=int,default=0);a=parser.parse_args()
    if a.mode=='supervise':supervise(a.out.resolve(),a.control.resolve())
    else:worker(a.out.resolve(),a.control.resolve(),a.worker_id)
