"""Independent Q6/Q6+ online outcome RL; frozen Q5 evaluation is untouched."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
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
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script.scale_live_parallel_pool import read,write,sha,now,identity,verify_manifest

TRAIN='__q6_online_training__'
CONFIG='__q6_evaluation_config__'


class ValidationDrained(Exception):pass


def prepare(out,parent):
    if out.exists():raise ValueError('Q6 root already exists')
    if not read(parent/'training/result.json')['complete']:raise ValueError('Q5 SFT incomplete')
    train=read(parent/'train_ids.json');test=read(parent/'test_ids.json')
    if len(set(train))!=200 or len(set(test))!=300 or set(train)&set(test):raise ValueError('Invalid 200/300 isolation')
    source=Path(read(parent/'experiment.json')['untrained500'])
    if not read(source/'report.json')['complete']:raise ValueError('Frozen source preparation incomplete')
    out.mkdir(parents=True)
    shutil.copytree(parent/'runtime',out/'runtime',ignore=shutil.ignore_patterns('__pycache__'))
    from script.physical_pool_preparation import stage_support
    stage_support(out/'runtime')
    for name in ['script/train_q6_rl.py','script_core/q6_rl.py']:
        shutil.copy2(ROOT/name,out/'runtime'/name)
    with (out/'runtime/script_core/backend.py').open('a') as f:
        f.write('\nfrom .q6_rl import install_backend as _install_q6_rl\n_install_q6_rl(LocalModels)\n')
    with (out/'runtime/script_core/q_v4.py').open('a') as f:
        f.write("\nVERSION='Q6-outcome-RL-v1'\nPLUS_VERSION='Q6plus-outcome-RL-v1'\n")
    shutil.copytree(parent/'adapter',out/'initial_adapter')
    initial=read(out/'initial_adapter/adapter_config.json')
    if initial['weights_sha256']!=sha(out/'initial_adapter/adapter.safetensors'):raise ValueError('Warmstart hash mismatch')
    settings=dict(seed=20261106,epochs=6,group_size=2,algorithm='on-policy REINFORCE with leave-one-out baseline',
        learning_rate=5e-6,weight_decay=0.,betas=[.9,.999],gradient_clip=1.,
        epsilon=.1,temperature=1.,kl_coefficient=.02,policy_gradient_normalizer=256.,
        max_sequence_length=2048,rank=8,alpha=16,dropout=.05,
        reward='Q6plus final answer correct=1 incorrect=0; no retention/length reward',
        reference='fixed Q5 epoch6 adapter; exact four-action categorical KL on visited states',
        action_policy='first-token categorical over direct/context/irrelevant/uncertain; JSON and remaining label tokens forced',
        optimizer_initializations=1,adapter_initializations=1,internal_validation=False,selection='fixed final epoch6',
        loss='-LOO advantage * sum action log-probs /256 + .02 mean categorical KL; one update per question group',
        rollout_dropout=False,replay_epochs=1,training_questions=200,validation_questions=300,
        inference='same four-action policy argmax; adapter only q3_filter; base readers/audits/navigation',
        primary_arm='Qplus',comparison_arms=['Q'])
    write(out/'training_config.json',settings);write(out/'train_ids.json',train);write(out/'test_ids.json',test)
    cfg=read(parent/'base_qa_config.json');cfg['models'].pop('retention_training_ids',None)
    cfg['models']['retention_adapter']=str(out/'initial_adapter')
    cfg['models']['q6_rl']={k:settings[k] for k in ['epsilon','temperature','max_sequence_length']}
    cfg['models']['cuda_allocator_limit_mib']=40960
    write(out/'training_model_config.json',cfg)
    evalcfg=json.loads(json.dumps(cfg));evalcfg['models']['retention_adapter']=str(out/'adapter')
    evalcfg['models']['cuda_allocator_limit_mib']=22528
    write(out/'evaluation/qa_config.json',evalcfg)
    rewards={}
    for uid in train:
        teacher=read(parent/'teacher_inputs'/f'{uid}.json') if (parent/'teacher_inputs'/f'{uid}.json').exists() else read(Path(read(parent/'experiment.json')['historical_training_root'])/'teacher_inputs'/f'{uid}.json')
        if teacher['question_id']!=uid:raise ValueError('Foreign training reward')
        rewards[uid]=teacher['correct_index']
    write(out/'train_rewards.json',rewards)
    # Validation labels are a separate scoring-only file and never read by train_loaded.
    allgold=read(source/'evaluation_only.json')
    write(out/'evaluation/evaluation_only.json',{uid:allgold[uid] for uid in test})
    write(out/'evaluation/cohort.json',test);write(out/'evaluation/claims.json',{})
    files=['question.json','sentences.json','original_qa.json','archive.json']
    maximum=0
    from transformers import AutoTokenizer
    from script_core.q6_rl import action_tokens
    from script_core.q_repair import windows
    from script_core.o_batch_generation import messages_for
    tok=AutoTokenizer.from_pretrained(cfg['models']['vlm'],local_files_only=True);action_tokens(tok)
    for partition,ids in [('train_cases',train),('evaluation/cases',test)]:
        for uid in ids:
            origin=source/'cases'/uid;target=out/partition/uid;target.mkdir(parents=True)
            if read(origin/'question.json')['question_id']!=uid:raise ValueError('Case identity mismatch')
            for name in files:shutil.copy2(origin/name,target/name)
            binding={name:sha(target/name) for name in files}
            write(target/'source_binding.json',dict(source=str(origin.resolve()),files=binding))
            if partition=='train_cases':
                question=read(target/'question.json');ss=read(target/'sentences.json')
                for req in windows(question['question'],ss):
                    prompt=tok.apply_chat_template(messages_for('q3_filter',req)[0],tokenize=True,add_generation_prompt=True)
                    # Conservative allowance for all eight long labels and forced IDs.
                    maximum=max(maximum,len(prompt)+128)
    if maximum>2048:raise ValueError('Q6 training context too large; no truncation')
    write(out/'preflight.json',dict(passed=True,maximum_initial_window_bound=maximum,train200_test300_disjoint=True,
        source_inputs='Original N initial observations and complete archive; no prior Q answers/final retained collection copied',
        initial_adapter_sha256=initial['weights_sha256'],policy_training_data='train_rewards200 only; no teacher action targets',
        physical_pool='40GiB primary,22GiB helpers; allocate before model load; hold through queue',at=now()))
    write(out/'experiment.json',dict(version='Q6-outcome-RL-v1',plus_version='Q6plus-outcome-RL-v1',
        primary_arm='Qplus',comparison_arms=['Q'],mode='adaptive',epochs=6,training_questions=200,validation_questions=300,
        parent_sft=str(parent),source_root=str(source),initial_adapter_sha256=initial['weights_sha256'],
        allowed_gpu_indices=['0','1','2','3'],resource_policy='One resident42GiB RL primary; after epoch6 up to3 resident24GiB evaluation helpers; shared admission',
        validation_scope='User-designated original300 development validation; not unseen holdout; scoring only after6 epochs',
        rollout_count=2400,rollouts_per_question_per_epoch=2,model_protocol='Original Q4 adaptive with only contribution action decoder/weights changed',at=now()))
    immutable=[p for p in out.rglob('*') if p.is_file() and '__pycache__' not in p.parts
               and str(p.relative_to(out))!='evaluation/claims.json']
    write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in immutable})
    write(out/'queue_status.json',dict(state='prepared',stage='RL_ready',controller_pid=None,updated_at=now()))
    print('Q6 PREPARED',out,flush=True)


class LocalProxy:
    """Metered serial calls on the resident model; no model unload between calls."""
    requires_vlm_warmup=False
    def __init__(self,backend):self.backend=backend
    def call(self,operation,inputs,timeout):
        import torch
        backend=self.backend;previous=backend.deadline
        backend.deadline=time.monotonic()+max(0.,timeout-min(2.,timeout/5))
        backend.model_tokens={};backend.model_pixels={};began=time.monotonic();cpu=time.process_time()
        def expired():
            root=Path(backend.cfg['models']['retention_adapter']).parent
            write(root/'hard_deadline_failure.json',dict(operation=operation,timeout=timeout,pid=os.getpid(),at=now()))
            os._exit(124)
        watchdog=threading.Timer(timeout,expired);watchdog.daemon=True;watchdog.start()
        try:
            result=backend.call(operation,inputs);torch.cuda.synchronize()
            usage=result.setdefault('usage',{})
            usage.update(cpu_seconds=time.process_time()-cpu,gpu_resident_seconds=time.monotonic()-began,
                gpu_peak_allocated_bytes=torch.cuda.max_memory_allocated())
            return result
        finally:
            watchdog.cancel();backend.deadline=previous


def train_loaded(backend,inputs):
    import torch
    from script_core import q6_rl as rl,q4_retention_training as sft
    from script.run_q_v4_experiment import run_case
    from script.q_v4_validation import validate_live
    from script_core.physical_cuda_pool import status as pool_status
    out=Path(inputs['out']);settings=read(out/'training_config.json');gpu=inputs['gpu']
    folder=out/'training';folder.mkdir()
    def progress(stage,**detail):write(folder/'status.json',dict(stage=stage,pid=os.getpid(),gpu=gpu['index'],at=now(),**detail))
    model,processor=backend.load('vlm')
    modules=[m for m in model.modules() if isinstance(m,sft.LowRankLinear)]
    if len(modules)!=56:raise ValueError('Q6 warmstart must attach exactly56 LoRA modules once')
    params=sft.adapter_parameters(model)
    reference={n:p.detach().clone() for n,p in model.named_parameters() if n.endswith(('lora_A','lora_B'))}
    optimizer=torch.optim.AdamW(params,lr=settings['learning_rate'],betas=tuple(settings['betas']),weight_decay=0)
    rewards=read(out/'train_rewards.json');ids=read(out/'train_ids.json')
    if set(rewards)!=set(ids) or len(ids)!=200:raise ValueError('Invalid RL reward allowlist')
    proxy=LocalProxy(backend);backend._q6_sampling=False;backend._q6_policy_version=0
    progress('likelihood_gate')
    from script.q_v4_validation import synthetic_case
    from script_core.q_repair import windows
    gate_question,_,_,gate_sources=synthetic_case()
    request=windows(gate_question['question'],gate_sources)[0]
    packet=dict(operation='q3_filter',scope=dict(question_id='q6-likelihood-gate',condition='original_question_only'))
    packet['items']=[dict(source_id='W0',scope=packet['scope'],request=request)]
    gate_started=time.monotonic();output=proxy.call('q4_independent_batch',packet,150)
    trace=output['items'][0]['q6_action_trace']
    with torch.no_grad(),sft.enabled(model,True):
        logits=rl.cached_decision_logits(model,trace)
        p=rl.probabilities(logits,settings['epsilon'],settings['temperature'])
        actions=torch.tensor(trace['actions'],device=model.device)
        gate_logp=p.log().gather(-1,actions[:,None]).squeeze(-1)
        drift=float((gate_logp-torch.tensor(trace['old_logps'],device=model.device)).abs().max())
    write(out/'validation/likelihood_gate.json',dict(passed=drift<=.05,max_logprob_drift=drift,
        seconds=time.monotonic()-gate_started,output=output,additional_recompute_input_tokens=len(trace['input_ids']),
        optimizer_updates=0,adapter_initializations=1,at=now()))
    if drift>.05:raise ValueError('Q6 initial cache/causal likelihood gate failed')
    progress('semantic_gate');validate_live(proxy,backend.cfg,out/'validation/primary_initial')
    write(out/'validation/physical_pool.json',pool_status())
    torch.manual_seed(settings['seed']);rng=random.Random(settings['seed'])
    model.enable_input_require_grads();model.gradient_checkpointing_disable()
    model.config.use_cache=True
    history=[];updates=policy_updates=groups=0;training_tokens=reference_tokens=0;start=time.monotonic()
    total_processing=0.;first_gradient=False;unique_source_dependencies={}
    for epoch in range(1,settings['epochs']+1):
        order=list(ids);rng.shuffle(order);epoch_rewards=[];epoch_start=time.monotonic();epoch_updates=0
        for position,uid in enumerate(order,1):
            case=out/'train_cases'/uid;binding=read(case/'source_binding.json')
            if any(sha(case/name)!=h for name,h in binding['files'].items()):raise ValueError('RL source changed')
            question=read(case/'question.json');trajectories=[];terminal=[]
            for rollout in range(settings['group_size']):
                seed=int(hashlib.sha256(f"{settings['seed']}/{epoch}/{uid}/{rollout}".encode()).hexdigest()[:15],16)
                backend._q6_generator=torch.Generator(device=model.device).manual_seed(seed)
                backend._q6_sampling=True;backend._q6_traces=[];model.eval()
                target=folder/'rollouts'/f'epoch-{epoch}'/uid/str(rollout)
                progress('rollout',epoch=epoch,epochs=6,question=position,questions=200,question_id=uid,rollout=rollout,
                         completed_groups=groups,optimizer_steps=updates,policy_gradient_updates=policy_updates)
                try:
                    result=run_case(proxy,backend.cfg,question,read(case/'sentences.json'),read(case/'original_qa.json'),target,
                                    read(case/'archive.json'),'adaptive')
                finally:backend._q6_sampling=False
                reward=rl.outcome_reward(uid,result['arms']['Qplus']['answer']['selected_option'],rewards)
                traces=backend._q6_traces;backend._q6_traces=None
                if not traces:raise ValueError('No filter actions in RL trajectory')
                write(target/'policy_trace.json',dict(question_id=uid,policy_version=backend._q6_policy_version,seed=seed,
                    reward=reward,reward_arm='Qplus',traces=traces))
                trajectories.append(traces);terminal.append(reward);epoch_rewards.append(reward)
                total_processing+=result['cost_summary']['total']
                unique_source_dependencies[uid]=result['source_dependency_seconds']
            advantages=rl.loo_advantages(terminal);count=sum(len(t['actions']) for ts in trajectories for t in ts)
            optimizer.zero_grad(set_to_none=True);pg_sum=kl_sum=0.;maximum_drift=0.
            # Initial equal-reward groups have no policy signal and no reference drift.
            has_signal=any(a!=0 for a in advantages)
            if has_signal or updates:
                model.eval()
                for module in model.modules():
                    if isinstance(module,torch.nn.Dropout):module.eval()
                with sft.enabled(model,True):
                    for traces,advantage in zip(trajectories,advantages):
                        for trace in traces:
                            with rl.reference_weights(model,reference),torch.no_grad():ref=rl.cached_decision_logits(model,trace)
                            logits=rl.cached_decision_logits(model,trace,checkpoint=True)
                            actions=torch.tensor(trace['actions'],device=model.device,dtype=torch.long)
                            logp,kl=rl.policy_terms(logits,ref,actions,settings['epsilon'],settings['temperature'])
                            old=torch.tensor(trace['old_logps'],device=model.device)
                            drift=float((logp.detach()-old).abs().max());maximum_drift=max(maximum_drift,drift)
                            if drift>.05:raise ValueError(f'Q6 rollout/recompute likelihood mismatch: {drift}')
                            pg=-advantage*logp.sum()/settings['group_size']/settings['policy_gradient_normalizer']
                            regularizer=settings['kl_coefficient']*kl.sum()/count
                            loss=pg+regularizer
                            if not torch.isfinite(loss):raise ValueError('Nonfinite RL loss')
                            loss.backward();pg_sum+=float(pg.detach());kl_sum+=float(regularizer.detach())
                            training_tokens+=len(trace['input_ids']);reference_tokens+=len(trace['input_ids'])
                if any(p.grad is not None for n,p in model.named_parameters() if not n.endswith(('lora_A','lora_B'))):raise ValueError('RL base received gradients')
                if not all(p.grad is None or torch.isfinite(p.grad).all() for p in params):raise ValueError('Nonfinite RL gradients')
                nonzero=any(p.grad is not None and torch.count_nonzero(p.grad).item() for p in params)
                if has_signal and not nonzero:raise ValueError('Outcome advantage produced no adapter gradient')
                if nonzero:
                    norm=float(torch.nn.utils.clip_grad_norm_(params,settings['gradient_clip']))
                    optimizer.step();updates+=1;epoch_updates+=1;policy_updates+=int(has_signal);backend._q6_policy_version=updates
                    if has_signal and not first_gradient:
                        write(folder/'first_gradient_check.json',dict(passed=True,epoch=epoch,question_id=uid,rewards=terminal,
                            advantages=advantages,gradient_norm=norm,finite_nonzero_adapter_gradient=True,base_gradients_absent=True,
                            extra_probe_updates=0,adapter_initializations=1,optimizer_initializations=1,at=now()))
                        first_gradient=True
            optimizer.zero_grad(set_to_none=True);groups+=1;model.eval()
            write(folder/'groups'/f'{groups:04}.json',dict(epoch=epoch,question=position,question_id=uid,rewards=terminal,
                advantages=advantages,actions=count,optimizer_steps=updates,policy_gradient_updates=policy_updates,
                pg_loss=pg_sum,kl_loss=kl_sum,max_logprob_drift=maximum_drift,at=now()))
            progress('group_complete',epoch=epoch,epochs=6,question=position,questions=200,completed_groups=groups,
                optimizer_steps=updates,policy_gradient_updates=policy_updates,mean_reward=sum(epoch_rewards)/len(epoch_rewards))
        row=dict(epoch=epoch,mean_reward=sum(epoch_rewards)/len(epoch_rewards),rollouts=len(epoch_rewards),
            optimizer_steps=updates,policy_gradient_updates=policy_updates,epoch_updates=epoch_updates,seconds=time.monotonic()-epoch_start)
        history.append(row);write(folder/'history.json',history)
        checkpoint=folder/'checkpoints'/f'epoch-{epoch}'
        sft.save_adapter(model,checkpoint,dict(base_model=backend.cfg['models']['vlm'],epoch=epoch,purpose='crash recovery only; no checkpoint selection'))
        torch.save(dict(optimizer=optimizer.state_dict(),epoch=epoch,completed_groups=groups,updates=updates,
                        policy_updates=policy_updates,python_rng=rng.getstate()),checkpoint/'optimizer.pt')
    if not first_gradient or not policy_updates:raise ValueError('Six RL passes produced no genuine outcome-driven update')
    model.eval();model.gradient_checkpointing_disable();model.disable_input_require_grads();model.config.use_cache=True
    for p in model.parameters():p.grad=None
    final=sft.save_adapter(model,out/'adapter',dict(base_model=backend.cfg['models']['vlm'],epoch=6,epochs=6,
        algorithm=settings['algorithm'],initial_adapter_sha256=read(out/'experiment.json')['initial_adapter_sha256'],
        reward_arm='Qplus',training_questions=200,validation_questions_used_for_gradients=0,selection='fixed final epoch6'))
    backend._retention_adapter_binding=final
    result=dict(complete=True,epochs=6,completed_groups=groups,rollouts=groups*2,optimizer_steps=updates,
        policy_gradient_updates=policy_updates,adapter_initializations=1,optimizer_initializations=1,internal_validation=0,
        wall_seconds=time.monotonic()-start,rollout_processing_seconds=total_processing,
        unique_historical_source_dependency_seconds=sum(unique_source_dependencies.values()),
        training_forward_input_tokens=training_tokens,reference_forward_input_tokens=reference_tokens,
        peak_allocated_mib=torch.cuda.max_memory_allocated()/1024**2,physical_pool=pool_status(),adapter=final,at=now())
    write(folder/'result.json',result);progress('training_complete',epochs=6,optimizer_steps=updates)
    return result


def model_process(connection,cfg):
    from script_core import budget
    original=inspect.getsource(budget._worker)
    anchor='        operation, inputs, deadline = message\n'
    addition=anchor+'''        if operation == '__q6_online_training__':
            try:
                result = _q6_train(backend, inputs)
                connection.send((True,result))
            except Exception as exc:
                connection.send((False,dict(message=str(exc),usage={},raw_output=dict(traceback=__import__('traceback').format_exc()))))
            continue
        if operation == '__q6_evaluation_config__':
            try:
                import torch
                cfg = inputs['config']
                backend.cfg = cfg
                torch.cuda.empty_cache()
                torch.cuda.set_per_process_memory_fraction(cfg['models']['cuda_allocator_limit_mib']*1024**2/torch.cuda.get_device_properties(0).total_memory)
                backend._retention_adapter_binding = __import__('json').loads(__import__('pathlib').Path(cfg['models']['retention_adapter'],'adapter_config.json').read_text())
                connection.send((True,dict(configured=True,pid=__import__('os').getpid())))
            except Exception as exc:
                connection.send((False,dict(message=str(exc),usage={})))
            continue
'''
    if original.count(anchor)!=1:raise ValueError('Unknown model worker layout')
    scope=dict(vars(budget),_q6_train=train_loaded)
    exec(compile(original.replace(anchor,addition,1),'<Q6-model-process>','exec'),scope)
    scope['_worker'](connection,cfg)


def worker(out,wid):
    from script.gpu_queue import acquire
    from script_core import budget
    from script.q_v4_validation import validate_live
    from script.run_q_v4_experiment import run_case
    from script import run_q_experiment as base
    verify_manifest(out)
    folder=out/'workers'/str(wid);folder.mkdir(parents=True)
    def status(stage,**kw):write(folder/'status.json',dict(stage=stage,pid=os.getpid(),at=now(),**kw))
    def waiting(detail):
        if (out/'failure.json').exists():raise RuntimeError('Q6 pipeline halted')
        if wid and len(read(out/'evaluation/claims.json'))==300:raise ValidationDrained()
        status('waiting_for_gpu',**detail)
    policy=dict(preference=['0','1','2','3'],minimum_free_mib=43008 if wid==0 else 24576,
                stable_observations=3,admission_mode='memory_shared',maximum_utilization=100)
    backend=None
    try:
        with acquire(policy,waiting) as (gpu,_):
            os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid'];os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
            cfg=read(out/'training_model_config.json') if wid==0 else read(out/'evaluation/qa_config.json')
            budget._worker=model_process;backend=budget.ModelWorker(cfg)
            status('loading',gpu=gpu['index']);started=time.monotonic();setup=backend.call('prepare_vlm',{},600)
            write(folder/'setup.json',dict(seconds=time.monotonic()-started,gpu=gpu,details=setup))
            if wid==0:
                status('training_RL',gpu=gpu['index'],model_pid=backend.process.pid)
                result=backend.call(TRAIN,dict(out=str(out),gpu=gpu),14*24*3600)
                write(out/'adapter_ready.json',dict(result=result,at=now()))
                cfg=read(out/'evaluation/qa_config.json');backend.call(CONFIG,dict(config=cfg),60);backend.cfg=cfg
            target=out/'evaluation'
            status('evaluation_gate',gpu=gpu['index'],model_pid=backend.process.pid)
            validate_live(backend,cfg,folder/'validation')
            while True:
                if (out/'failure.json').exists():break
                item=base.claim(target,wid)
                if item is None:break
                index,uid=item;case=target/'cases'/uid
                if any(sha(case/n)!=h for n,h in read(case/'source_binding.json')['files'].items()):raise ValueError('Evaluation source changed')
                def progress(phase,**kw):status('query',gpu=gpu['index'],model_pid=backend.process.pid,index=index,question_id=uid,phase=phase,**kw)
                run_case(backend,cfg,read(case/'question.json'),read(case/'sentences.json'),read(case/'original_qa.json'),target/'attempts'/uid,
                         read(case/'archive.json'),'adaptive',progress)
                base.publish(target,uid,target/'attempts'/uid)
            status('complete',gpu=gpu['index'])
    except ValidationDrained:status('complete',reason='evaluation_drained_before_admission')
    except BaseException as exc:
        write(out/'failure.json',dict(worker_id=wid,message=str(exc),traceback=traceback.format_exc(),at=now()))
        status('failed',error=str(exc));raise
    finally:
        if backend is not None:backend.close(graceful=True)


def evaluation_report(out):
    target=out/'evaluation';gold=read(target/'evaluation_only.json');cases=[]
    for uid in read(target/'cohort.json'):
        p=target/'runs'/uid/'result.json'
        if p.exists():cases.append(read(p))
    successful=[d for d in cases if d['complete'] and not d['error']]
    arms={arm:dict(completed=len(successful),correct=sum(str(d['arms'][arm]['answer']['selected_option'])==str(gold[d['question_id']]) for d in successful),
        mean_input_tokens=sum(d['arms'][arm]['input_tokens'] for d in successful)/len(successful) if successful else None)
        for arm in ['Qplus','Q']}
    return dict(completed=len(successful),planned=300,complete=len(successful)==300,errors=sum(bool(d['error']) for d in cases),
                primary_arm='Qplus',arms=arms,processing_seconds=sum(d['cost_summary']['total'] for d in cases))


def supervise(out):
    verify_manifest(out)
    from script_core.physical_cuda_pool import build_library
    build_library()
    if os.environ.get('VIDEOAGENT_PHYSICAL_CUDA_POOL')!='1':raise ValueError('Q6 requires physical CUDA pool mode')
    children={};rows=[]
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Q6 already launched; no implicit restart')
        write(out/'launch.json',dict(pid=os.getpid(),identity=identity(os.getpid()),at=now()))
        def launch(wid):
            command=[sys.executable,'-u',str(out/'runtime/script/train_q6_rl.py'),'worker','--out',str(out),'--worker-id',str(wid)]
            env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',CUDA_DEVICE_ORDER='PCI_BUS_ID')
            with (out/f'worker-{wid}.log').open('x') as log:
                process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env)
            children[wid]=process;rows.append(dict(id=wid,pid=process.pid,identity=identity(process.pid)));write(out/'workers_launch.json',rows)
        launch(0)
        while True:
            if (out/'adapter_ready.json').exists() and len(children)==1 and not (out/'failure.json').exists():
                for wid in [1,2,3]:launch(wid)
            for wid,process in children.items():
                if process.poll() not in (None,0) and not (out/'failure.json').exists():write(out/'failure.json',dict(message='Q6 worker exited',worker_id=wid,returncode=process.returncode,at=now()))
            statuses={str(wid):read(out/'workers'/str(wid)/'status.json') for wid in children if (out/'workers'/str(wid)/'status.json').exists()}
            failure=read(out/'failure.json') if (out/'failure.json').exists() else None
            trained=(out/'training/result.json').exists();evaluation=evaluation_report(out) if trained else None
            live=any(p.poll() is None for p in children.values())
            complete=bool(evaluation and evaluation['complete'] and not failure and not live)
            if not live and not complete and not failure:
                failure=dict(message='Q6 exited before completing training and validation300',at=now());write(out/'failure.json',failure)
            state='failed' if failure else 'complete' if complete else 'running'
            stage='failed' if failure else 'complete' if complete else 'validation300' if trained else 'training_RL'
            write(out/'queue_status.json',dict(state=state,stage=stage,controller_pid=os.getpid(),workers=statuses,
                allowed_gpu_indices=['0','1','2','3'],primary_arm='Qplus',updated_at=now()))
            write(out/'report.json',dict(stage=stage,complete=complete,primary_arm='Qplus',comparison_arms=['Q'],epochs=6,
                training_progress=read(out/'training/status.json') if (out/'training/status.json').exists() else None,
                training=read(out/'training/result.json') if trained else None,evaluation=evaluation,failure=failure,updated_at=now()))
            if not live:return
            time.sleep(10)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','supervise','worker'])
    p.add_argument('--out',type=Path,required=True);p.add_argument('--parent',type=Path);p.add_argument('--worker-id',type=int)
    a=p.parse_args();out=a.out.resolve()
    if a.action=='prepare':prepare(out,a.parent.resolve())
    elif a.action=='supervise':supervise(out)
    else:worker(out,a.worker_id)
