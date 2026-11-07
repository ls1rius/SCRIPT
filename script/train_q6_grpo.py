"""Independent fixed3 Q6-GRPO8 experiment, warm-started from Q5 SFT."""
import argparse
import fcntl
import inspect
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.scale_live_parallel_pool import read, write, sha, now, identity, verify_manifest
from script import q6_parallel_rl as parallel
from script import train_q6_rl as old


def settings():
    return dict(seed=20261107, epochs=3, group_size=8, algorithm='GRPO clipped surrogate with group-standardized binary outcome reward',
        learning_rate=5e-6, weight_decay=0., betas=[.9,.999], gradient_clip=1.,
        epsilon=.1, temperature=1., clip_epsilon=.2, kl_coefficient=.02, advantage_epsilon=1e-4,
        num_iterations=2, rank=8, alpha=16, dropout=.05, rollout_dropout=False,
        max_sequence_length=2048, optimizer_initializations=1, adapter_initializations=1,
        internal_validation=False, selection='fixed final epoch3', training_questions=200, validation_questions=300,
        rollout_workers=5, questions_per_update=1, reward='Qplus correctness only: correct1 incorrect0',
        reference='frozen Q5 final epoch6 adapter', normalization='mean stochastic actions within trajectory, then mean8 trajectories',
        inference='same four-action argmax decoder as prior Q6; adapter only q3_filter',
        pilot_groups=4, pilot_policy='first4 committed formal training groups; no reset, extra update, or validation label',
        allowed_gpu_indices=['0','2'], primary_arm='Qplus', comparison_arms=['Q'])


def prepare(out, parent):
    if out.exists():
        raise ValueError('New experiment root required')
    verify_manifest(parent)
    source_result = read(parent/'training/result.json')
    if not source_result['complete'] or not read(parent/'report.json')['complete']:
        raise ValueError('Use completed Q6 as immutable runtime/data source')
    out.mkdir(parents=True)
    shutil.copytree(parent/'runtime', out/'runtime', ignore=shutil.ignore_patterns('__pycache__'))
    for name in ['script/train_q6_grpo.py','script_core/q6_grpo.py','script/gpu_queue.py','script/gpu_shared_queue.py']:
        shutil.copy2(ROOT/name, out/'runtime'/name)
    with (out/'runtime/script_core/q_v4.py').open('a') as f:
        f.write("\nVERSION='Q6-GRPO8-v1'\nPLUS_VERSION='Q6-GRPO8plus-v1'\n")
    for name in ['train_ids.json','test_ids.json','train_rewards.json']:
        shutil.copy2(parent/name, out/name)
    train, test = read(out/'train_ids.json'), read(out/'test_ids.json')
    if len(set(train)) != 200 or len(set(test)) != 300 or set(train)&set(test):
        raise ValueError('Original training/validation isolation required')
    # The old RL adapter is deliberately not the starting point: copy the original Q5 weights.
    shutil.copytree(parent/'initial_adapter', out/'initial_adapter')
    binding = read(out/'initial_adapter/adapter_config.json')
    if binding['epoch'] != 6 or binding['weights_sha256'] != sha(out/'initial_adapter/adapter.safetensors'):
        raise ValueError('Original Q5 SFT epoch6 checkpoint required')
    (out/'train_cases').symlink_to((parent/'train_cases').resolve(), target_is_directory=True)
    (out/'evaluation/runs').mkdir(parents=True)
    (out/'evaluation/cases').symlink_to((parent/'evaluation/cases').resolve(), target_is_directory=True)
    for name in ['cohort.json','evaluation_only.json']:
        shutil.copy2(parent/'evaluation'/name, out/'evaluation'/name)
    write(out/'evaluation/claims.json', {})
    cfg = read(parent/'training_model_config.json')
    cfg['models']['retention_adapter'] = str(out/'initial_adapter')
    write(out/'training_model_config.json', cfg)
    actor = json.loads(json.dumps(cfg)); actor['models']['cuda_allocator_limit_mib'] = 22528
    write(out/'rollout_model_config.json', actor)
    evaluation = json.loads(json.dumps(actor)); evaluation['models']['retention_adapter'] = str(out/'adapter')
    write(out/'evaluation/qa_config.json', evaluation)
    cfg = settings(); write(out/'training_config.json', cfg)
    write(out/'jobs.json', {})
    write(out/'experiment.json', dict(version='Q6-GRPO8-v1', plus_version='Q6-GRPO8plus-v1', primary_arm='Qplus',
        comparison_arms=['Q'], mode='adaptive', epochs=3, training_questions=200, validation_questions=300,
        rollouts_per_question_per_epoch=8, rollout_count=4800, parent_sft=read(parent/'experiment.json')['parent_sft'],
        runtime_data_parent=str(parent), initial_adapter_sha256=binding['weights_sha256'],
        allowed_gpu_indices=['0','2'], maximum_model_workers=6, training_algorithm=cfg['algorithm'],
        validation_scope='Original300 development validation, scoring after fixed3; not unseen holdout',
        resource_policy='Resident42GiB learner/40GiB pool, up to5 resident24GiB actors/22GiB pools; GPU0/2 only',at=now()))
    files = [f for f in out.rglob('*') if f.is_file() and '__pycache__' not in f.parts
             and str(f.relative_to(out)) not in ['jobs.json','evaluation/claims.json']]
    mapping = {str(f.relative_to(out)):sha(f) for f in files}
    for folder in ['train_cases','evaluation/cases']:
        for f in (out/folder).rglob('*'):
            if f.is_file(): mapping[str(f.relative_to(out))] = sha(f)
    write(out/'frozen_hashes.json', mapping)
    write(out/'queue_status.json', dict(state='prepared', stage='GRPO8_ready', controller_pid=None, updated_at=now()))
    print('GRPO8 PREPARED',out,flush=True)


