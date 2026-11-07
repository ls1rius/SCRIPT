"""Four-GPU scheduling with resident models through teacher, training and Q4 QA.

All experiment data and model routines come from the original frozen runtime.
The primary keeps one model and a 42GiB lease throughout the pipeline. The
second teacher keeps its model and 24GiB lease until trained evaluation is ready.
"""
import argparse
import copy
from contextlib import contextmanager
import fcntl
import importlib
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

HERE=Path(__file__).resolve().parent
if (HERE/'config.json').exists():
    sys.path.insert(0,str(Path(json.loads((HERE/'config.json').read_text())['out'])/'runtime'))
else:
    sys.path.insert(0,str(HERE.parent))
from script.scale_live_parallel_pool import read,write,sha,identity,now,verify_manifest

TRAIN='__resident_training__'
CONFIG='__resident_evaluation_config__'
GATE='__resident_gradient_gate__'
GPUS=['0','1','2','3']


class PipelineDrained(Exception):
    pass


def frozen(out):
    sys.path.insert(0,str(out/'runtime'))
    return importlib.import_module('script.train_q4_retention')


def complete_labels(out):
    claims=read(out/'teacher_claims.json')
    return all(claims.get(u,{}).get('state')=='complete' for u in read(out/'train_ids.json'))


def compatible(teacher,evaluation,out):
    a,b=copy.deepcopy(teacher),copy.deepcopy(evaluation)
    if b['models'].get('retention_adapter')!=str(out/'adapter'):
        raise ValueError('Unexpected trained adapter destination')
    if a['models'].get('retention_training_ids')!=str(out/'train_ids.json'):
        raise ValueError('Unexpected teacher question allowlist')
    a['models'].pop('retention_training_ids');b['models'].pop('retention_adapter')
    if a!=b:raise ValueError('Teacher-to-evaluation switch changed frozen model configuration')


@contextmanager
def borrowed(gpu,minimum=24576):
    from script.gpu_shared_queue import locked_registry,process_snapshot
    with locked_registry() as leases:
        lease=leases.get(gpu['reservation_id'])
        processes=process_snapshot()
        if (not lease or lease['capacity_mib']<minimum or lease['uuid']!=gpu['uuid']
                or processes.get(lease['pid'],{}).get('start')!=lease['start']):
            raise ValueError('Resident training lease is absent, stale or too small')
        ancestors={os.getpid()}
        pid=os.getpid()
        while pid in processes and processes[pid]['parent'] not in ancestors:
            pid=processes[pid]['parent'];ancestors.add(pid)
        if lease['pid'] not in ancestors:
            raise ValueError('Resident lease belongs to a different process tree')
    yield gpu


def train_loaded(backend,inputs):
    """Execute the unchanged optimizer lifecycle on the teacher's loaded base."""
    import torch
    out=Path(inputs['out']);gpu=inputs['gpu'];trainer=frozen(out)
    if not complete_labels(out):raise ValueError('Teacher labels incomplete')
    model,processor=backend.load('vlm')
    before_id=id(model);use_cache=model.config.use_cache
    old_loader,old_lease=trainer.training_model,trainer.lease
    def loaded(_):
        torch.cuda.set_per_process_memory_fraction(40960*1024**2/torch.cuda.get_device_properties(0).total_memory)
        return model,processor.tokenizer
    @contextmanager
    def held(out,folder,minimum=24576,allocator=22528):
        with borrowed(gpu,minimum):yield gpu
    trainer.training_model,trainer.lease=loaded,held
    try:
        trainer.train(out)
    finally:
        trainer.training_model,trainer.lease=old_loader,old_lease
    # Restore the same inference state that a fresh evaluation worker would use.
    model.gradient_checkpointing_disable()
    model.disable_input_require_grads()
    model.config.use_cache=use_cache
    model.requires_grad_(False);model.eval()
    for p in model.parameters():p.grad=None
    backend._retention_adapter_binding=read(out/'adapter/adapter_config.json')
    if id(backend.loaded['vlm'][0])!=before_id:raise ValueError('Training replaced resident base model')
    return dict(complete=True,pid=os.getpid(),model_identity=before_id,base_model_retained=True,
                adapter_weights_sha256=backend._retention_adapter_binding['weights_sha256'])


