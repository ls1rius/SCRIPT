"""All200, one LoRA initialization, fixed six epochs, final300 validation."""
import argparse
from contextlib import contextmanager
import fcntl
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
import traceback


class ValidationDrained(Exception):
    pass


def train(out):
    from script import train_q4_retention as runner
    from script_core import q4_retention_training as t
    import torch
    runner.verify(out)
    read,write,sha=runner.read,runner.write,runner.sha
    settings=read(out/'training_config.json');ids=read(out/'train_ids.json');validation_ids=read(out/'test_ids.json')
    if len(set(ids))!=len(ids) or set(ids)&set(validation_ids):raise ValueError('Training/300 validation overlap')
    epochs=settings['epochs']
    if epochs!=6:raise ValueError('This frozen experiment requires exactly six epochs')
    samples=[]
    for uid in ids:
        if read(out/'teacher_claims.json')[uid]['state']!='complete':raise ValueError('Teacher labels incomplete')
        rows=read(out/'teacher_runs'/uid/'samples.json')
        if any(s['question_id']!=uid for s in rows):raise ValueError('Foreign training sample')
        samples.extend(rows)
    folder=out/'training';folder.mkdir(exist_ok=False)
    write(folder/'dataset_manifest.json',{str((out/'teacher_runs'/u/'samples.json').relative_to(out)):sha(out/'teacher_runs'/u/'samples.json') for u in ids})
    with runner.lease(out,folder,minimum=43008,allocator=40960) as gpu:
        began=time.monotonic();torch.manual_seed(settings['seed']);random.seed(settings['seed'])
        model,tok=runner.training_model(out);setup=time.monotonic()-began
        torch.manual_seed(settings['seed'])
        encoded=[dict(t.encode_example(tok,s['request'],s['target'],settings['max_sequence_length']),question_id=s['question_id']) for s in samples]
        modules=runner.configure_gradients(model,settings)  # The only formal LoRA initialization.
        params=t.adapter_parameters(model);counts,weights=runner.class_balance(encoded)
        write(folder/'dataset_statistics.json',dict(samples=len(encoded),training_questions=len(ids),internal_validation_samples=0,
            maximum_tokens=max(e['input_tokens'] for e in encoded),label_counts=counts,class_weights=weights,
            module_count=len(modules),trainable_parameters=sum(p.numel() for p in params),validation300_used_for_gradients=False))
        optimizer=torch.optim.AdamW(params,lr=settings['learning_rate'],betas=tuple(settings['betas']),weight_decay=settings['weight_decay'])
        steps_per_epoch=math.ceil(len(encoded)/settings['accumulation']);total_steps=steps_per_epoch*epochs
        warmup=max(1,math.ceil(total_steps*settings['warmup_fraction']))
        def scale(step):
            if step<warmup:return (step+1)/warmup
            return .5*(1+math.cos(math.pi*min(1.,(step-warmup)/max(1,total_steps-warmup))))
        scheduler=torch.optim.lr_scheduler.LambdaLR(optimizer,scale)
        rng=random.Random(settings['seed']);history=[];tokens=supervised=microsteps=0;first_gradient_checked=False
        for epoch in range(1,epochs+1):
            order=list(range(len(encoded)));rng.shuffle(order);model.train();optimizer.zero_grad(set_to_none=True)
            total_loss=0.;epoch_start=time.monotonic()
            with t.enabled(model,True):
                for position,i in enumerate(order):
                    sample=encoded[i];group_start=(position//settings['accumulation'])*settings['accumulation']
                    group_size=min(settings['accumulation'],len(order)-group_start)
                    loss=t.loss_for(model,sample,weights)
                    if not torch.isfinite(loss):raise ValueError('Nonfinite training loss')
                    (loss/group_size).backward();total_loss+=float(loss.detach())
                    if not first_gradient_checked:
                        if not any(p.grad is not None and torch.count_nonzero(p.grad).item() for p in params):raise ValueError('No adapter gradient')
                        if any(p.grad is not None for n,p in model.named_parameters() if not n.endswith(('lora_A','lora_B'))):raise ValueError('Base received gradients')
                        write(folder/'first_gradient_check.json',dict(passed=True,formal_microstep=1,extra_training_steps=0,
                            adapter_initializations=1,finite_nonzero_adapter_gradient=True,base_gradients_absent=True,at=runner.now()))
                        first_gradient_checked=True
                    tokens+=sample['input_tokens'];supervised+=sample['supervised_tokens'];microsteps+=1
                    if (position+1)%settings['accumulation']==0 or position+1==len(order):
                        torch.nn.utils.clip_grad_norm_(params,settings['gradient_clip'],error_if_nonfinite=True)
                        optimizer.step();scheduler.step();optimizer.zero_grad(set_to_none=True)
                    if position%10==0:
                        write(folder/'status.json',dict(stage='training',phase='fixed6',epoch=epoch,epochs=epochs,
                            sample=position+1,samples=len(order),optimizer_steps=scheduler.last_epoch,input_tokens=tokens,
                            supervised_tokens=supervised,elapsed_seconds=time.monotonic()-began,pid=os.getpid(),gpu=gpu['index'],at=runner.now()))
            row=dict(epoch=epoch,train_loss=total_loss/len(encoded),seconds=time.monotonic()-epoch_start,
                     input_tokens=tokens,supervised_tokens=supervised,optimizer_steps=scheduler.last_epoch)
            history.append(row);write(folder/'history.json',history);print('FIXED_EPOCH',row,flush=True)
        model.eval()
        binding=t.save_adapter(model,out/'adapter',dict(base_model=read(out/'base_qa_config.json')['models']['vlm'],
            epoch=epochs,epochs=epochs,selection='fixed final epoch; no checkpoint selection or refit',
            training_questions=len(ids),train_question_ids_sha256=sha(out/'train_ids.json'),
            training_dataset_manifest_sha256=sha(folder/'dataset_manifest.json'),validation_questions_used_for_gradients=0,
            training_config_sha256=sha(out/'training_config.json'),adapter_initializations=1))
        torch.cuda.synchronize()
        write(folder/'result.json',dict(complete=True,epochs=epochs,final_epoch=epochs,adapter_initializations=1,
            optimizer_initializations=1,training_questions=len(ids),internal_validation_questions=0,refit=False,
            setup_seconds=setup,wall_seconds=time.monotonic()-began,training_forward_input_tokens=tokens,
            supervised_tokens=supervised,microsteps=microsteps,optimizer_steps=scheduler.last_epoch,
            validation_forward_input_tokens=0,peak_allocated_mib=torch.cuda.max_memory_allocated()/1024**2,
            adapter=binding,gpu=gpu,at=runner.now()))


def boot(out):
    sys.path.insert(0,str(out/'runtime'));sys.path.insert(0,str(out/'control'))


def prepare(out,old):
    # Capture current support before boot(old) selects its frozen modules.
    from script.physical_pool_preparation import stage_support
    boot(old)
    from script.scale_live_parallel_pool import read,write,sha,now
    from script import train_q4_retention as runner
    runner.verify(old)
    control=out/'control';control.mkdir()
    shutil.copytree(old/'runtime',out/'runtime',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(Path(__file__),out/'runtime/script/train_q4_fixed_epochs.py')
    for name in ['train_ids.json','test_ids.json','teacher_claims.json','base_qa_config.json']:
        shutil.copy2(old/name,out/name)
    (out/'teacher_runs').symlink_to(old/'teacher_runs',target_is_directory=True)
    settings=read(old/'training_config.json')
    for key in ['select','refit','max_epochs']:settings.pop(key,None)
    settings.update(epochs=6,internal_validation=False,selection='fixed final epoch6',adapter_initializations=1,
                    class_balance='inverse square-root frequency over all200, clipped [0.5,3]')
    write(out/'training_config.json',settings)
    cfg=read(old/'teacher_config.json');cfg['models']['retention_training_ids']=str(out/'train_ids.json');write(out/'teacher_config.json',cfg)
    policy=read(old/'experiment.json');policy.update(version='Q4-trained-fixed6-v1',plus_version='Q4plus-trained-fixed6-v1',
        primary_arm='Qplus',comparison_arms=['Q'],
        internal_training=200,internal_validation=0,validation_questions=300,epochs=6,selection='final_epoch6',refit=False,
        allowed_gpu_indices=['0','1','2','3'],resource_policy='Resident primary42GiB and evaluation helpers24GiB; four GPU shared-memory admission',
        evaluation_scope='User-designated original300 development validation, repeatedly evaluated; not unseen holdout.',
        label_scope='All200 teacher labels reused; 300 answers are scoring-only and never used in gradient training',
        historical_training_root=str(old),at=now())
    write(out/'experiment.json',policy)
    parent=Path(policy['parent']);shutil.copy2(parent/'qa_expected_artifacts.json',out/'qa_expected_artifacts.json')
    write(out/'inherited_teacher.json',dict(source=str(old),processing_seconds=read(old/'report.json')['teacher_processing_seconds'],
        files={str(p):sha(p) for p in (old/'teacher_runs').rglob('*') if p.is_file()},relabeling=False))
    (out/'validation').mkdir()
    source=old/'resident-20261106-r1'
    code=(source/'resident_train_q4.py').read_text()
    before='        trainer.train(out)';after='        from script.train_q4_fixed_epochs import train\n        train(out)'
    if code.count(before)!=1:raise ValueError('Unexpected resident training function')
    (control/'resident_train_q4.py').write_text(code.replace(before,after,1))
    shutil.copy2(source/'model_process.py',control/'model_process.py')
    shutil.copy2(old/'recovery-20261106-r2/evaluation_worker.py',control/'evaluation_worker.py')
    code=(source/'prepare_evaluation.py').read_text().replace('all200 refit after 180/20 epoch selection; no test metric used','all200 continuous six epochs; final epoch6; no checkpoint selection')
    code=code.replace('Q4-adaptive-trained-retention-v1','Q4-trained-fixed6-v1').replace('Q4plus-adaptive-trained-retention-v1','Q4plus-trained-fixed6-v1')
    (control/'prepare_evaluation.py').write_text(code)
    with (out/'runtime/script_core/q_v4.py').open('a') as f:f.write("\nVERSION='Q4-trained-fixed6-v1'\nPLUS_VERSION='Q4plus-trained-fixed6-v1'\n")
    write(control/'config.json',dict(out=str(out),old=str(old),allowed_gpu_indices=['0','1','2','3']))
    if os.environ.get('VIDEOAGENT_PHYSICAL_CUDA_POOL') == '1':
        stage_support(out/'runtime', [control/'model_process.py'])
    files=[p for p in (out/'runtime').rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    files.extend(out/n for n in ['train_ids.json','test_ids.json','teacher_claims.json','base_qa_config.json','teacher_config.json','training_config.json','experiment.json','inherited_teacher.json'])
    files.extend(out/'teacher_runs'/u/'samples.json' for u in read(out/'train_ids.json'))
    write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in files})
    write(control/'frozen_hashes.json',{p.name:sha(p) for p in control.iterdir() if p.is_file()})
    write(out/'queue_status.json',dict(state='prepared',stage='fixed6_ready',controller_pid=None,allowed_gpu_indices=['0','1','2','3'],updated_at=now()))


def worker(out,wid):
    boot(out)
    from script.scale_live_parallel_pool import read,write,now,verify_manifest
    from script.gpu_queue import acquire
    from script_core import budget
    import resident_train_q4 as resident
    from script import train_q4_retention as runner
    runner.verify(out);verify_manifest(out/'control')
    folder=out/'control/workers'/str(wid);folder.mkdir(parents=True)
    def status(stage,**kw):write(folder/'status.json',dict(stage=stage,pid=os.getpid(),at=now(),**kw))
    def waiting(d):
        if (out/'failure.json').exists():raise RuntimeError('Pipeline halted')
        if wid>0 and len(read(out/'evaluation/claims.json'))==len(read(out/'test_ids.json')):
            raise ValidationDrained()
        status('waiting_for_gpu',**d)
    policy=dict(preference=['0','1','2','3'],minimum_free_mib=43008 if wid==0 else 24576,
                maximum_utilization=100,stable_observations=3,admission_mode='memory_shared')
    backend=None
    try:
        with acquire(policy,waiting) as (gpu,_):
            os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid'];os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
            cfg=read(out/'teacher_config.json') if wid==0 else read(out/'evaluation/qa_config.json')
            budget._worker=resident.pipeline_process;backend=budget.ModelWorker(cfg)
            status('loading',gpu=gpu['index']);began=time.monotonic();details=backend.call('prepare_vlm',{},600)
            write(folder/'setup.json',dict(seconds=time.monotonic()-began,gpu=gpu,details=details))
            if wid==0:
                status('training',gpu=gpu['index'],model_pid=backend.process.pid)
                result=backend.call(resident.TRAIN,dict(out=str(out),gpu=gpu),7*24*3600)
                write(out/'control/adapter_ready.json',dict(result,gpu=gpu,at=now()))
                while not (out/'control/evaluation_ready.json').exists():
                    if (out/'failure.json').exists():raise RuntimeError('Pipeline halted')
                    time.sleep(2)
                new=read(out/'evaluation/qa_config.json');details=backend.call(resident.CONFIG,dict(out=str(out),config=new),60);backend.cfg=new
                write(folder/'resident_switch.json',dict(details,gpu=gpu,at=now()))
            target=out/'evaluation';qmod=runner.runtime(out)
            def held(policy,on_wait):return resident.borrowed(gpu,policy['minimum_free_mib'])
            scope=dict(vars(qmod),SHARED_BACKEND=backend,borrowed_evaluation_lease=held)
            exec(compile((out/'control/evaluation_worker.py').read_text(),str(out/'control/evaluation_worker.py'),'exec'),scope)
            status('validation300',gpu=gpu['index'],model_pid=backend.process.pid);scope['worker'](target,wid)
            status('complete',gpu=gpu['index'])
    except ValidationDrained:
        status('complete',reason='validation_queue_drained_before_admission')
    except BaseException as exc:
        write(out/'failure.json',dict(worker_id=wid,message=str(exc),traceback=traceback.format_exc(),at=now()));status('failed',error=str(exc));raise
    finally:
        if backend is not None:backend.close(graceful=True)


def supervise(out):
    boot(out)
    from script.scale_live_parallel_pool import read,write,now,identity,verify_manifest
    from script import train_q4_retention as runner
    runner.verify(out);verify_manifest(out/'control');rows=[];children={};target=None
    env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',CUDA_DEVICE_ORDER='PCI_BUS_ID')
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Already launched')
        write(out/'launch.json',dict(pid=os.getpid(),identity=identity(os.getpid()),at=now()))
        def launch(wid):
            cmd=[sys.executable,'-u',str(out/'runtime/script/train_q4_fixed_epochs.py'),'worker','--out',str(out),'--worker-id',str(wid)]
            with (out/f'worker-{wid}.log').open('x') as log:p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env)
            rows.append(dict(id=wid,pid=p.pid,identity=identity(p.pid),command=cmd));children[p.pid]=p;write(out/'workers_launch.json',rows)
        launch(0)
        while True:
            statuses={str(r['id']):read(out/'control/workers'/str(r['id'])/'status.json') for r in rows if (out/'control/workers'/str(r['id'])/'status.json').exists()}
            for r in rows:
                if children[r['pid']].poll() not in (None,0) and not (out/'failure.json').exists():write(out/'failure.json',dict(message='Worker failed',worker=r,at=now()))
            if target is None and (out/'control/adapter_ready.json').exists() and not (out/'failure.json').exists():
                scope=dict(vars(runner));exec((out/'control/prepare_evaluation.py').read_text(),scope);target=scope['prepare_evaluation'](out)
                write(out/'control/evaluation_ready.json',dict(target=str(target),at=now()))
                for wid in [1,2,3]:launch(wid)
            evaluation=None
            if target:
                qmod=runner.runtime(out)
                with qmod.base.locked(target):evaluation=qmod.build_report(target)
                for r in rows:
                    f=target/'workers'/str(r['id'])/'status.json'
                    if f.exists():statuses[str(r['id'])]=read(f)
            failure=read(out/'failure.json') if (out/'failure.json').exists() else None
            live=any(p.poll() is None for p in children.values())
            complete=bool(evaluation and evaluation['complete'] and not evaluation['scheduler_failure'] and not failure and not live)
            if not live and not complete and not failure:
                failure=dict(message='Fixed6 pipeline exited without completing training and all300 validation',at=now())
                write(out/'failure.json',failure)
            stage='failed' if failure else 'complete' if complete else 'validation300' if target else 'training_fixed6'
            state='failed' if failure else 'complete' if complete else 'running'
            if target:
                write(target/'queue_status.json',dict(state=state,controller_pid=os.getpid(),workers=statuses,
                    allowed_gpu_indices=['0','1','2','3'],updated_at=now()))
            write(out/'queue_status.json',dict(state=state,stage=stage,controller_pid=os.getpid(),workers=statuses,allowed_gpu_indices=['0','1','2','3'],updated_at=now()))
            write(out/'report.json',dict(stage=stage,complete=complete,epochs=6,selection='fixed final epoch6',internal_validation_questions=0,
                primary_arm='Qplus',comparison_arms=['Q'],
                teacher_dependency=read(out/'inherited_teacher.json')['processing_seconds'],
                training=read(out/'training/result.json') if (out/'training/result.json').exists() else None,
                training_progress=read(out/'training/status.json') if (out/'training/status.json').exists() else None,
                evaluation=evaluation,failure=failure,updated_at=now()))
            if not live:
                if complete:runner.compare(out)
                return
            time.sleep(10)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','supervise','worker']);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--old',type=Path);p.add_argument('--worker-id',type=int);a=p.parse_args();out=a.out.resolve()
    if a.action=='prepare':prepare(out,a.old.resolve())
    elif a.action=='supervise':supervise(out)
    else:worker(out,a.worker_id)