def trainable_parameters(model):
    from script_core import q4_retention_training as sft
    params=sft.adapter_parameters(model)
    if len(params) != 112 or sum(p.numel() for p in params) != 2523136:
        raise ValueError('Wrong trainable LoRA parameter set')
    return params


def learn(backend, inputs):
    import torch
    from script_core import q6_rl as rl, q6_grpo as grpo, q4_retention_training as sft
    from script.q_v4_validation import validate_live
    from script_core.physical_cuda_pool import status as pool_status
    out = Path(inputs['out']); folder = out/'training'; folder.mkdir(exist_ok=True)
    cfg = read(out/'training_config.json'); ids = read(out/'train_ids.json'); rewards = read(out/'train_rewards.json')
    if len(ids) != 200 or set(ids) != set(rewards) or cfg['group_size'] != 8:
        raise ValueError('GRPO8 original training reward isolation failed')
    model,_ = backend.load('vlm'); model.eval(); model.gradient_checkpointing_disable(); model.config.use_cache=True
    params = trainable_parameters(model)
    reference = {n:p.detach().clone() for n,p in model.named_parameters() if n.endswith(('lora_A','lora_B'))}
    optimizer = torch.optim.AdamW(params, lr=cfg['learning_rate'], betas=tuple(cfg['betas']), weight_decay=0.)
    backend._q6_policy_version=0; backend._q6_sampling=False
    def progress(stage, **detail):
        write(folder/'status.json',dict(stage=stage,pid=os.getpid(),gpu=inputs['gpu']['index'],at=now(),**detail))
    progress('semantic_gate')
    validate_live(old.LocalProxy(backend), backend.cfg, out/'validation/learner_initial')
    write(out/'validation/physical_pool.json',pool_status())
    binding=read(out/'initial_adapter/adapter_config.json')
    policy=dict(path=str(out/'initial_adapter'),sha256=binding['weights_sha256'],version=0)
    rng=random.Random(cfg['seed']); updates=0; pg_updates=0; groups=0; mixed_groups=0; history=[]; seconds=0.; start=time.monotonic()
    for epoch in range(1,cfg['epochs']+1):
        order=list(ids);rng.shuffle(order); epoch_rewards=[]; began=time.monotonic()
        for index,uid in enumerate(order,1):
            behavior_version=updates; tickets=[]
            for r in range(cfg['group_size']):
                key,job=parallel.dispatch(out,epoch,uid,r,updates,policy,cfg,{})
                tickets.append((key,job))
            progress('parallel_rollouts',epoch=epoch,epochs=3,question=index,questions=200,completed_groups=groups,
                     optimizer_steps=updates,policy_gradient_updates=pg_updates,group_size=8,behavior_version=behavior_version)
            batch=[]
            for key,job in tickets:
                result=parallel.await_job(out,key,job,backend,updates); target=Path(result['folder'])
                if sha(target/'policy_trace.json') != result['trace_sha256'] or sha(target/'result.json') != result['result_sha256']:
                    raise ValueError('Rollout artifacts changed')
                trajectory=read(target/'result.json'); trace=read(target/'policy_trace.json')
                if trace['policy_version'] != behavior_version or trace['policy_weights_sha256'] != policy['sha256']:
                    raise ValueError('Stale GRPO behavior policy')
                if not trajectory['complete'] or trajectory['error'] or list(target.rglob('*.pending.json')):
                    raise ValueError('Incomplete GRPO rollout')
                reward=rl.outcome_reward(uid,trajectory['arms']['Qplus']['answer']['selected_option'],rewards)
                batch.append(dict(reward=reward,traces=trace['traces'],folder=str(target)))
                seconds+=trajectory['cost_summary']['total']
            if len(batch) != 8 or len({d['folder'] for d in batch}) != 8:
                raise ValueError('GRPO group is not eight independent trajectories')
            adv, reward_info=grpo.advantages([d['reward'] for d in batch],cfg['advantage_epsilon'])
            signal=not reward_info['zero_variance']; mixed_groups+=signal
            iteration_rows=[]
            for iteration in range(cfg['num_iterations']):
                optimizer.zero_grad(set_to_none=True); model.eval()
                diagnostics=dict(actions=0,clipped_actions=0,pg_loss=0.,kl_loss=0.,max_behavior_drift=0.,ratio_min=float('inf'),ratio_max=0.)
                progress('gradient_update',epoch=epoch,question=index,completed_groups=groups,optimizer_steps=updates,
                         policy_gradient_updates=pg_updates,group_size=8,iteration=iteration+1,reward_mean=reward_info['mean'],reward_std=reward_info['std'])
                with sft.enabled(model,True):
                    for trajectory,advantage in zip(batch,adv):
                        count=sum(len(t['actions']) for t in trajectory['traces'])
                        if count <= 0: raise ValueError('No stochastic filter actions')
                        for trace in trajectory['traces']:
                            with rl.reference_weights(model,reference),torch.no_grad():
                                ref=rl.cached_decision_logits(model,trace)
                            logits=rl.cached_decision_logits(model,trace,checkpoint=True)
                            losses,metrics=grpo.objective(logits,ref,trace['actions'],trace['old_logps'],advantage,
                                cfg['epsilon'],cfg['temperature'],cfg['clip_epsilon'],cfg['kl_coefficient'])
                            if iteration == 0 and metrics['max_behavior_drift'] > .05:
                                raise ValueError('GRPO behavior likelihood mismatch before any update: '+str(metrics['max_behavior_drift']))
                            loss=losses.sum()/(8*count);loss.backward()
                            diagnostics['pg_loss']+=metrics['pg_sum']/(8*count)
                            diagnostics['kl_loss']+=cfg['kl_coefficient']*metrics['kl_sum']/(8*count)
                            diagnostics['actions']+=metrics['actions'];diagnostics['clipped_actions']+=metrics['clipped_actions']
                            diagnostics['max_behavior_drift']=max(diagnostics['max_behavior_drift'],metrics['max_behavior_drift'])
                            diagnostics['ratio_min']=min(diagnostics['ratio_min'],metrics['ratio_min']);diagnostics['ratio_max']=max(diagnostics['ratio_max'],metrics['ratio_max'])
                if any(p.grad is not None for n,p in model.named_parameters() if not n.endswith(('lora_A','lora_B'))):
                    raise ValueError('Base gradients present')
                if not all(p.grad is None or torch.isfinite(p.grad).all() for p in params):
                    raise ValueError('Nonfinite adapter gradients')
                nonzero=any(p.grad is not None and torch.count_nonzero(p.grad).item() for p in params)
                if signal and not nonzero:raise ValueError('Reward group produced no actual gradient')
                diagnostics['gradient_norm']=float(torch.nn.utils.clip_grad_norm_(params,cfg['gradient_clip'])) if nonzero else 0.
                if nonzero:
                    optimizer.step();updates+=1;pg_updates+=int(signal);backend._q6_policy_version=updates
                    if not all(torch.isfinite(p).all() for p in params):raise ValueError('Nonfinite updated adapter')
                diagnostics.update(iteration=iteration+1,optimizer_step_applied=bool(nonzero),optimizer_steps=updates,
                                   clip_fraction=diagnostics['clipped_actions']/diagnostics['actions'])
                iteration_rows.append(diagnostics)
                if signal and not (folder/'first_gradient_check.json').exists():
                    write(folder/'first_gradient_check.json',dict(passed=True,algorithm=cfg['algorithm'],epoch=epoch,question_id=uid,
                        rewards=[d['reward'] for d in batch],advantages=adv,iteration=iteration+1,optimizer_steps=updates,
                        gradient_norm=diagnostics['gradient_norm'],finite_nonzero_adapter_gradient=True,base_gradients_absent=True,
                        max_behavior_drift=diagnostics['max_behavior_drift'],group_size=8,at=now()))
            optimizer.zero_grad(set_to_none=True)
            if any(d['optimizer_step_applied'] for d in iteration_rows):
                checkpoint=folder/'policies'/f'v{updates:06}'
                saved=sft.save_adapter(model,checkpoint,dict(base_model=backend.cfg['models']['vlm'],policy_version=updates,epoch=epoch,purpose='GRPO8 committed group'))
                policy=dict(path=str(checkpoint),sha256=saved['weights_sha256'],version=updates)
                torch.save(dict(optimizer=optimizer.state_dict(),epoch=epoch,next_offset=index,completed_groups=groups+1,
                    optimizer_steps=updates,policy_gradient_updates=pg_updates),checkpoint/'optimizer.pt')
            groups+=1;epoch_rewards.extend(d['reward'] for d in batch)
            write(folder/'groups'/f'{groups:04}.json',dict(epoch=epoch,question=index,question_id=uid,group_size=8,
                behavior_version=behavior_version,rewards=[d['reward'] for d in batch],advantages=adv,reward_statistics=reward_info,
                optimizer_steps=updates,policy_gradient_updates=pg_updates,iterations=iteration_rows,trajectories=[d['folder'] for d in batch],at=now()))
            if groups == cfg['pilot_groups']:
                write(out/'validation/pilot.json',dict(passed=True,formal_prefix_groups=groups,rollouts=groups*8,mixed_reward_groups=mixed_groups,
                    zero_variance_group_fraction=1-mixed_groups/groups,optimizer_steps=updates,policy_gradient_updates=pg_updates,
                    first_reward_gradient_observed=(folder/'first_gradient_check.json').exists(),
                    decision='Functional integration passed; continue same optimizer and committed prefix. No efficacy claim.',at=now()))
            progress('group_complete',epoch=epoch,question=index,completed_groups=groups,optimizer_steps=updates,
                     policy_gradient_updates=pg_updates,mixed_reward_groups=mixed_groups,zero_variance_group_fraction=1-mixed_groups/groups)
        history.append(dict(epoch=epoch,mean_reward=sum(epoch_rewards)/len(epoch_rewards),rollouts=len(epoch_rewards),
                            optimizer_steps=updates,policy_gradient_updates=pg_updates,seconds=time.monotonic()-began))
        write(folder/'history.json',history)
    if not (folder/'first_gradient_check.json').exists():raise ValueError('No genuine outcome-driven gradient in full GRPO run')
    for param in model.parameters():param.grad=None
    final=sft.save_adapter(model,out/'adapter',dict(base_model=backend.cfg['models']['vlm'],epoch=3,epochs=3,policy_version=updates,
        algorithm=cfg['algorithm'],reward_arm='Qplus',selection='fixed final epoch3',training_questions=200,validation_questions_used_for_gradients=0))
    backend._retention_adapter_binding=final
    result=dict(complete=True,algorithm=cfg['algorithm'],epochs=3,completed_groups=groups,group_size=8,rollouts=groups*8,
        optimizer_steps=updates,policy_gradient_updates=pg_updates,mixed_reward_groups=mixed_groups,
        zero_variance_group_fraction=1-mixed_groups/groups,rollout_processing_seconds=seconds,wall_seconds=time.monotonic()-start,
        adapter_initializations=1,optimizer_initializations=1,adapter=final,physical_pool=pool_status(),at=now())
    write(folder/'result.json',result);progress('training_complete',epochs=3,optimizer_steps=updates)
    return result