def validate_loaded(backend,inputs):
    """Repeat the original real-gradient gate on this resident teacher model."""
    import torch
    from script_core.q4_retention_training import LowRankLinear
    out=Path(inputs['out']);gpu=inputs['gpu'];trainer=frozen(out)
    model,processor=backend.load('vlm');use_cache=model.config.use_cache
    def loaded(_):
        torch.cuda.set_per_process_memory_fraction(40960*1024**2/torch.cuda.get_device_properties(0).total_memory)
        return model,processor.tokenizer
    @contextmanager
    def held(out,folder,minimum=24576,allocator=22528):
        with borrowed(gpu,minimum):yield gpu
    scope=dict(vars(trainer),training_model=loaded,lease=held)
    exec(compile((HERE/'resident_gate.py').read_text(),str(HERE/'resident_gate.py'),'exec'),scope)
    scope['preflight'](out)
    for name,module in list(model.named_modules()):
        if isinstance(module,LowRankLinear):
            parent,attr=name.rsplit('.',1);setattr(model.get_submodule(parent),attr,module.base)
    model.gradient_checkpointing_disable();model.disable_input_require_grads()
    model.config.use_cache=use_cache;model.requires_grad_(False);model.eval()
    for p in model.parameters():p.grad=None
    torch.cuda.empty_cache()
    torch.cuda.set_per_process_memory_fraction(22528*1024**2/torch.cuda.get_device_properties(0).total_memory)
    torch.cuda.reset_peak_memory_stats()
    return dict(passed=True,pid=os.getpid(),model_identity=id(model),base_model_retained=True,
                result=str(out/'validation/resident_gradient_gate/result.json'))


def set_evaluation_config(backend,cfg,inputs):
    import torch
    out=Path(inputs['out']);new=inputs['config'];compatible(cfg,new,out)
    backend.cfg=new
    # Training temporaries may be freed; model weights and the GPU lease stay.
    torch.cuda.empty_cache()
    cap=new['models']['cuda_allocator_limit_mib']
    torch.cuda.set_per_process_memory_fraction(cap*1024**2/torch.cuda.get_device_properties(0).total_memory)
    torch.cuda.reset_peak_memory_stats()
    return new,dict(configured=True,pid=os.getpid(),model_identity=id(backend.loaded['vlm'][0]),
                    allocator_mib=cap,loaded=sorted(backend.loaded))


def process_code(original):
    before='        operation, inputs, deadline = message\n'
    after=before+'''        if operation in ('__resident_training__','__resident_evaluation_config__','__resident_gradient_gate__'):
            from resident_train_q4 import train_loaded,set_evaluation_config,validate_loaded
            try:
                if operation == '__resident_training__':
                    result = train_loaded(backend, inputs)
                elif operation == '__resident_gradient_gate__':
                    result = validate_loaded(backend, inputs)
                else:
                    cfg,result = set_evaluation_config(backend,cfg,inputs)
                    allocator_limit_applied = True
                gpu_reported_until = time.monotonic()
                connection.send((True,result))
            except Exception as exc:
                connection.send((False,dict(message=type(exc).__name__+': '+str(exc),usage={})))
            continue
'''
    if original.count(before)!=1:raise ValueError('Frozen model-process layout changed')
    return original.replace(before,after,1)


def pipeline_process(connection,cfg):
    from script_core import budget
    sys.path.insert(0,str(HERE));scope=dict(vars(budget))
    exec(compile((HERE/'model_process.py').read_text(),str(HERE/'model_process.py'),'exec'),scope)
    scope['_worker'](connection,cfg)


