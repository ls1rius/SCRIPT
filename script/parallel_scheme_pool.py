"""Drain sequential M/O/P at a durable question boundary, then run isolated workers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('pool_support', Path(__file__).with_name('scale_live_parallel_pool.py'))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
read, write, now, sha, identity = support.read, support.write, support.now, support.sha, support.identity


@contextmanager
def locked(control):
    with (control/'claims.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def complete_result(row, scheme):
    if row.get('error'):
        return False
    if scheme == 'M':
        return bool(row.get('answer'))
    return bool(row.get('complete') and (row.get('answer') if scheme == 'P' else row.get('arms')))


def completed(out, scheme):
    result = []
    ids=set(read(out/'cohort.json'))
    for folder in (out/'runs').iterdir():
        if folder.name not in ids:
            if scheme=='M' and folder.name=='memory_cache':
                continue
            raise ValueError('Unexpected non-question output directory: '+str(folder))
        path = folder/'result.json'
        if not path.exists() or not complete_result(read(path), scheme):
            return None
        result.append(folder.name)
    return result


def boundary_ready(out, scheme, uid):
    path = out/'runs'/uid/'result.json'
    if not path.exists() or not complete_result(read(path), scheme):
        return False
    if completed(out, scheme) is None:
        return False
    if any((out/'source_staging').glob('*')):
        return False
    return not any((out/'runs'/uid).rglob('*.pending.json'))


def frozen_modules(out):
    sys.path.insert(0, str(out/'runtime'))
    from script import controlled_memory_io as io
    from script_core.budget import ModelWorker
    from script.run_evidence_planner_experiment import verify_model
    return io, ModelWorker, verify_model


def prepare(out, control, scheme, workers):
    if scheme not in ('M','O','P') or workers != 2:
        raise ValueError('This extension supports two isolated workers per M/O/P')
    status = read(out/'queue_status.json')
    if status['state'] != 'running' or status.get('scheduler'):
        raise ValueError('Require the active sequential run')
    if (out/'failure.json').exists():
        raise ValueError('Never scale or retry a failed run')
    support.verify_manifest(out)
    old_worker = status['worker_pid']
    old_controller = status['controller_pid']
    if identity(old_worker) is None or identity(old_controller) is None:
        raise ValueError('Original processes must be alive')
    gates = ['functional.json'] if scheme=='M' else ['repair_functional.json','role_contract_functional.json'] if scheme=='P' else ['source_bound_functional.json','batch_functional.json']
    if any(not read(out/'validation'/g)['passed'] for g in gates):
        raise ValueError('Require passed identical-runtime technical gates')
    control.mkdir()
    shutil.copy2(__file__, control/'runner.py')
    shutil.copy2(Path(__file__).with_name('scale_live_parallel_pool.py'), control/'scale_live_parallel_pool.py')
    policy = read(out/'experiment.json')
    write(control/'config.json', dict(out=str(out), scheme=scheme, workers=workers,
        resource_policy=policy['resource_policy'], original_controller=dict(pid=old_controller,identity=identity(old_controller)),
        original_worker=dict(pid=old_worker,identity=identity(old_worker)), at=now(),
        method_scope='Identical frozen runtime, model, schema, prompts, batch sizes, source rules and per-question budgets',
        handoff_scope='Wait for a successful durable question boundary; verify no partial run/pending call; then close old worker',
        gates=dict(inherited=True, files=gates, scope='Technical gates inherited, not new semantic validation'),
        accounting='Successful calls retained once; no interrupted model calls, discarded work, budget expansion or retries'))
    write(control/'preflight.json',dict(passed=True,at=now(),frozen_runtime_verified=True))
    write(control/'frozen_hashes.json',{f.name:sha(f) for f in control.iterdir() if f.is_file()})
    write(control/'queue_status.json',dict(state='prepared',updated_at=now()))


def initialize_claims(out, control, scheme):
    ids = read(out/'cohort.json')
    done = completed(out,scheme)
    if done is None or not set(done)<=set(ids):
        raise ValueError('Drain did not leave only successful complete questions')
    write(control/'claims.json',{q:dict(state='inherited',index=ids.index(q)+1) for q in done})
    with ThreadPoolExecutor(max_workers=8) as pool:
        hashes=dict(pool.map(lambda q:(q,sha(out/'runs'/q/'result.json')),done))
    write(control/'inherited_results.json',hashes)
    return len(done)


def claim(out, control, wid, scheme):
    with locked(control):
        if (control/'failure.json').exists():
            return None
        state=read(control/'claims.json')
        for index,uid in enumerate(read(out/'cohort.json'),1):
            if uid in state:
                continue
            if (out/'runs'/uid).exists():
                raise ValueError('Unclaimed existing output: '+uid)
            if scheme!='M':
                parent=Path(read(out/'experiment.json')['parent_N'])
                path=parent/'runs'/uid/'result.json'
                if not path.exists():
                    return ('wait_source',uid)
                row=read(path)
                if row.get('error') or not row.get('answer'):
                    raise ValueError('Unsuccessful upstream source: '+uid)
            state[uid]=dict(state='running',index=index,worker_id=wid,pid=os.getpid(),at=now())
            write(control/'claims.json',state)
            return index,uid
        return None


def publish(out, control, uid, folder, scheme):
    with locked(control):
        if not complete_result(read(folder/'result.json'),scheme):
            raise ValueError('Cannot publish incomplete or failed question')
        if any(folder.rglob('*.pending.json')):
            raise ValueError('Cannot publish pending model calls')
        target=out/'runs'/uid
        if target.exists():
            raise ValueError('Duplicate question publication')
        folder.rename(target)
        # All absolute call/input references continue to resolve immediately.
        folder.symlink_to(target,target_is_directory=True)
        state=read(control/'claims.json')
        state[uid].update(state='complete',completed_at=now())
        write(control/'claims.json',state)


def mark_failure(control, exc, wid=None):
    with locked(control):
        if not (control/'failure.json').exists():
            write(control/'failure.json',dict(type=type(exc).__name__,message=str(exc),worker_id=wid,at=now()))


def report(out, control, cfg):
    scheme=cfg['scheme']
    if scheme=='M':
        from script.run_m_scene_experiment import report as build
    elif scheme=='P':
        from script.run_p_full_experiment import report as build
    else:
        from script.o_composition_context_eval import report as build
    with locked(control):
        result=build(out)
        result.update(parallel_worker_count=cfg['workers'],parallel_scheduler=str(control),
            parallel_setup_seconds=sum(read(f).get('seconds',0) for f in (control/'workers').glob('*/setup.json')),
            scheduler_failure=read(control/'failure.json') if (control/'failure.json').exists() else None)
        write(out/'report.json',result)
    return result


def prepare_view(out, folder):
    folder.mkdir()
    for name in ['experiment.json','evaluation_only.json','cohort.json','qa_config.json','cases','questions','validation']:
        (folder/name).symlink_to(out/name,target_is_directory=(out/name).is_dir())
    (folder/'runs').mkdir()


def run_question(out, control, cfg, wid, uid, backend, model_cfg):
    scheme=cfg['scheme']
    if scheme=='M':
        import torch
        from script_core.scheme4 import answer
        packet=read(out/'inputs'/f'{uid}.json')
        if packet['question']['question_id']!=uid:
            raise ValueError('Wrong question packet')
        if sha(packet['feature_uri'])!=packet['feature_hash']:
            raise ValueError('Feature hash changed')
        with ThreadPoolExecutor(max_workers=4) as pool:
            checks=list(pool.map(lambda f:sha(f['path'])==f['sha256'],packet['frames']))
        if not all(checks):
            raise ValueError('Frame hash changed')
        features=torch.load(packet['feature_uri'],map_location='cpu',weights_only=True).tolist()
        target=control/'attempts'/uid/'run'
        if target.exists():
            raise ValueError('Existing M attempt; no retry')
        row=answer(packet,features,model_cfg,backend,target)
        if not complete_result(row,'M') or read(target/'partial_temporal/result.json').get('memory_variant')!='M':
            raise ValueError('M query failed or variant changed: '+str(row.get('error')))
        search=read(target/'partial_search/result.json')
        if search['opened_scenes']!=search['retrieval']['candidate_order'][:len(search['opened_scenes'])]:
            raise ValueError('M candidate order changed')
    else:
        from script.run_o_full_experiment import prepare_case,verify_case
        if not prepare_case(out,uid):
            raise ValueError('Claimed source is no longer ready')
        case=out/'cases'/uid
        verify_case(case)
        if scheme=='P':
            from script.run_p_sentence_experiment import run_case
            target=control/'attempts'/uid/'run'
            policy=read(out/'experiment.json')
            extra=dict(selection_replay=policy['selection_replays'][uid]) if uid in policy.get('selection_replays',{}) else {}
            run_case(backend,model_cfg,read(case/'question.json'),read(case/'sentences.json'),read(case/'original_qa.json'),target,**extra)
        else:
            from script.o_batched_matrix import run
            view=control/'workers'/str(wid)/'view'
            if not view.exists():
                prepare_view(out,view)
            target=view/'runs'/uid
            if target.exists():
                raise ValueError('Existing O attempt; no retry')
            run(view,backend,model_cfg,case_ids=[uid])
    publish(out,control,uid,target,scheme)


def worker(control, wid):
    support.verify_manifest(control)
    cfg=read(control/'config.json');out=Path(cfg['out'])
    io,ModelWorker,verify_model=frozen_modules(out)
    verify_model(out)
    model_cfg=read(out/'qa_config.json')
    from script.gpu_queue import acquire
    folder=control/'workers'/str(wid)
    if (folder/'setup.json').exists():
        raise ValueError('Worker already attempted')
    backend=ModelWorker(model_cfg);lease=None;gpu=None;reader_validated=False
    def status(stage,**extra):
        write(folder/'status.json',dict(stage=stage,pid=os.getpid(),at=now(),**extra))
    def release():
        nonlocal lease,gpu
        backend.close(graceful=True)
        if lease:
            lease.__exit__(None,None,None)
        lease=None;gpu=None
        os.environ.pop('VIDEOAGENT_GPU_LEASE',None)
    def waiting(details):
        if (control/'failure.json').exists():
            raise RuntimeError('Pool halted after failure')
        status('waiting_for_gpu',**details)
    try:
        while True:
            item=claim(out,control,wid,cfg['scheme'])
            if item is None:
                break
            index,uid=item
            if index=='wait_source':
                release()
                from script.run_o_full_experiment import assert_upstream_running
                assert_upstream_running(Path(read(out/'experiment.json')['parent_N']))
                status('waiting_for_N_source',question_id=uid,model_released=True)
                time.sleep(10)
                continue
            if lease is None:
                lease=acquire(cfg['resource_policy'],waiting)
                gpu,_=lease.__enter__()
                os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid']
                os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
                status('loading',gpu=gpu['index'],index=index,question_id=uid)
                start=time.monotonic();details=backend.call('prepare_vlm',{},600)
                previous=read(folder/'setup.json') if (folder/'setup.json').exists() else dict(seconds=0,reloads=[])
                previous['seconds']+=time.monotonic()-start
                previous['reloads'].append(dict(at=now(),gpu=gpu,details=details))
                previous['gates']=cfg['gates']
                write(folder/'setup.json',previous)
                if cfg.get('reader_repair_validation') and not reader_validated:
                    from script.run_p_reader_repair import validate_reader
                    validate_reader(folder,backend,model_cfg)
                    reader_validated=True
            status('query',index=index,question_id=uid,gpu=gpu['index'])
            run_question(out,control,cfg,wid,uid,backend,model_cfg)
            print('PARALLEL COMPLETE',cfg['scheme'],wid,index,uid,flush=True)
        status('complete')
    except BaseException as exc:
        mark_failure(control,exc,wid)
        status('failed',error=str(exc))
        raise
    finally:
        release()


def process_state(pid):
    try:
        return Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[0]
    except FileNotFoundError:
        return None


def drain(out, control, cfg):
    old=cfg['original_worker'];controller=cfg['original_controller']
    tried=None
    last_status=0.
    while True:
        if identity(old['pid'])!=old['identity'] or identity(controller['pid'])!=controller['identity']:
            raise RuntimeError('Original process exited before a safe handoff')
        if (out/'failure.json').exists() or read(out/'queue_status.json')['state']=='failed':
            raise RuntimeError('Original run failed; no automatic retry')
        progress=read(out/'progress.json');uid=progress.get('question_id')
        if time.monotonic()-last_status>=2:
            write(control/'status.json',dict(state='waiting_for_boundary',pid=os.getpid(),
                original_progress=progress,updated_at=now()))
            last_status=time.monotonic()
        path=out/'runs'/str(uid)/'result.json'
        if uid and uid!=tried and path.exists() and complete_result(read(path),cfg['scheme']):
            tried=uid
            os.kill(old['pid'],signal.SIGSTOP)
            deadline=time.monotonic()+2
            while process_state(old['pid'])!='T':
                if time.monotonic()>deadline:
                    raise RuntimeError('Could not pause worker for boundary verification')
                time.sleep(.001)
            if not boundary_ready(out,cfg['scheme'],uid):
                os.kill(old['pid'],signal.SIGCONT)
                continue
            write(control/'boundary.json',dict(passed=True,question_id=uid,at=now(),
                pending_model_calls=0,all_started_questions_successfully_complete=True,
                worker_paused_between_questions=True))
            # Freeze the supervisor before stopping it so it cannot mark the
            # deliberately drained worker's exit as an experimental failure.
            support.handoff_supervisor(controller,[old])
            # SIGINT is delivered on continuation; the worker's finally blocks
            # close its idle model and release its reservation normally.
            os.kill(old['pid'],signal.SIGINT)
            os.kill(old['pid'],signal.SIGCONT)
            deadline=time.monotonic()+30
            while identity(old['pid'])==old['identity']:
                if time.monotonic()>deadline:
                    raise RuntimeError('Drained worker did not close its idle model')
                time.sleep(.1)
            return
        time.sleep(.01)


def supervise(control):
    support.verify_manifest(control)
    cfg=read(control/'config.json');out=Path(cfg['out'])
    frozen_modules(out)
    with (control/'controller.lock').open('a') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (control/'launch.json').exists():
            raise ValueError('Pool launch already attempted')
        write(control/'launch.json',dict(pid=os.getpid(),phase='draining',at=now()))
        write(control/'queue_status.json',dict(state='waiting_for_boundary',controller_pid=os.getpid(),updated_at=now()))
        children=[]
        try:
            if not cfg.get('resume_failed_run'):
                drain(out,control,cfg)
            with (out/'controller.lock').open('a') as ownership:
                fcntl.flock(ownership,fcntl.LOCK_EX|fcntl.LOCK_NB)
                count=initialize_claims(out,control,cfg['scheme'])
                write(control/'handoff.json',dict(passed=True,inherited_successes=count,at=now(),interrupted_model_calls=0))
                env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',CUDA_DEVICE_ORDER='PCI_BUS_ID',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
                for wid in range(cfg['workers']):
                    folder=control/'workers'/str(wid);folder.mkdir(parents=True)
                    command=[sys.executable,'-u',str(control/'runner.py'),'worker','--control',str(control),'--worker-id',str(wid)]
                    with (folder/'worker.log').open('a') as log:
                        child=subprocess.Popen(command,cwd=control,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
                    children.append(child)
                write(control/'launch.json',dict(pid=os.getpid(),phase='running',worker_pids=[p.pid for p in children],at=now()))
                while True:
                    for wid,child in enumerate(children):
                        if child.poll() not in (None,0):
                            mark_failure(control,RuntimeError('Worker exited: '+str(child.returncode)),wid)
                    rows={str(i):read(control/'workers'/str(i)/'status.json') for i in range(len(children)) if (control/'workers'/str(i)/'status.json').exists()}
                    result=report(out,control,cfg)
                    alive=any(p.poll() is None for p in children)
                    state='running' if alive else 'complete' if result['complete'] and not (control/'failure.json').exists() else 'failed'
                    snapshot=dict(state=state,controller_pid=os.getpid(),worker_pids=[p.pid for p in children],
                        workers=rows,scheduler=str(control),effective_worker_count=cfg['workers'],updated_at=now())
                    write(out/'queue_status.json',snapshot);write(control/'queue_status.json',snapshot)
                    write(control/'status.json',snapshot)
                    active=[v for v in rows.values() if v.get('stage')=='query']
                    if active:
                        first=min(active,key=lambda v:v['index'])
                        write(out/'progress.json',dict(stage='parallel_queries',index=first['index'],question_id=first['question_id'],active_queries=active,updated_at=now()))
                    if not alive:
                        return
                    time.sleep(5)
        except BaseException as exc:
            # Always resume a worker if boundary verification/handoff failed.
            old=cfg['original_worker']
            if identity(old['pid'])==old['identity'] and process_state(old['pid'])=='T':
                os.kill(old['pid'],signal.SIGCONT)
            mark_failure(control,exc)
            write(control/'queue_status.json',dict(state='failed',controller_pid=os.getpid(),error=str(exc),updated_at=now()))
            # A still-running original scheduler keeps ownership of its status.
            if cfg.get('resume_failed_run') or identity(cfg['original_controller']['pid'])!=cfg['original_controller']['identity']:
                write(out/'queue_status.json',dict(state='failed',controller_pid=os.getpid(),scheduler=str(control),error=str(exc),updated_at=now()))
            raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['prepare','supervise','worker'])
    parser.add_argument('--out',type=Path)
    parser.add_argument('--control',type=Path,required=True)
    parser.add_argument('--scheme',choices=['M','O','P'])
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--worker-id',type=int)
    args=parser.parse_args()
    if args.mode=='prepare':
        prepare(args.out.resolve(),args.control.resolve(),args.scheme,args.workers)
    elif args.mode=='supervise':
        supervise(args.control.resolve())
    else:
        worker(args.control.resolve(),args.worker_id)
