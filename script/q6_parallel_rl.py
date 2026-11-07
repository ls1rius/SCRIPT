"""Q6 recovery: exact cached policy replay and synchronous parallel rollouts."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script.scale_live_parallel_pool import read,write,sha,now,identity,verify_manifest
from script import train_q6_rl as old

TRAIN='__q6_parallel_train__';ROLLOUT='__q6_parallel_rollout__';LOAD='__q6_policy_load__';CONFIG=old.CONFIG


class StartupPoolBusy(Exception):pass


def startup_allocation_busy(exc,wid):
    message=str(exc)
    return wid>0 and 'out of memory' in message and 'videoagent_install_pool' in message


@contextmanager
def locked(out):
    with (out/'jobs.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);yield


def fail(out,wid,exc):
    with locked(out):
        if not (out/'failure.json').exists():
            write(out/'failure.json',dict(worker_id=wid,message=str(exc) or type(exc).__name__,traceback=traceback.format_exc(),at=now()))


def prepare(out,parent,epochs=None,gpus=None,actor_workers=None,handoff=None):
    if out.exists():raise ValueError('New recovery root required')
    handoff=handoff or parent/'handoff_checkpoint.json'
    resume=read(handoff) if handoff.exists() else None
    if not resume and (read(parent/'training/status.json')['optimizer_steps']!=0 or (parent/'training/first_gradient_check.json').exists()):
        raise ValueError('This recovery supports the proven zero-update prefix only')
    old.verify_manifest(parent)
    out.mkdir(parents=True)
    shutil.copytree(parent/'runtime',out/'runtime',ignore=shutil.ignore_patterns('__pycache__'))
    for name in ['script_core/q6_rl.py','script/q6_parallel_rl.py','script/gpu_queue.py','script/gpu_shared_queue.py']:
        shutil.copy2(ROOT/name,out/'runtime'/name)
    for name in ['train_ids.json','test_ids.json','train_rewards.json']:
        shutil.copy2(parent/name,out/name)
    for name in ['train_cases','initial_adapter']:
        (out/name).symlink_to((parent/name).resolve(),target_is_directory=True)
    if resume:
        origin=Path(resume['checkpoint'])
        if sha(origin/'adapter.safetensors')!=resume['adapter_sha256'] or sha(origin/'optimizer.pt')!=resume['optimizer_sha256']:
            raise ValueError('Handoff checkpoint changed')
        shutil.copytree(origin,out/'resume_state')
        write(out/'resume_checkpoint.json',resume)
        shutil.copy2(parent/'training/first_gradient_check.json',out/'inherited_first_gradient_check.json')
    (out/'evaluation').mkdir()
    (out/'evaluation/runs').mkdir()
    (out/'evaluation/cases').symlink_to((parent/'evaluation/cases').resolve(),target_is_directory=True)
    for name in ['cohort.json','evaluation_only.json']:
        shutil.copy2(parent/'evaluation'/name,out/'evaluation'/name)
    write(out/'evaluation/claims.json',{})
    cfg=read(parent/'training_model_config.json');cfg['models']['retention_adapter']=str(out/'initial_adapter')
    write(out/'training_model_config.json',cfg)
    actor=json.loads(json.dumps(cfg));actor['models']['cuda_allocator_limit_mib']=22528
    if resume:actor['models']['retention_adapter']=str(out/'resume_state')
    write(out/'rollout_model_config.json',actor)
    evaluation=json.loads(json.dumps(actor));evaluation['models']['retention_adapter']=str(out/'adapter')
    write(out/'evaluation/qa_config.json',evaluation)
    settings=read(parent/'training_config.json');settings.update(rollout_workers=7,questions_per_update=4,adaptive_question_batch=True,
        gradient_replay='identical incremental KV chunks; reentrant whole-window checkpoint; model.eval dropout off',
        recovery_parent=str(parent),optimizer_state='Restore complete committed AdamW state' if resume else 'No previous optimizer step occurred; equivalent empty AdamW state')
    if epochs is not None:
        if type(epochs) is not int or epochs<1:raise ValueError('Positive fixed RL epoch count required')
        if resume and resume['epoch']>epochs:raise ValueError('Requested epoch is behind committed training')
        settings['epochs']=epochs;settings['selection']=f'fixed final epoch{epochs}'
    if actor_workers is not None:settings['rollout_workers']=actor_workers
    write(out/'training_config.json',settings)
    inherited=read(parent/'inherited_rollouts.json') if resume else {}
    frozen=read(parent/'inherited_artifact_hashes.json') if resume else {}
    for f in (parent/'training/rollouts').glob('epoch-*/*/*/policy_trace.json'):
        target=f.parent;trace=read(f);result=read(target/'result.json')
        if not result['complete'] or result['error'] or list(target.rglob('*.pending.json')):raise ValueError('Incomplete inherited rollout')
        if not resume and trace['policy_version']!=0:raise ValueError('Inherited rollout used an updated policy')
        epoch=int(target.parent.parent.name.split('-')[-1]);uid=target.parent.name;r=int(target.name)
        key=f'{epoch}/{uid}/{r}'
        inherited[key]=dict(folder=str(target.resolve()),trace_sha256=sha(f),result_sha256=sha(target/'result.json'),
                            processing_seconds=result['cost_summary']['total'])
        for path in target.rglob('*'):
            if path.is_file():frozen[str(path.resolve())]=sha(path)
    if not resume and len(inherited)!=12:raise ValueError('Expected all12 completed saved rollouts')
    groups=[]
    for f in sorted((parent/'training/groups').glob('*.json')):
        row=read(f)
        if not resume and (row['optimizer_steps'] or any(row['advantages'])):raise ValueError('Prefix is not zero-update')
        groups.append(dict(file=str(f.resolve()),sha256=sha(f),row=row))
    if len(groups)!=(resume['completed_groups'] if resume else 5):raise ValueError('Committed group count mismatch')
    settings['recovery_prefix_groups']=len(groups);write(out/'training_config.json',settings)
    write(out/'inherited_history.json',read(parent/'training/history.json') if (parent/'training/history.json').exists() else [])
    write(out/'inherited_rollouts.json',inherited);write(out/'inherited_groups.json',groups)
    write(out/'inherited_artifact_hashes.json',frozen)
    experiment=read(parent/'experiment.json');experiment.update(recovery_parent=str(parent),
        resource_policy='Resident42GiB learner + up to7 resident24GiB actors, all GPU0123; adaptive synchronous batches, up to4 questions',
        total_model_workers=8,rollout_workers=7,evaluation_workers=8,questions_per_update='adaptive1..4',
        probability_repair='Match behavior KV chunks and no-grad forward via reentrant checkpoint; original.05 stop retained',at=now())
    experiment.update(epochs=settings['epochs'],rollout_count=len(read(out/'train_ids.json'))*settings['epochs']*2,
        total_model_workers=1+settings['rollout_workers'],rollout_workers=settings['rollout_workers'],evaluation_workers=1+settings['rollout_workers'])
    if gpus is not None:
        if not gpus or any(v not in ['0','1','2','3'] for v in gpus):raise ValueError('Invalid GPU mask')
        experiment['allowed_gpu_indices']=gpus
        experiment['resource_policy']=f"GPU{','.join(gpus)} only; resident42GiB learner and{settings['rollout_workers']} resident24GiB actors"
    write(out/'experiment.json',experiment)
    write(out/'jobs.json',{})
    immutable=[p for p in out.rglob('*') if p.is_file() and '__pycache__' not in p.parts
        and str(p.relative_to(out)) not in ['jobs.json','evaluation/claims.json']]
    # rglob does not recurse directory symlinks; freeze every externally inherited input explicitly.
    mapping={str(p.relative_to(out)):sha(p) for p in immutable}
    for folder in ['train_cases','initial_adapter','evaluation/cases']:
        for p in (out/folder).rglob('*'):
            if p.is_file():mapping[str(p.relative_to(out))]=sha(p)
    write(out/'frozen_hashes.json',mapping)
    write(out/'queue_status.json',dict(state='prepared',stage='parallel_RL_ready',controller_pid=None,updated_at=now()))
    print('Q6 PARALLEL PREPARED',out,flush=True)


def load_policy(backend,inputs):
    import torch
    from safetensors.torch import load_file
    folder=Path(inputs['path']);cfg=read(folder/'adapter_config.json')
    if sha(folder/'adapter.safetensors')!=inputs['sha256'] or cfg['weights_sha256']!=inputs['sha256']:
        raise ValueError('Policy checkpoint hash mismatch')
    model,_=backend.load('vlm')
    parameters={n:p for n,p in model.named_parameters() if n.endswith(('lora_A','lora_B'))}
    tensors=load_file(str(folder/'adapter.safetensors'),device='cpu')
    if set(parameters)!=set(tensors):raise ValueError('Policy tensor names mismatch')
    with torch.no_grad():
        for n,p in parameters.items():
            if p.shape!=tensors[n].shape or not torch.isfinite(tensors[n]).all():raise ValueError('Invalid policy tensor')
            p.copy_(tensors[n])
    backend._q6_policy_version=inputs['version'];backend._retention_adapter_binding=cfg;model.eval()
    return dict(loaded=True,version=inputs['version'],weights_sha256=inputs['sha256'],model_identity=id(model))


def rollout(backend,job):
    import torch
    from script.run_q_v4_experiment import run_case
    out=Path(job['out']);uid=job['uid'];case=out/'train_cases'/uid;target=out/'training/rollouts'/f"epoch-{job['epoch']}"/uid/str(job['rollout'])
    if backend._q6_policy_version!=job['version']:raise ValueError('Actor has stale policy')
    if any(sha(case/n)!=h for n,h in read(case/'source_binding.json')['files'].items()):raise ValueError('Source input changed')
    backend._q6_sampling=True;backend._q6_traces=[]
    backend._q6_generator=torch.Generator(device=backend.load('vlm')[0].device).manual_seed(job['seed'])
    try:
        result=run_case(old.LocalProxy(backend),backend.cfg,read(case/'question.json'),read(case/'sentences.json'),
            read(case/'original_qa.json'),target,read(case/'archive.json'),'adaptive')
    finally:backend._q6_sampling=False
    if not backend._q6_traces:raise ValueError('Missing actor policy trace')
    write(target/'policy_trace.json',dict(question_id=uid,policy_version=job['version'],seed=job['seed'],
        policy_weights_sha256=job['policy']['sha256'],traces=backend._q6_traces))
    backend._q6_traces=None
    return dict(folder=str(target),version=job['version'],processing_seconds=result['cost_summary']['total'],
        trace_sha256=sha(target/'policy_trace.json'),result_sha256=sha(target/'result.json'))


def claim_job(out,wid):
    with locked(out):
        if (out/'failure.json').exists() or (out/'adapter_ready.json').exists():return None
        jobs=read(out/'jobs.json')
        for key,job in jobs.items():
            if job['state']=='pending':
                job.update(state='running',worker_id=wid,pid=os.getpid(),claimed_at=now());write(out/'jobs.json',jobs)
                return key,job
    return None


def publish_job(out,key,result):
    with locked(out):
        jobs=read(out/'jobs.json')
        if jobs[key]['state']!='running':raise ValueError('Duplicate actor publication')
        jobs[key].update(state='complete',result=result,completed_at=now());write(out/'jobs.json',jobs)


def dispatch(out,epoch,uid,r,version,policy,settings,inherited):
    key=f'{epoch}/{uid}/{r}'
    if version==0 and key in inherited:
        result=inherited[key]
        folder=Path(result['folder'])
        if sha(folder/'policy_trace.json')!=result['trace_sha256'] or sha(folder/'result.json')!=result['result_sha256']:
            raise ValueError('Inherited rollout changed')
        return key,dict(state='complete',result=result,inherited=True,version=0)
    seed=int(hashlib.sha256(f"{settings['seed']}/{epoch}/{uid}/{r}".encode()).hexdigest()[:15],16)
    job=dict(out=str(out),epoch=epoch,uid=uid,rollout=r,version=version,policy=policy,seed=seed,state='pending')
    with locked(out):
        jobs=read(out/'jobs.json')
        if key in jobs:raise ValueError('Duplicate rollout job')
        jobs[key]=job;write(out/'jobs.json',jobs)
    return key,job


def await_job(out,key,initial,backend,version):
    if initial['state']=='complete':return initial['result']
    began=time.monotonic()
    while True:
        if (out/'failure.json').exists():raise RuntimeError('Parallel actor failed: '+read(out/'failure.json')['message'])
        with locked(out):job=read(out/'jobs.json')[key]
        if job['version']!=version:raise ValueError('Off-policy reply')
        if job['state']=='complete':return job['result']
        # If no actor can be admitted, the learner performs pending work itself.
        if time.monotonic()-began>90 and job['state']=='pending':
            claimed=claim_job(out,0)
            if claimed:
                chosen,payload=claimed
                publish_job(out,chosen,rollout(backend,payload))
                continue
        time.sleep(.2)


def learn(backend,inputs):
    import torch
    from script_core import q6_rl as rl,q4_retention_training as sft
    from script.q_v4_validation import validate_live
    from script_core.physical_cuda_pool import status as pool_status
    out=Path(inputs['out']);folder=out/'training';folder.mkdir(exist_ok=True)
    settings=read(out/'training_config.json');ids=read(out/'train_ids.json');rewards=read(out/'train_rewards.json')
    inherited=read(out/'inherited_rollouts.json');prefix=read(out/'inherited_groups.json')
    if len(ids)!=200 or set(ids)!=set(rewards):raise ValueError('Training reward isolation failed')
    model,_=backend.load('vlm');model.eval();model.gradient_checkpointing_disable();model.config.use_cache=True
    modules=[m for m in model.modules() if isinstance(m,sft.LowRankLinear)]
    if len(modules)!=56:raise ValueError('Unexpected LoRA module count')
    params=sft.adapter_parameters(model);reference={n:p.detach().clone() for n,p in model.named_parameters() if n.endswith(('lora_A','lora_B'))}
    optimizer=torch.optim.AdamW(params,lr=settings['learning_rate'],betas=tuple(settings['betas']),weight_decay=0)
    backend._q6_policy_version=0;backend._q6_sampling=False
    resume=read(out/'resume_checkpoint.json') if (out/'resume_checkpoint.json').exists() else None
    restored=None
    if resume:
        restored=torch.load(out/'resume_state/optimizer.pt',map_location='cpu',weights_only=False)
        optimizer.load_state_dict(restored['optimizer'])
        load_policy(backend,dict(path=str(out/'resume_state'),sha256=resume['adapter_sha256'],version=resume['optimizer_steps']))
        shutil.copy2(out/'inherited_first_gradient_check.json',folder/'first_gradient_check.json')
        write(out/'validation/optimizer_restore.json',dict(passed=True,optimizer_steps=restored['optimizer_steps'],
            optimizer_state_entries=len(optimizer.state),completed_groups=restored['completed_groups'],
            policy_sha256=resume['adapter_sha256'],model_identity=id(model),at=now()))
    def progress(stage,**kw):write(folder/'status.json',dict(stage=stage,pid=os.getpid(),gpu=inputs['gpu']['index'],at=now(),**kw))
    progress('semantic_gate');validate_live(old.LocalProxy(backend),backend.cfg,out/'validation/learner_initial')
    write(out/'validation/physical_pool.json',pool_status())
    # Validate every saved action on the same cached path before any optimizer step.
    progress('inherited_likelihood_gate');maximum=0.;checked=0;gate_start=time.monotonic()
    with torch.no_grad(),sft.enabled(model,True):
        for entry in inherited.values():
            if resume:break  # Saved behavior belongs to earlier versions, already validated and hash-bound.
            payload=read(Path(entry['folder'])/'policy_trace.json')
            for trace in payload['traces']:
                logits=rl.cached_decision_logits(model,trace)
                actions=torch.tensor(trace['actions'],device=model.device)
                logp,_=rl.policy_terms(logits,logits,actions,settings['epsilon'],settings['temperature'])
                drift=float((logp-torch.tensor(trace['old_logps'],device=model.device)).abs().max())
                maximum=max(maximum,drift);checked+=len(trace['actions'])
                if drift>.05:raise ValueError(f'Cached inherited policy drift: {drift}')
    if resume:
        from script.q_v4_validation import synthetic_case
        from script_core.q_repair import windows
        question,_,_,sources=synthetic_case();request=windows(question['question'],sources)[0]
        packet=dict(operation='q3_filter',scope=dict(question_id='q6-resume-gate',condition='original_question_only'))
        packet['items']=[dict(source_id='W0',scope=packet['scope'],request=request)]
        output=old.LocalProxy(backend).call('q4_independent_batch',packet,150)
        trace=output['items'][0]['q6_action_trace']
        with torch.no_grad(),sft.enabled(model,True):
            logits=rl.cached_decision_logits(model,trace)
            logp,_=rl.policy_terms(logits,logits,torch.tensor(trace['actions'],device=model.device),settings['epsilon'],settings['temperature'])
            maximum=float((logp-torch.tensor(trace['old_logps'],device=model.device)).abs().max());checked=len(trace['actions'])
            if maximum>.05:raise ValueError('Restored policy cached likelihood gate failed')
    write(out/'validation/inherited_likelihood_gate.json',dict(passed=True,maximum_drift=maximum,actions=checked,seconds=time.monotonic()-gate_start,
        optimizer_steps=restored['optimizer_steps'] if restored else 0,scope='Current restored policy synthetic cache check; old raw artifacts verified separately' if resume else 'All saved initial-policy actions',at=now()))
    rng=random.Random(settings['seed']);updates=restored['optimizer_steps'] if restored else 0
    policy_updates=restored['policy_gradient_updates'] if restored else 0;groups=len(prefix)
    history=read(out/'inherited_history.json');new_seconds=0.;old_seconds=0.
    for entry in prefix:write(folder/'groups'/Path(entry['file']).name,entry['row'])
    initial=read(out/'initial_adapter/adapter_config.json')
    policy=dict(path=str(out/'resume_state'),sha256=resume['adapter_sha256'],version=updates) if resume else dict(path=str(out/'initial_adapter'),sha256=initial['weights_sha256'],version=0)
    first=bool(resume and policy_updates);train_tokens=ref_tokens=0;start=time.monotonic()
    epochs=settings['epochs']
    for epoch in range(1,epochs+1):
        order=list(ids);rng.shuffle(order)
        if epoch<=len(history):continue
        if restored and epoch<restored['epoch']:continue
        offset=restored['next_offset'] if restored and epoch==restored['epoch'] else len(prefix) if not restored and epoch==1 else 0
        epoch_rewards=[r for entry in prefix if entry['row']['epoch']==epoch for r in entry['row']['rewards']];epoch_start=time.monotonic()
        while offset<len(order):
            # Recover the failed genuine-reward group alone, then batch two questions.
            if not resume and groups==len(prefix):size=1
            elif settings.get('adaptive_question_batch'):
                ready=0
                for status_file in (out/'workers').glob('*/status.json'):
                    if status_file.parent.name=='0':continue
                    state=read(status_file)
                    if state['stage'] in ['actor_ready','rollout']:ready+=1
                size=min(settings['questions_per_update'],max(1,(ready+1)//2))
            else:size=settings['questions_per_update']
            selected=order[offset:offset+size];tickets=[]
            for uid in selected:
                for r in range(2):
                    key,job=dispatch(out,epoch,uid,r,updates,policy,settings,inherited)
                    tickets.append((uid,r,key,job))
            progress('parallel_rollouts',epoch=epoch,epochs=epochs,question=offset+1,questions=200,
                batch_questions=len(selected),completed_groups=groups,optimizer_steps=updates,policy_gradient_updates=policy_updates)
            batch={uid:[] for uid in selected}
            for uid,r,key,job in tickets:
                result=await_job(out,key,job,backend,updates);target=Path(result['folder'])
                if sha(target/'policy_trace.json')!=result['trace_sha256'] or sha(target/'result.json')!=result['result_sha256']:raise ValueError('Published trajectory changed')
                trajectory=read(target/'result.json');trace=read(target/'policy_trace.json')
                if trace['policy_version']!=updates or not trajectory['complete'] or trajectory['error']:raise ValueError('Off-policy or incomplete rollout')
                reward=rl.outcome_reward(uid,trajectory['arms']['Qplus']['answer']['selected_option'],rewards)
                batch[uid].append(dict(reward=reward,traces=trace['traces'],folder=str(target)))
                if job.get('inherited'):old_seconds+=trajectory['cost_summary']['total']
                else:new_seconds+=trajectory['cost_summary']['total']
            optimizer.zero_grad(set_to_none=True);model.eval();drift_max=0.;pg_sum=kl_sum=0.
            signals={uid:rl.loo_advantages([d['reward'] for d in batch[uid]]) for uid in selected}
            has_signal=any(a for values in signals.values() for a in values)
            count=sum(len(t['actions']) for uid in selected for d in batch[uid] for t in d['traces'])
            progress('gradient_update',epoch=epoch,epochs=epochs,question=offset+1,questions=200,
                completed_groups=groups,optimizer_steps=updates,policy_gradient_updates=policy_updates)
            if has_signal or updates:
                with sft.enabled(model,True):
                    for uid in selected:
                        for trajectory,advantage in zip(batch[uid],signals[uid]):
                            for trace in trajectory['traces']:
                                with rl.reference_weights(model,reference),torch.no_grad():ref=rl.cached_decision_logits(model,trace)
                                logits=rl.cached_decision_logits(model,trace,checkpoint=True)
                                actions=torch.tensor(trace['actions'],device=model.device)
                                logp,kl=rl.policy_terms(logits,ref,actions,settings['epsilon'],settings['temperature'])
                                drift=float((logp.detach()-torch.tensor(trace['old_logps'],device=model.device)).abs().max())
                                drift_max=max(drift_max,drift)
                                if drift>.05:raise ValueError(f'Exact cached checkpoint likelihood mismatch: {drift}')
                                pg=-advantage*logp.sum()/(2*len(selected)*settings['policy_gradient_normalizer'])
                                regularizer=settings['kl_coefficient']*kl.sum()/count
                                loss=pg+regularizer
                                if not torch.isfinite(loss):raise ValueError('Nonfinite RL loss')
                                loss.backward();pg_sum+=float(pg.detach());kl_sum+=float(regularizer.detach())
                                train_tokens+=len(trace['input_ids']);ref_tokens+=len(trace['input_ids'])
                if any(p.grad is not None for n,p in model.named_parameters() if not n.endswith(('lora_A','lora_B'))):raise ValueError('Base gradients present')
                if not all(p.grad is None or torch.isfinite(p.grad).all() for p in params):raise ValueError('Nonfinite adapter gradient')
                nonzero=any(p.grad is not None and torch.count_nonzero(p.grad).item() for p in params)
                if has_signal and not nonzero:raise ValueError('No actual reward gradient')
                if nonzero:
                    norm=float(torch.nn.utils.clip_grad_norm_(params,settings['gradient_clip']))
                    optimizer.step();updates+=1;policy_updates+=int(has_signal);backend._q6_policy_version=updates
                    checkpoint=folder/'policies'/f'v{updates:06}'
                    saved=sft.save_adapter(model,checkpoint,dict(base_model=backend.cfg['models']['vlm'],policy_version=updates,epoch=epoch,purpose='synchronous actor handoff and crash recovery'))
                    policy=dict(path=str(checkpoint),sha256=saved['weights_sha256'],version=updates)
                    torch.save(dict(optimizer=optimizer.state_dict(),epoch=epoch,next_offset=offset+len(selected),completed_groups=groups+len(selected),
                        optimizer_steps=updates,policy_gradient_updates=policy_updates,python_rng=rng.getstate()),checkpoint/'optimizer.pt')
                    if has_signal and not first:
                        write(folder/'first_gradient_check.json',dict(passed=True,epoch=epoch,question_ids=selected,
                            rewards={uid:[t['reward'] for t in batch[uid]] for uid in selected},advantages=signals,
                            gradient_norm=norm,maximum_likelihood_drift=drift_max,finite_nonzero_adapter_gradient=True,
                            base_gradients_absent=True,optimizer_steps=updates,adapter_initializations=1,optimizer_initializations=1,
                            behavior_replay='identical cached chunks with reentrant checkpoint',at=now()))
                        first=True
                    if resume and not (folder/'first_resumed_gradient_check.json').exists():
                        write(folder/'first_resumed_gradient_check.json',dict(passed=True,epoch=epoch,question_ids=selected,
                            optimizer_steps=updates,previous_optimizer_steps=resume['optimizer_steps'],
                            finite_nonzero_adapter_gradient=True,base_gradients_absent=True,maximum_likelihood_drift=drift_max,
                            policy_signal=bool(has_signal),state_restored=True,at=now()))
            optimizer.zero_grad(set_to_none=True)
            for uid in selected:
                groups+=1;terminal=[d['reward'] for d in batch[uid]];epoch_rewards.extend(terminal)
                write(folder/'groups'/f'{groups:04}.json',dict(epoch=epoch,question=offset+1,question_id=uid,rewards=terminal,advantages=signals[uid],
                    optimizer_steps=updates,policy_gradient_updates=policy_updates,batch_questions=len(selected),pg_loss=pg_sum,kl_loss=kl_sum,
                    max_logprob_drift=drift_max,trajectories=[d['folder'] for d in batch[uid]],at=now()))
                offset+=1
            progress('group_complete',epoch=epoch,epochs=epochs,question=offset,questions=200,completed_groups=groups,
                optimizer_steps=updates,policy_gradient_updates=policy_updates,mean_reward=sum(epoch_rewards)/len(epoch_rewards))
        history.append(dict(epoch=epoch,mean_reward=sum(epoch_rewards)/len(epoch_rewards),rollouts=len(epoch_rewards),optimizer_steps=updates,
            policy_gradient_updates=policy_updates,seconds=time.monotonic()-epoch_start));write(folder/'history.json',history)
    if not first or not policy_updates:raise ValueError('No genuine reward-driven training')
    model.eval();model.config.use_cache=True
    for p in model.parameters():p.grad=None
    final=sft.save_adapter(model,out/'adapter',dict(base_model=backend.cfg['models']['vlm'],epoch=epochs,epochs=epochs,policy_version=updates,
        algorithm=settings['algorithm'],reward_arm='Qplus',selection=f'fixed final epoch{epochs}',training_questions=200,validation_questions_used_for_gradients=0))
    backend._retention_adapter_binding=final
    result=dict(complete=True,epochs=epochs,completed_groups=groups,rollouts=len(ids)*epochs*2,optimizer_steps=updates,policy_gradient_updates=policy_updates,
        inherited_completed_groups=len(prefix),inherited_rollouts=len(inherited),inherited_consumed_processing_seconds=old_seconds,
        new_rollout_processing_seconds=new_seconds,wall_seconds=time.monotonic()-start,training_forward_input_tokens=train_tokens,
        reference_forward_input_tokens=ref_tokens,adapter_initializations=1,optimizer_initializations=1,physical_pool=pool_status(),adapter=final,at=now())
    write(folder/'result.json',result);progress('training_complete',epochs=epochs,optimizer_steps=updates)
    return result


def dispatch_control(backend,operation,inputs):
    if operation==TRAIN:return learn(backend,inputs)
    if operation==ROLLOUT:return rollout(backend,inputs)
    if operation==LOAD:return load_policy(backend,inputs)
    if operation==CONFIG:
        import torch
        backend.cfg=inputs['config'];torch.cuda.empty_cache()
        torch.cuda.set_per_process_memory_fraction(22528*1024**2/torch.cuda.get_device_properties(0).total_memory)
        backend._retention_adapter_binding=read(Path(backend.cfg['models']['retention_adapter'])/'adapter_config.json')
        return dict(configured=True,pid=os.getpid())
    raise ValueError('Unknown control')


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
    if source.count(anchor)!=1:raise ValueError('Worker layout changed')
    scope=dict(vars(budget),_dispatch=dispatch_control);exec(compile(source.replace(anchor,extra,1),'<parallel-Q6-worker>','exec'),scope)
    scope['_worker'](connection,cfg)


def worker_once(out,wid):
    from script.gpu_queue import acquire
    from script_core import budget
    from script.q_v4_validation import validate_live
    from script.run_q_v4_experiment import run_case
    from script import run_q_experiment as base
    verify_manifest(out)
    folder=out/'workers'/str(wid);folder.mkdir(parents=True,exist_ok=True)
    def status(stage,**kw):write(folder/'status.json',dict(stage=stage,pid=os.getpid(),at=now(),**kw))
    def waiting(detail):
        if (out/'failure.json').exists():raise RuntimeError('Parallel RL halted')
        if (out/'training/result.json').exists() and len(read(out/'evaluation/claims.json'))==300:raise old.ValidationDrained()
        status('waiting_for_gpu',**detail)
    backend=None
    try:
        policy=dict(preference=read(out/'experiment.json')['allowed_gpu_indices'],minimum_free_mib=43008 if wid==0 else 24576,stable_observations=3,
            admission_mode='memory_shared',maximum_utilization=100)
        with acquire(policy,waiting) as (gpu,_):
            os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid'];os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
            cfg=read(out/('training_model_config.json' if wid==0 else 'rollout_model_config.json'))
            budget._worker=model_process;backend=budget.ModelWorker(cfg)
            status('loading',gpu=gpu['index']);began=time.monotonic()
            try:setup=backend.call('prepare_vlm',{},600)
            except Exception as exc:
                if startup_allocation_busy(exc,wid):
                    write(folder/'allocation_failures'/f'{time.time_ns()}.json',dict(gpu=gpu,error=str(exc),seconds=time.monotonic()-began,
                        formal_job_claimed=False,at=now()))
                    status('allocation_wait',gpu=gpu['index'],reason='Physical pool unavailable before model load; release and retry admission')
                    raise StartupPoolBusy() from exc
                raise
            write(folder/'setup.json',dict(seconds=time.monotonic()-began,gpu=gpu,details=setup))
            if wid==0:
                write(out/'learner_admitted.json',dict(gpu=gpu,at=now()))
                status('learning',gpu=gpu['index'],model_pid=backend.process.pid)
                result=backend.call(TRAIN,dict(out=str(out),gpu=gpu),14*24*3600)
                write(out/'adapter_ready.json',dict(result=result,at=now()))
            else:
                status('actor_gate',gpu=gpu['index'],model_pid=backend.process.pid)
                validate_live(backend,cfg,folder/'initial_validation')
                current=-1
                while not (out/'adapter_ready.json').exists():
                    if (out/'failure.json').exists():raise RuntimeError('Another worker failed')
                    item=claim_job(out,wid)
                    if item is None:
                        status('actor_ready',gpu=gpu['index'],model_pid=backend.process.pid,policy_version=current);time.sleep(.5);continue
                    key,job=item
                    if current!=job['version']:
                        backend.call(LOAD,job['policy'],120);current=job['version']
                    elif current==0:
                        backend.call(LOAD,job['policy'],120)
                    status('rollout',gpu=gpu['index'],model_pid=backend.process.pid,job=key,policy_version=current)
                    result=backend.call(ROLLOUT,job,3600);publish_job(out,key,result)
                final=read(out/'adapter/adapter_config.json')
                backend.call(LOAD,dict(path=str(out/'adapter'),sha256=final['weights_sha256'],version=final['policy_version']),120)
            cfg=read(out/'evaluation/qa_config.json');backend.call(CONFIG,dict(config=cfg),60);backend.cfg=cfg
            status('evaluation_gate',gpu=gpu['index'],model_pid=backend.process.pid)
            validate_live(backend,cfg,folder/'final_validation')
            target=out/'evaluation'
            while not (out/'failure.json').exists():
                item=base.claim(target,wid)
                if item is None:break
                index,uid=item;case=target/'cases'/uid
                def progress(phase,**kw):status('query',gpu=gpu['index'],model_pid=backend.process.pid,index=index,question_id=uid,phase=phase,**kw)
                run_case(backend,cfg,read(case/'question.json'),read(case/'sentences.json'),read(case/'original_qa.json'),target/'attempts'/uid,
                    read(case/'archive.json'),'adaptive',progress)
                base.publish(target,uid,target/'attempts'/uid)
            status('complete',gpu=gpu['index'])
    except StartupPoolBusy:raise
    except old.ValidationDrained:status('complete',reason='queue_drained_before_admission')
    except BaseException as exc:fail(out,wid,exc);status('failed',error=str(exc));raise
    finally:
        if backend is not None:backend.close(graceful=True)


def worker(out,wid):
    while True:
        try:return worker_once(out,wid)
        except StartupPoolBusy:time.sleep(10)


def supervise(out):
    verify_manifest(out)
    if os.environ.get('VIDEOAGENT_PHYSICAL_CUDA_POOL')!='1':raise ValueError('Physical CUDA pools required')
    children={};rows=[]
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Already launched')
        write(out/'launch.json',dict(pid=os.getpid(),identity=identity(os.getpid()),at=now()))
        def launch(wid):
            command=[sys.executable,'-u',str(out/'runtime/script/q6_parallel_rl.py'),'worker','--out',str(out),'--worker-id',str(wid)]
            env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',CUDA_DEVICE_ORDER='PCI_BUS_ID')
            with (out/f'worker-{wid}.log').open('x') as log:p=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env)
            children[wid]=p;rows.append(dict(id=wid,pid=p.pid,identity=identity(p.pid)));write(out/'workers_launch.json',rows)
        launch(0)
        while True:
            if (out/'learner_admitted.json').exists() and len(children)==1 and not (out/'failure.json').exists():
                for wid in range(1,read(out/'training_config.json')['rollout_workers']+1):launch(wid)
            for wid,p in children.items():
                if p.poll() not in (None,0) and not (out/'failure.json').exists():fail(out,wid,RuntimeError('Worker exited '+str(p.returncode)))
            states={str(wid):read(out/'workers'/str(wid)/'status.json') for wid in children if (out/'workers'/str(wid)/'status.json').exists()}
            error=read(out/'failure.json') if (out/'failure.json').exists() else None
            trained=(out/'training/result.json').exists();evaluation=old.evaluation_report(out) if trained else None
            live=any(p.poll() is None for p in children.values());complete=bool(evaluation and evaluation['complete'] and not error and not live)
            if not live and not complete and not error:
                fail(out,'controller',RuntimeError('Exited before complete'));error=read(out/'failure.json')
            stage='failed' if error else 'complete' if complete else 'validation300' if trained else 'parallel_RL'
            state='failed' if error else 'complete' if complete else 'running'
            write(out/'queue_status.json',dict(state=state,stage=stage,controller_pid=os.getpid(),workers=states,
                allowed_gpu_indices=read(out/'experiment.json')['allowed_gpu_indices'],maximum_model_workers=1+read(out/'training_config.json')['rollout_workers'],primary_arm='Qplus',updated_at=now()))
            write(out/'report.json',dict(stage=stage,complete=complete,primary_arm='Qplus',epochs=read(out/'training_config.json')['epochs'],
                training_progress=read(out/'training/status.json') if (out/'training/status.json').exists() else None,
                training=read(out/'training/result.json') if trained else None,evaluation=evaluation,failure=error,updated_at=now()))
            if not live:return
            time.sleep(10)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','supervise','worker'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--parent',type=Path);parser.add_argument('--worker-id',type=int)
    parser.add_argument('--epochs',type=int);parser.add_argument('--gpus',nargs='+');parser.add_argument('--actor-workers',type=int)
    parser.add_argument('--handoff',type=Path)
    args=parser.parse_args();out=args.out.resolve()
    if args.action=='prepare':prepare(out,args.parent.resolve(),epochs=args.epochs,gpus=args.gpus,actor_workers=args.actor_workers,
        handoff=args.handoff.resolve() if args.handoff else None)
    elif args.action=='supervise':supervise(out)
    else:worker(out,args.worker_id)