def annotate_code(original):
    edits=[('backend=ModelWorker(cfg)','backend=SHARED_BACKEND'),
           ('backend.close(graceful=True)','pass # resident model remains loaded')]
    for before,after in edits:
        if original.count(before)!=1:raise ValueError('Frozen annotation layout changed')
        original=original.replace(before,after,1)
    compile(original,'resident-annotation','exec');return original


def evaluation_code(original):
    edits=[('backend=ModelWorker(cfg)','backend=SHARED_BACKEND'),
           ("lease=acquire(policy['resource_policy'],waiting)","lease=borrowed_evaluation_lease(policy['resource_policy'],waiting)"),
           ('gpu,_=lease.__enter__();entered=True','gpu=lease.__enter__();entered=True'),
           ('backend.close(graceful=True)','pass # resident model remains loaded'),
           ("os.environ.pop('VIDEOAGENT_GPU_LEASE',None)",'pass # outer lifetime owns the lease environment')]
    for before,after in edits:
        if original.count(before)!=1:raise ValueError('Frozen evaluation-worker layout changed')
        original=original.replace(before,after,1)
    compile(original,'resident-evaluation','exec');return original


def prepare_evaluation_code(original):
    before="    write(target/'experiment.json',policy)\n"
    after="    policy['resource_policy']['preference']=['0','1','2','3']\n    policy['allowed_gpu_indices']=['0','1','2','3']\n"+before
    if original.count(before)!=1:raise ValueError('Frozen evaluation preparation layout changed')
    return original.replace(before,after,1)


def prepare(out,control):
    trainer=frozen(out);trainer.verify(out)
    q=read(out/'queue_status.json')
    if q.get('stage')!='waiting_for_untrained500' or (out/'failure.json').exists():
        raise ValueError('Training must be waiting without formal work')
    if read(out/'teacher_claims.json') or any((out/n).exists() for n in ['teacher_workers','training','evaluation','adapter']):
        raise ValueError('Formal training pipeline has already started')
    if not read(out/'validation/real_gradient_gate/result.json')['passed']:
        raise ValueError('Original real-gradient gate must have passed')
    control.mkdir()
    write(control/'config.json',dict(out=str(out),control=str(control),old_controller=dict(pid=q['controller_pid'],
        identity=identity(q['controller_pid'])),allowed_gpu_indices=GPUS,teacher_workers=2,evaluation_workers=4,
        primary_admission_mib=43008,helper_admission_mib=24576,poll_seconds=10,
        residency='Same teacher model and lease through trained evaluation; primary trains its loaded model',
        may_run_alongside_untrained500=True,at=now()))
    (control/'resident_train_q4.py').write_text(Path(__file__).read_text())
    (control/'annotate.py').write_text(annotate_code(inspect.getsource(trainer.annotate)))
    (control/'prepare_evaluation.py').write_text(prepare_evaluation_code(inspect.getsource(trainer.prepare_evaluation)))
    qmod=trainer.runtime(out)
    (control/'evaluation_worker.py').write_text(evaluation_code(inspect.getsource(qmod.worker)))
    from script_core import budget
    (control/'model_process.py').write_text(process_code(inspect.getsource(budget._worker)))
    gate=inspect.getsource(trainer.preflight)
    if gate.count("folder=out/'validation/real_gradient_gate'")!=1:raise ValueError('Unexpected original gradient gate layout')
    (control/'resident_gate.py').write_text(gate.replace("folder=out/'validation/real_gradient_gate'","folder=out/'validation/resident_gradient_gate'",1))
    (control/'status.py').write_text(inspect.getsource(trainer.status).replace("allowed_gpu_indices=['0','1']","allowed_gpu_indices=['0','1','2','3']"))
    write(control/'queue_before.json',q)
    write(control/'frozen_hashes.json',{p.name:sha(p) for p in control.iterdir() if p.is_file()})
    print('RESIDENT_TRAINING_PREPARED',control,flush=True)