def dispatch(backend, operation, inputs):
    if operation == parallel.TRAIN:return learn(backend,inputs)
    return parallel.dispatch_control(backend,operation,inputs)


def model_process(connection,cfg):
    from script_core import budget
    source=inspect.getsource(budget._worker);anchor='        operation, inputs, deadline = message\n'
    extra=anchor+'''        if operation in ('__q6_parallel_train__','__q6_parallel_rollout__','__q6_policy_load__','__q6_evaluation_config__'):
            try:
                value = _dispatch(backend,operation,inputs)
                connection.send((True,value))
            except Exception as exc:
                connection.send((False,dict(message=str(exc),usage={},raw_output=dict(traceback=__import__('traceback').format_exc()))))
            continue
'''
    if source.count(anchor)!=1:raise ValueError('Model worker layout changed')
    scope=dict(vars(budget),_dispatch=dispatch)
    exec(compile(source.replace(anchor,extra,1),'<GRPO8-worker>','exec'),scope)
    scope['_worker'](connection,cfg)


def worker_once(out,wid):
    from script.gpu_queue import acquire
    from script_core import budget
    from script.q_v4_validation import validate_live
    from script.run_q_v4_experiment import run_case
    from script import run_q_experiment as base
    verify_manifest(out); folder=out/'workers'/str(wid); folder.mkdir(parents=True,exist_ok=True)
    def status(stage,**detail):write(folder/'status.json',dict(stage=stage,pid=os.getpid(),at=now(),**detail))
    def waiting(detail):
        if (out/'failure.json').exists():raise RuntimeError('GRPO halted')
        if (out/'training/result.json').exists() and len(read(out/'evaluation/claims.json'))==300:raise old.ValidationDrained()
        status('waiting_for_gpu',**detail)
    backend=None
    try:
        policy=dict(preference=['0','2'],minimum_free_mib=43008 if wid==0 else 24576,stable_observations=3,admission_mode='memory_shared',maximum_utilization=100)
        with acquire(policy,waiting) as (gpu,_):
            try:
                os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid'];os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
                cfg=read(out/('training_model_config.json' if wid==0 else 'rollout_model_config.json'))
                budget._worker=model_process;backend=budget.ModelWorker(cfg);status('loading',gpu=gpu['index'])
                try:setup=backend.call('prepare_vlm',{},600)
                except Exception as exc:
                    if parallel.startup_allocation_busy(exc,wid):
                        write(folder/'allocation_failures'/f'{time.time_ns()}.json',dict(error=str(exc),formal_job_claimed=False,at=now()))
                        raise parallel.StartupPoolBusy() from exc
                    raise
                write(folder/'setup.json',dict(gpu=gpu,details=setup,at=now()))
                if wid==0:
                    write(out/'learner_admitted.json',dict(gpu=gpu,at=now()))
                    status('learning',gpu=gpu['index'],model_pid=backend.process.pid)
                    result=backend.call(parallel.TRAIN,dict(out=str(out),gpu=gpu),14*24*3600)
                    write(out/'adapter_ready.json',dict(result=result,at=now()))
                else:
                    status('actor_gate',gpu=gpu['index'],model_pid=backend.process.pid)
                    validate_live(backend,cfg,folder/'initial_validation');current=-1
                    while not (out/'adapter_ready.json').exists():
                        if (out/'failure.json').exists():raise RuntimeError('Another GRPO worker failed')
                        item=parallel.claim_job(out,wid)
                        if item is None:
                            status('actor_ready',gpu=gpu['index'],model_pid=backend.process.pid,policy_version=current);time.sleep(.5);continue
                        key,job=item
                        if current!=job['version']:
                            backend.call(parallel.LOAD,job['policy'],120);current=job['version']
                        status('rollout',gpu=gpu['index'],model_pid=backend.process.pid,job=key,policy_version=current)
                        parallel.publish_job(out,key,backend.call(parallel.ROLLOUT,job,3600))
                    final=read(out/'adapter/adapter_config.json')
                    backend.call(parallel.LOAD,dict(path=str(out/'adapter'),sha256=final['weights_sha256'],version=final['policy_version']),120)
                cfg=read(out/'evaluation/qa_config.json');backend.call(parallel.CONFIG,dict(config=cfg),60);backend.cfg=cfg
                status('evaluation_gate',gpu=gpu['index'],model_pid=backend.process.pid)
                validate_live(backend,cfg,folder/'final_validation')
                while not (out/'failure.json').exists():
                    item=base.claim(out/'evaluation',wid)
                    if item is None:break
                    index,uid=item;case=out/'evaluation/cases'/uid
                    def progress(phase,**detail):status('query',gpu=gpu['index'],model_pid=backend.process.pid,index=index,question_id=uid,phase=phase,**detail)
                    run_case(backend,cfg,read(case/'question.json'),read(case/'sentences.json'),read(case/'original_qa.json'),
                             out/'evaluation/attempts'/uid,read(case/'archive.json'),'adaptive',progress)
                    base.publish(out/'evaluation',uid,out/'evaluation/attempts'/uid)
                status('complete',gpu=gpu['index'])
            finally:
                if backend is not None:backend.close(graceful=True)
    except parallel.StartupPoolBusy:raise
    except old.ValidationDrained:status('complete',reason='queue_drained_before_admission')
    except BaseException as exc:parallel.fail(out,wid,exc);status('failed',error=str(exc));raise