def worker(control,wid):
    verify_manifest(control);cfg=read(control/'config.json');out=Path(cfg['out']);trainer=frozen(out);trainer.verify(out)
    from script.gpu_queue import acquire
    from script_core import budget
    folder=control/'workers'/str(wid);folder.mkdir(parents=True,exist_ok=False)
    def status(stage,**kw):write(folder/'status.json',dict(stage=stage,pid=os.getpid(),at=now(),**kw))
    def check():
        if (out/'failure.json').exists():raise RuntimeError('Training pipeline halted; see failure.json')
    def wait_until(test,stage,**kw):
        while not test():
            check();status(stage,**kw);time.sleep(10)
    def evaluation_claimed():
        marker=control/'evaluation_ready.json'
        if not marker.exists():return False
        target=Path(read(marker)['target']);claims=read(target/'claims.json')
        return all(u in claims for u in read(target/'cohort.json'))
    def waiting(details):
        check()
        if wid>0 and evaluation_claimed():raise PipelineDrained()
        status('waiting_for_gpu',**details)
    minimum=43008 if wid==0 else 24576
    policy=dict(preference=GPUS,minimum_free_mib=minimum,maximum_utilization=100,stable_observations=3,admission_mode='memory_shared')
    backend=None
    try:
        with acquire(policy,waiting) as (gpu,_):
            if wid>0 and evaluation_claimed():raise PipelineDrained()
            gpu=dict(gpu,resident_capacity_mib=minimum)
            os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid'];os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
            budget._worker=pipeline_process
            backend=budget.ModelWorker(read(out/'teacher_config.json'))
            @contextmanager
            def held(out,folder,minimum=24576,allocator=22528):
                with borrowed(gpu,minimum):yield gpu
            if wid<2:
                if wid==0:
                    status('loading',gpu=gpu['index'])
                    started=time.monotonic();details=backend.call('prepare_vlm',{},600)
                    write(folder/'initial_setup.json',dict(seconds=time.monotonic()-started,details=details,gpu=gpu,at=now()))
                    status('resident_gradient_gate',gpu=gpu['index'],model_pid=backend.process.pid)
                    gate=backend.call(GATE,dict(out=str(out),gpu=gpu),900)
                    write(folder/'gradient_residency.json',dict(gate,gpu=gpu,at=now()))
                status('annotate',gpu=gpu['index'])
                scope=dict(vars(trainer),SHARED_BACKEND=backend,lease=held)
                exec(compile((control/'annotate.py').read_text(),str(control/'annotate.py'),'exec'),scope)
                scope['annotate'](out,wid)
                write(folder/'teacher_residency.json',dict(pid=backend.process.pid,gpu=gpu,at=now()))
                wait_until(lambda:complete_labels(out),'waiting_for_teacher_completion',gpu=gpu['index'],model_pid=backend.process.pid)
            else:
                started=time.monotonic();details=backend.call('prepare_vlm',{},600)
                write(folder/'initial_setup.json',dict(seconds=time.monotonic()-started,details=details,gpu=gpu,at=now()))
            if wid==0:
                status('training',gpu=gpu['index'],model_pid=backend.process.pid)
                result=backend.call(TRAIN,dict(out=str(out),gpu=gpu),7*24*3600)
                write(folder/'training_residency.json',dict(result,gpu=gpu,at=now()))
                write(control/'adapter_ready.json',dict(result,gpu=gpu,at=now()))
            wait_until(lambda:(control/'evaluation_ready.json').exists(),'waiting_for_trained_evaluation',gpu=gpu['index'],model_pid=backend.process.pid)
            target=Path(read(control/'evaluation_ready.json')['target'])
            evaluation_cfg=read(target/'qa_config.json')
            result=backend.call(CONFIG,dict(out=str(out),config=evaluation_cfg),60)
            backend.cfg=evaluation_cfg
            write(folder/'evaluation_residency.json',dict(result,gpu=gpu,at=now()))
            qmod=trainer.runtime(out)
            def eval_lease(policy,on_wait):return borrowed(gpu,policy['minimum_free_mib'])
            scope=dict(vars(qmod),SHARED_BACKEND=backend,borrowed_evaluation_lease=eval_lease)
            exec(compile((control/'evaluation_worker.py').read_text(),str(control/'evaluation_worker.py'),'exec'),scope)
            status('evaluation',gpu=gpu['index'],model_pid=backend.process.pid)
            scope['worker'](target,wid)
            status('complete',gpu=gpu['index'],model_pid=backend.process.pid)
            backend.close(graceful=True);backend=None
    except PipelineDrained:
        status('complete',reason='evaluation_queue_drained_before_admission')
    except BaseException as exc:
        write(out/'failure.json',dict(stage='resident_training',worker_id=wid,message=f'{type(exc).__name__}: {exc}',traceback=traceback.format_exc(),at=now()))
        status('failed',error=str(exc));raise
    finally:
        if backend is not None:backend.close(graceful=True)
        os.environ.pop('VIDEOAGENT_GPU_LEASE',None)


def supervise(control):
    verify_manifest(control);cfg=read(control/'config.json');out=Path(cfg['out']);trainer=frozen(out);trainer.verify(out)
    scope=dict(vars(trainer));exec(compile((control/'status.py').read_text(),str(control/'status.py'),'exec'),scope)
    status=scope['status'];old=cfg['old_controller'];rows=[];children={};target=None
    env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',CUDA_DEVICE_ORDER='PCI_BUS_ID',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
    with (control/'controller.lock').open('a') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (control/'launch.json').exists():raise ValueError('Resident trainer already started')
        if identity(old['pid'])!=old['identity']:raise ValueError('Old trainer identity changed')
        os.kill(old['pid'],signal.SIGSTOP)
        try:
            deadline=time.monotonic()+5
            while Path('/proc',str(old['pid']),'stat').read_text().rsplit(')',1)[1].split()[0] not in ['T','t']:
                if time.monotonic()>deadline:raise RuntimeError('Old training controller did not stop')
                time.sleep(.02)
            q=read(out/'queue_status.json')
            if q['stage']!='waiting_for_untrained500' or read(out/'teacher_claims.json'):
                raise ValueError('Training advanced before handoff')
            from script.gpu_shared_queue import process_snapshot,allocations,locked_registry
            processes=process_snapshot();descendants={old['pid']}
            while True:
                more=descendants|{pid for pid,p in processes.items() if p['parent'] in descendants}
                if more==descendants:break
                descendants=more
            if any(u['pid'] in descendants for u in allocations()):raise ValueError('Old trainer has CUDA work')
            with locked_registry() as leases:
                if any(l['pid'] in descendants for l in leases.values()):raise ValueError('Old trainer owns a GPU lease')
            status(out,'resident_handoff',resident_control=str(control))
            os.kill(old['pid'],signal.SIGTERM);os.kill(old['pid'],signal.SIGCONT)
        except BaseException:
            if identity(old['pid'])==old['identity']:os.kill(old['pid'],signal.SIGCONT)
            raise
        deadline=time.monotonic()+10
        while identity(old['pid']) is not None:
            if time.monotonic()>deadline:raise RuntimeError('Old trainer did not release controller lock')
            time.sleep(.1)
        with (out/'controller.lock').open('a') as ownership:
            fcntl.flock(ownership,fcntl.LOCK_EX|fcntl.LOCK_NB)
            write(control/'launch.json',dict(controller_pid=os.getpid(),identity=identity(os.getpid()),at=now()))
            def launch(wid):
                cmd=[sys.executable,'-u',str(control/'resident_train_q4.py'),'worker','--control',str(control),'--worker-id',str(wid)]
                with (control/f'worker-{wid}.log').open('x') as log:
                    p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env)
                row=dict(id=wid,pid=p.pid,identity=identity(p.pid),command=cmd)
                rows.append(row);children[p.pid]=p;write(control/'workers_launch.json',rows)
            try:
                launch(0);launch(1)
                while True:
                    statuses={str(r['id']):read(control/'workers'/str(r['id'])/'status.json') for r in rows if (control/'workers'/str(r['id'])/'status.json').exists()}
                    for r in rows:
                        code=children[r['pid']].poll()
                        if code is not None and code!=0:raise RuntimeError('Resident pipeline worker failed: '+str(r['id']))
                    if (out/'failure.json').exists():raise RuntimeError(read(out/'failure.json')['message'])
                    if target is None and (control/'adapter_ready.json').exists():
                        scope=dict(vars(trainer));exec(compile((control/'prepare_evaluation.py').read_text(),str(control/'prepare_evaluation.py'),'exec'),scope)
                        target=scope['prepare_evaluation'](out)
                        write(out/'evaluation_launch.json',dict(pid=os.getpid(),identity=identity(os.getpid()),resident_control=str(control),at=now()))
                        write(control/'evaluation_ready.json',dict(target=str(target),at=now()))
                        launch(2);launch(3)
                    phase='evaluation300' if target else 'training' if complete_labels(out) else 'annotate'
                    if target:
                        qmod=trainer.runtime(out)
                        with qmod.base.locked(target):report=qmod.build_report(target)
                        es={str(r['id']):read(target/'workers'/str(r['id'])/'status.json') for r in rows if (target/'workers'/str(r['id'])/'status.json').exists()}
                        live=any(p.poll() is None for p in children.values())
                        state='running' if live else 'complete' if report['complete'] else 'failed'
                        write(target/'queue_status.json',dict(state=state,controller_pid=os.getpid(),workers=es,resident_control=str(control),allowed_gpu_indices=GPUS,updated_at=now()))
                        if not live:
                            if state!='complete' or report['scheduler_failure']:raise RuntimeError('Trained evaluation incomplete')
                            trainer.compare(out);status(out,'complete',resident_control=str(control),workers=statuses)
                            report=read(out/'report.json');report.update(complete=True,comparison=read(out/'comparison.json'));write(out/'report.json',report)
                            q=read(out/'queue_status.json');q.update(state='complete');write(out/'queue_status.json',q);return
                    status(out,phase,resident_control=str(control),workers=statuses,resource_policy=dict(allowed_gpu_indices=GPUS,
                        primary_admission_mib=43008,helper_admission_mib=24576,continuous_model_and_lease=True,
                        independent_of_untrained500_completion=True))
                    report=read(out/'report.json')
                    report.update(resident_control=str(control),resident_initial_setup_seconds=sum(read(f)['seconds'] for f in (control/'workers').glob('*/initial_setup.json')),
                        resident_gradient_gate=read(out/'validation/resident_gradient_gate/result.json') if (out/'validation/resident_gradient_gate/result.json').exists() else None)
                    write(out/'report.json',report)
                    time.sleep(10)
            except BaseException as exc:
                if not (out/'failure.json').exists():write(out/'failure.json',dict(message=str(exc),traceback=traceback.format_exc(),at=now()))
                status(out,'failed',resident_control=str(control),error=str(exc));q=read(out/'queue_status.json');q['state']='failed';write(out/'queue_status.json',q);raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','supervise','worker']);p.add_argument('--control',type=Path,required=True)
    p.add_argument('--out',type=Path);p.add_argument('--worker-id',type=int)
    a=p.parse_args();control=a.control.resolve()
    if a.action=='prepare':prepare(a.out.resolve(),control)
    elif a.action=='supervise':supervise(control)
    else:worker(control,a.worker_id)