def worker(out,wid):
    while True:
        try:return worker_once(out,wid)
        except parallel.StartupPoolBusy:time.sleep(10)


def supervise(out):
    verify_manifest(out)
    if os.environ.get('VIDEOAGENT_PHYSICAL_CUDA_POOL')!='1':raise ValueError('Physical pool required')
    children={};rows=[]
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Already launched')
        write(out/'launch.json',dict(pid=os.getpid(),identity=identity(os.getpid()),at=now()))
        def launch(wid):
            cmd=[sys.executable,'-u',str(out/'runtime/script/train_q6_grpo.py'),'worker','--out',str(out),'--worker-id',str(wid)]
            env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',CUDA_DEVICE_ORDER='PCI_BUS_ID')
            with (out/f'worker-{wid}.log').open('x') as log:child=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env)
            children[wid]=child;rows.append(dict(id=wid,pid=child.pid,identity=identity(child.pid)));write(out/'workers_launch.json',rows)
        launch(0)
        while True:
            if (out/'learner_admitted.json').exists() and len(children)==1 and not (out/'failure.json').exists():
                for wid in range(1,6):launch(wid)
            for wid,child in children.items():
                if child.poll() not in (None,0) and not (out/'failure.json').exists():parallel.fail(out,wid,RuntimeError('Worker exited '+str(child.returncode)))
            states={str(w):read(out/'workers'/str(w)/'status.json') for w in children if (out/'workers'/str(w)/'status.json').exists()}
            failure=read(out/'failure.json') if (out/'failure.json').exists() else None
            trained=(out/'training/result.json').exists();evaluation=old.evaluation_report(out) if trained else None
            live=any(c.poll() is None for c in children.values());complete=bool(evaluation and evaluation['complete'] and not live and not failure)
            if not live and not complete and not failure:
                parallel.fail(out,'controller',RuntimeError('Exited before completion'));failure=read(out/'failure.json')
            stage='failed' if failure else 'complete' if complete else 'validation300' if trained else 'GRPO8_training'
            write(out/'queue_status.json',dict(state='failed' if failure else 'complete' if complete else 'running',stage=stage,
                controller_pid=os.getpid(),workers=states,allowed_gpu_indices=['0','2'],user_reserved_gpu_indices=['1','3'],maximum_model_workers=6,
                primary_arm='Qplus',group_size=8,updated_at=now()))
            write(out/'report.json',dict(stage=stage,complete=complete,primary_arm='Qplus',epochs=3,group_size=8,
                training_progress=read(out/'training/status.json') if (out/'training/status.json').exists() else None,
                training=read(out/'training/result.json') if trained else None,evaluation=evaluation,failure=failure,updated_at=now()))
            if not live:return
            time.sleep(5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','supervise','worker'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--parent',type=Path);parser.add_argument('--worker-id',type=int)
    args=parser.parse_args();out=args.out.resolve()
    if args.action=='prepare':prepare(out,args.parent.resolve())
    elif args.action=='supervise':supervise(out)
    else:worker(out,args.worker_id)
