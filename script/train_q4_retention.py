"""Frozen 200-train / 300-evaluate Q4 retention experiment and queued lifecycle.

No existing Q4 process/runtime is changed. Only teacher annotation sees training
answers; a question-level internal split selects epochs. Both final readers use
the unmodified base model, with LoRA enabled exclusively for q3_filter.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import importlib
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

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from script.extend_q4_500 import read, write, sha, now, pid_identity


def runtime(out):
    sys.path.insert(0, str(out/'runtime'))
    return importlib.import_module('script.run_q_v4_experiment')


def verify(out):
    def check(row):
        n, h = row
        if sha(out/n) != h:
            raise ValueError('Frozen training artifact changed: '+n)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(check, read(out/'frozen_hashes.json').items()))


def prepare(out, parent, extension):
    if out.exists():
        raise ValueError('Training root already exists')
    test = read(parent/'cohort.json')
    train = read(extension/'new_case_ids.json')
    if len(test) != 300 or len(set(train)) != 200 or set(train)&set(test):
        raise ValueError('Invalid 200/300 question isolation')
    out.mkdir(parents=True)
    shutil.copytree(parent/'runtime', out/'runtime', ignore=shutil.ignore_patterns('__pycache__'))
    for name in ['script/train_q4_retention.py', 'script_core/q4_retention_training.py', 'script/extend_q4_500.py']:
        shutil.copy2(REPO/name, out/'runtime'/name)
    # All behavioral hooks live only in this new frozen runtime.
    with (out/'runtime/script_core/backend.py').open('a') as f:
        f.write('\n# Retention-training-only hook; untrained runtime is unchanged.\nfrom .q4_retention_training import install_backend as _install_retention\n_install_retention(LocalModels)\n')
    with (out/'runtime/script_core/q_v4.py').open('a') as f:
        f.write("\nVERSION = 'Q4-adaptive-trained-retention-v1'\nPLUS_VERSION = 'Q4plus-adaptive-trained-retention-v1'\n")
    write(out/'train_ids.json', train)
    write(out/'test_ids.json', test)
    ordered = sorted(train, key=lambda u: hashlib.sha256(('Q4-retention-20261105/'+u).encode()).hexdigest())
    write(out/'split.json', dict(training=ordered[:180], validation=ordered[180:], refit=train, evaluation=test,
        rule='SHA256(Q4-retention-20261105/question_id), first180 training, last20 validation; no window-level split'))
    cfg = read(parent/'qa_config.json')
    cfg['models']['retention_training_ids'] = str(out/'train_ids.json')
    write(out/'teacher_config.json', cfg)
    write(out/'base_qa_config.json', read(parent/'qa_config.json'))
    write(out/'training_config.json', dict(seed=20261105, rank=8, alpha=16, dropout=.05,
        target_modules=['language_model.layers.*.self_attn.q_proj','language_model.layers.*.self_attn.v_proj'],
        learning_rate=2e-5, weight_decay=.01, betas=[.9,.999], gradient_clip=1., microbatch=1,
        accumulation=16, effective_batch=16, max_epochs=3, warmup_fraction=.05, scheduler='cosine',
        precision='bf16 frozen base; fp32 adapters/optimizer', gradient_checkpointing=True,
        max_sequence_length=2048, truncation=False, loss='class-value tokens only, per-class total token weight normalized',
        class_balance='inverse square-root class frequency on training partition only, clipped [0.5,3]',
        select='minimum mean validation class-token NLL across epochs1..3; ties choose earliest',
        refit='fresh adapter initialization on all200 for selected epoch count; same fixed hyperparameters',
        teacher='same frozen Qwen, training-only full options/correct option; not human labels, not policy gradient',
        inference='adapter enabled for q4_independent_batch/q3_filter only; all other operations frozen base'))
    write(out/'experiment.json', dict(version='Q4-adaptive-trained-retention-v1', plus_version='Q4plus-adaptive-trained-retention-v1',
        parent=str(parent), untrained500=str(extension), train_questions=200, evaluation_questions=300,
        internal_training=180, internal_validation=20, teacher_workers=2, evaluation_workers=4,
        allowed_gpu_indices=['0','1'], mode='adaptive', training=True,
        label_scope='Only additional200 answers are used for supervision; no original300 answers or old Q4 predictions enter teacher/train/checkpoint selection',
        unit='complete immutable source observation, not generated entity-role fields',
        sources='All original captions of training200, exact Q4 bounded windows; testing starts from original frozen N initial observations and archive, independently navigates, filters, and answers',
        evaluation_scope='Original300 previously analyzed and evaluated repeatedly: fixed development test, not unseen holdout. Additional200 also have historical benchmark evaluation.',
        limitation='Teacher supervision can be biased or wrong; validation NLL measures agreement with that teacher, not QA accuracy. No QA improvement claim before paired300 results.',
        resource_policy='Preflight shares only spare GPU01 memory; full teacher/training/evaluation queued until untrained500 releases workers; no interruptions or runtime hot edits',
        at=now()))
    # Materialize ONLY training labels here; never load extension evaluation_only500.
    answers = read(REPO/'data/EgoSchema/egoschema/subset_answers.json')
    training_rows = []
    sys.path.insert(0, str(out/'runtime'))
    from script_core import q_repair
    from transformers import AutoTokenizer
    from script_core.o_batch_generation import messages_for
    tok = AutoTokenizer.from_pretrained(cfg['models']['vlm'], local_files_only=True)
    maximum = dict(student=0, teacher=0)
    for uid in train:
        packet = read(extension/'source_inputs'/f'{uid}.json')
        question = packet['question']
        sources = [dict(id=f'S{i}',text=c['text'],source_intervals=[c['interval']],source_caption_ids=[c['id']],
            source_type='caption',source_status='caption_asserted') for i,c in enumerate(packet['captions'])]
        requests = q_repair.windows(question['question'], sources)
        row = dict(question_id=uid, question=question, correct_index=int(answers[uid]), windows=requests,
            source_packet=str(extension/'source_inputs'/f'{uid}.json'), source_sha256=sha(extension/'source_inputs'/f'{uid}.json'))
        write(out/'teacher_inputs'/f'{uid}.json', row)
        training_rows.append(dict(question_id=uid, windows=len(requests), observations=len(sources)))
        for req in requests:
            messages = messages_for('q3_filter', req)[0]
            n = len(tok.apply_chat_template(messages, tokenize=True, add_generation_prompt=True))
            maximum['student'] = max(maximum['student'], n)
            # Worst-case target length is also checked with the real tokenizer later.
            from script_core.q4_retention_training import TEACHER_PROMPT
            messages[0]['content'] += '\n'+TEACHER_PROMPT
            messages[1]['content'] = json.dumps(dict(observation_request=req, training_reference=dict(
                options=question['options'],correct_index=int(answers[uid]),correct_option=question['options'][int(answers[uid])])),ensure_ascii=False)
            maximum['teacher'] = max(maximum['teacher'], len(tok.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)))
    if maximum['student']+256 > 2048 or maximum['teacher']+1024 > cfg['models']['max_input_tokens']:
        raise ValueError('Training/teacher capacity exceeded; do not truncate')
    # Evaluation labels remain isolated in the later evaluation-only stage.
    write(out/'validation/data_preflight.json',dict(passed=True,train_questions=200,test_questions=300,
        overlap=[], train_internal=180, validation_internal=20, windows=sum(r['windows'] for r in training_rows),
        observations=sum(r['observations'] for r in training_rows), maximum_prompt_tokens=maximum,
        rows=training_rows, student_options_or_answers=False, source_runtime=str(parent/'runtime')))
    write(out/'teacher_claims.json', {})
    immutable = [p for p in out.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name != 'teacher_claims.json']
    write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in immutable})
    verify(out)
    write(out/'queue_status.json',dict(state='prepared',stage='prepared',updated_at=now()))
    print('TRAINING_PREPARED',out,maximum,flush=True)


@contextmanager
def lease(out, folder, minimum=24576, allocator=22528):
    from script.gpu_queue import acquire
    policy = dict(preference=['0','1'],minimum_free_mib=minimum,maximum_utilization=100,stable_observations=3,
        admission_mode='memory_shared',allocator_limit_mib=allocator)
    def waiting(details):
        if (out/'failure.json').exists():
            raise RuntimeError('Experiment halted')
        write(folder/'status.json',dict(stage='waiting_for_gpu',pid=os.getpid(),at=now(),**details))
    with acquire(policy,waiting) as (gpu,_):
        os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid']
        os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
        try:
            yield gpu
        finally:
            os.environ.pop('VIDEOAGENT_GPU_LEASE',None)


def teacher_claim(out,wid):
    with (out/'teacher_claims.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        if (out/'failure.json').exists():
            return None
        claims=read(out/'teacher_claims.json')
        for uid in read(out/'train_ids.json'):
            if uid not in claims:
                if (out/'teacher_runs'/uid).exists():
                    raise ValueError('Teacher attempt exists without claim; no implicit retry')
                claims[uid]=dict(state='running',worker_id=wid,pid=os.getpid(),at=now())
                write(out/'teacher_claims.json',claims)
                return uid
    return None


def annotate(out,wid):
    runtime(out);verify(out)
    from script_core.budget import ModelWorker,BudgetLedger,Meter
    from script_core.q_repair import validate_verdict
    from script_core.config import digest
    folder=out/'teacher_workers'/str(wid)
    folder.mkdir(parents=True,exist_ok=False)
    cfg=read(out/'teacher_config.json')
    backend=ModelWorker(cfg)
    try:
        with lease(out,folder) as gpu:
            started=time.monotonic();details=backend.call('prepare_vlm',{},600)
            write(folder/'setup.json',dict(seconds=time.monotonic()-started,gpu=gpu,details=details))
            while True:
                uid=teacher_claim(out,wid)
                if uid is None:
                    break
                write(folder/'status.json',dict(stage='annotation',question_id=uid,gpu=gpu['index'],pid=os.getpid(),at=now()))
                row=read(out/'teacher_inputs'/f'{uid}.json')
                if sha(row['source_packet'])!=row['source_sha256']:
                    raise ValueError('Training source changed')
                target=out/'teacher_runs'/uid
                target.mkdir(parents=True,exist_ok=False)
                ledger=BudgetLedger(3600,answer_reserve=0,calls_path=target/'calls.jsonl')
                meter=Meter(backend,ledger,cfg,target/'raw_calls')
                samples=[]
                try:
                    for start in range(0,len(row['windows']),4):
                        scope=dict(question_id=uid,condition='original_question_only')
                        reqs=row['windows'][start:start+4]
                        packet=dict(operation='q3_filter',scope=scope,items=[dict(source_id=f'W{start+i}',scope=scope,request=req) for i,req in enumerate(reqs)])
                        inputs=dict(question_id=uid,options=row['question']['options'],correct_index=row['correct_index'],packet=packet)
                        result,call=meter.call('q4_training_teacher_batch',inputs)
                        if len(result['items'])!=len(reqs) or result['supervision_scope']!='training_only_options_and_gold; not question_only':
                            raise ValueError('Teacher row/supervision scope mismatch')
                        for i,(req,item) in enumerate(zip(reqs,result['items'])):
                            if item['parse_error'] or not item['ended_normally'] or item['request_hash']!=digest(req) or item['source_id']!=f'W{start+i}' or item['scope']!=scope:
                                raise ValueError('Teacher identity/termination/schema error')
                            target_labels=validate_verdict('q3_filter',req,item['data'])
                            samples.append(dict(question_id=uid,window=start+i,request=req,target=target_labels,
                                teacher_output_ref=call['output_ref'],teacher_scope=result['supervision_scope']))
                    write(target/'samples.json',samples)
                finally:
                    write(target/'cost_summary.json',ledger.summary());meter.close()
                with (out/'teacher_claims.lock').open('a') as f:
                    fcntl.flock(f,fcntl.LOCK_EX);claims=read(out/'teacher_claims.json');claims[uid].update(state='complete',at=now());write(out/'teacher_claims.json',claims)
                print('TEACHER_COMPLETE',wid,uid,len(samples),flush=True)
            write(folder/'status.json',dict(stage='complete',pid=os.getpid(),at=now()))
    finally:
        backend.close(graceful=True)


def training_model(out):
    import torch
    from script_core.backend import LocalModels
    cfg=read(out/'base_qa_config.json')
    cfg['models']['cuda_allocator_limit_mib']=40960
    from script_core.physical_cuda_pool import ensure
    ensure(cfg['models'])
    # Direct local load; unlike ModelWorker, this process owns the autograd graph.
    torch.cuda.set_per_process_memory_fraction(40960*1024**2/torch.cuda.get_device_properties(0).total_memory)
    backend=LocalModels(cfg)
    model,processor=backend.load('vlm')
    return model,processor.tokenizer


def configure_gradients(model,settings):
    from script_core.q4_retention_training import attach
    modules=attach(model,settings['rank'],settings['alpha'],settings['dropout'])
    model.config.use_cache=False
    model.enable_input_require_grads()
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
    return modules


def preflight(out):
    runtime(out);verify(out)
    from script.run_evidence_planner_experiment import verify_model
    verify_model(Path(read(out/'experiment.json')['parent']))
    import torch
    from script_core import q4_retention_training as t
    from script.q_repair_validation import source
    from script_core.q_repair import windows
    folder=out/'validation/real_gradient_gate';folder.mkdir(parents=True,exist_ok=False)
    with lease(out,folder,minimum=43008,allocator=40960) as gpu:
        started=time.monotonic();model,tok=training_model(out)
        setup=time.monotonic()-started;settings=read(out/'training_config.json');torch.manual_seed(settings['seed'])
        req=windows('Which object does M clean?',[source('S0','M cleans a red plate.',0),source('S1','A distant clock is green.',1)])[0]
        ex=t.encode_example(tok,req,dict(S0='direct',S1='irrelevant'))
        ids=torch.tensor([ex['input_ids']],device=model.device)
        model.eval()
        with torch.inference_mode():baseline=model(input_ids=ids,logits_to_keep=1,use_cache=False).logits.detach().float().cpu()
        modules=configure_gradients(model,settings)
        params=t.adapter_parameters(model)
        optimizer=torch.optim.AdamW(params,lr=settings['learning_rate'])
        losses=[]
        model.train()
        with t.enabled(model,True):
            for _ in range(2):
                optimizer.zero_grad(set_to_none=True);loss=t.loss_for(model,ex,{k:1. for k in t.LABELS})
                loss.backward()
                if not torch.isfinite(loss) or not any(p.grad is not None and torch.count_nonzero(p.grad).item() for p in params):
                    raise ValueError('Nonfinite loss or absent adapter gradient')
                if any(p.grad is not None for n,p in model.named_parameters() if not n.endswith(('lora_A','lora_B'))):
                    raise ValueError('Frozen base model received gradients')
                torch.nn.utils.clip_grad_norm_(params,1.,error_if_nonfinite=True);optimizer.step();losses.append(float(loss.detach()))
        model.eval()
        with t.enabled(model,False),torch.inference_mode():after=model(input_ids=ids,logits_to_keep=1,use_cache=False).logits.detach().float().cpu()
        if not torch.equal(baseline,after):
            raise ValueError('Disabling adapter changed frozen base logits')
        metadata=dict(base_model=read(out/'base_qa_config.json')['models']['vlm'],purpose='synthetic_gradient_gate_only')
        binding=t.save_adapter(model,folder/'probe_adapter',metadata)
        with t.enabled(model,True),torch.inference_mode():adapted=model(input_ids=ids,logits_to_keep=1,use_cache=False).logits.detach().float().cpu()
        from safetensors.torch import load_file
        state=load_file(str(folder/'probe_adapter/adapter.safetensors'))
        with torch.no_grad():
            for n,p in model.named_parameters():
                if n in state:p.zero_()
            for n,p in model.named_parameters():
                if n in state:p.copy_(state[n])
        with t.enabled(model,True),torch.inference_mode():reloaded=model(input_ids=ids,logits_to_keep=1,use_cache=False).logits.detach().float().cpu()
        if not torch.equal(adapted,reloaded):
            raise ValueError('Adapter round-trip changed output')
        torch.cuda.synchronize()
        write(folder/'result.json',dict(passed=True,losses=losses,finite_nonzero_gradients=True,base_gradients_absent=True,
            base_logits_bitwise_equal_when_disabled=True,adapter_round_trip_equal=True,modules=len(modules),
            trainable_parameters=binding['trainable_parameters'],setup_seconds=setup,wall_seconds=time.monotonic()-started,
            input_tokens=ex['input_tokens']*2,supervised_tokens=ex['supervised_tokens']*2,
            peak_allocated_mib=torch.cuda.max_memory_allocated()/1024**2,gpu=gpu,at=now()))
        print('REAL_GRADIENT_GATE_PASS',losses,binding['trainable_parameters'],flush=True)


def class_balance(samples):
    counts=Counter(label for s in samples for label in s['target'].values())
    total=sum(counts.values())
    weights={label:min(3.,max(.5,math.sqrt(total/(len(counts)*count)))) for label,count in counts.items()}
    return counts,{k:weights.get(k,1.) for k in ['direct','context','irrelevant','uncertain']}


def train(out):
    runtime(out);verify(out)
    import torch
    from script_core import q4_retention_training as t
    settings=read(out/'training_config.json');split=read(out/'split.json')
    folder=out/'training';folder.mkdir(parents=True,exist_ok=False)
    all_samples=[]
    for uid in read(out/'train_ids.json'):
        if read(out/'teacher_claims.json')[uid]['state']!='complete':raise ValueError('Teacher dataset is incomplete')
        all_samples.extend(read(out/'teacher_runs'/uid/'samples.json'))
    if {s['question_id'] for s in all_samples}!=set(split['refit']) or set(split['refit'])&set(split['evaluation']):
        raise ValueError('Training sample question isolation failed')
    binding={str(p.relative_to(out)):sha(p) for p in (out/'teacher_runs').rglob('samples.json')}
    write(folder/'dataset_manifest.json',binding)
    with lease(out,folder,minimum=43008,allocator=40960) as gpu:
        started=time.monotonic();torch.manual_seed(settings['seed']);random.seed(settings['seed'])
        model,tok=training_model(out);setup=time.monotonic()-started
        torch.manual_seed(settings['seed'])
        encoded=[dict(t.encode_example(tok,s['request'],s['target'],settings['max_sequence_length']),question_id=s['question_id']) for s in all_samples]
        train_rows=[e for e in encoded if e['question_id'] in set(split['training'])]
        validation=[e for e in encoded if e['question_id'] in set(split['validation'])]
        if len(train_rows)+len(validation)!=len(encoded):raise ValueError('Incomplete internal split')
        modules=configure_gradients(model,settings)
        params=t.adapter_parameters(model)
        initial={n:p.detach().cpu().clone() for n,p in model.named_parameters() if n.endswith(('lora_A','lora_B'))}
        counts,weights=class_balance(train_rows)
        write(folder/'dataset_statistics.json',dict(samples=len(encoded),training_samples=len(train_rows),validation_samples=len(validation),
            maximum_tokens=max(e['input_tokens'] for e in encoded),train_label_counts=counts,train_class_weights=weights,
            module_count=len(modules),trainable_parameters=sum(p.numel() for p in params),test_questions_seen=0))
        history=[];best_loss=float('inf');best_epoch=None;tokens=0;supervised_tokens=0;microsteps=0
        def fit(rows,epochs,weights,phase,validate=False):
            nonlocal best_loss,best_epoch,tokens,supervised_tokens,microsteps
            optimizer=torch.optim.AdamW(params,lr=settings['learning_rate'],betas=tuple(settings['betas']),weight_decay=settings['weight_decay'])
            total_steps=math.ceil(len(rows)/settings['accumulation'])*epochs;warmup=max(1,math.ceil(total_steps*settings['warmup_fraction']))
            def scale(step):
                if step<warmup:return (step+1)/warmup
                return .5*(1+math.cos(math.pi*min(1.,(step-warmup)/max(1,total_steps-warmup))))
            scheduler=torch.optim.lr_scheduler.LambdaLR(optimizer,scale)
            rng=random.Random(settings['seed']);phase_start=time.monotonic()
            for epoch in range(1,epochs+1):
                order=list(range(len(rows)));rng.shuffle(order);model.train();optimizer.zero_grad(set_to_none=True)
                total_loss=0.;epoch_start=time.monotonic()
                with t.enabled(model,True):
                    for position,i in enumerate(order):
                        sample=rows[i]
                        group_start=(position//settings['accumulation'])*settings['accumulation']
                        group_size=min(settings['accumulation'],len(order)-group_start)
                        loss=t.loss_for(model,sample,weights)
                        if not torch.isfinite(loss):raise ValueError('Nonfinite training loss')
                        (loss/group_size).backward();total_loss+=float(loss.detach())
                        tokens+=sample['input_tokens'];supervised_tokens+=sample['supervised_tokens'];microsteps+=1
                        if (position+1)%settings['accumulation']==0 or position+1==len(order):
                            torch.nn.utils.clip_grad_norm_(params,settings['gradient_clip'],error_if_nonfinite=True)
                            optimizer.step();scheduler.step();optimizer.zero_grad(set_to_none=True)
                        if position%10==0:
                            write(folder/'status.json',dict(stage='training',phase=phase,epoch=epoch,epochs=epochs,sample=position+1,
                                samples=len(order),optimizer_steps=scheduler.last_epoch,input_tokens=tokens,supervised_tokens=supervised_tokens,
                                elapsed_seconds=time.monotonic()-phase_start,pid=os.getpid(),gpu=gpu['index'],at=now()))
                    val_loss=None
                    if validate:
                        model.eval()
                        with torch.inference_mode():
                            values=[float(t.loss_for(model,s,weights)) for s in validation]
                        val_loss=sum(values)/len(values)
                        if val_loss<best_loss:
                            best_loss=val_loss;best_epoch=epoch
                row=dict(phase=phase,epoch=epoch,train_loss=total_loss/len(rows),validation_loss=val_loss,
                    seconds=time.monotonic()-epoch_start,input_tokens=tokens,supervised_tokens=supervised_tokens)
                history.append(row);write(folder/'history.json',history)
                t.save_adapter(model,folder/f'{phase}-epoch-{epoch}',dict(base_model=read(out/'base_qa_config.json')['models']['vlm'],
                    phase=phase,epoch=epoch,validation_loss=val_loss,training_dataset_manifest_sha256=sha(folder/'dataset_manifest.json')))
                print('TRAIN_EPOCH',row,flush=True)
        fit(train_rows,settings['max_epochs'],weights,'selection',validate=True)
        if best_epoch is None:raise ValueError('No valid epoch selected')
        write(folder/'selection.json',dict(epoch=best_epoch,validation_loss=best_loss,selection_questions=split['validation'],test_questions_used=0))
        # Discard selection weights and optimizer state before the all200 refit.
        with torch.no_grad():
            for n,p in model.named_parameters():
                if n in initial:p.copy_(initial[n])
        torch.manual_seed(settings['seed']);counts,weights=class_balance(encoded)
        fit(encoded,best_epoch,weights,'refit')
        model.eval()
        binding=t.save_adapter(model,out/'adapter',dict(base_model=read(out/'base_qa_config.json')['models']['vlm'],
            selected_epoch=best_epoch,train_question_ids_sha256=sha(out/'train_ids.json'),
            training_dataset_manifest_sha256=sha(folder/'dataset_manifest.json'),test_questions_used=0,
            training_config_sha256=sha(out/'training_config.json')))
        torch.cuda.synchronize()
        write(folder/'result.json',dict(complete=True,selected_epoch=best_epoch,validation_loss=best_loss,
            setup_seconds=setup,wall_seconds=time.monotonic()-started,training_forward_input_tokens=tokens,
            supervised_tokens=supervised_tokens,microsteps=microsteps,validation_forward_input_tokens=sum(e['input_tokens'] for e in validation)*settings['max_epochs'],
            peak_allocated_mib=torch.cuda.max_memory_allocated()/1024**2,adapter=binding,gpu=gpu,
            all200_label_counts=counts,all200_class_weights=weights,at=now()))


def prepare_evaluation(out):
    runtime(out);verify(out)
    target=out/'evaluation';target.mkdir(exist_ok=False)
    parent=Path(read(out/'experiment.json')['parent'])
    for name in ['cases','runtime']:
        (target/name).symlink_to((out/'runtime') if name=='runtime' else (parent/'cases'),target_is_directory=True)
    for name in ['runs','attempts','workers']:(target/name).mkdir()
    write(target/'claims.json',{})
    write(target/'cohort.json',read(out/'test_ids.json'))
    write(target/'evaluation_only.json',read(parent/'evaluation_only.json'))
    shutil.copy2(parent/'qa_expected_artifacts.json',target/'qa_expected_artifacts.json')
    cfg=read(out/'base_qa_config.json');cfg['models']['retention_adapter']=str(out/'adapter')
    write(target/'qa_config.json',cfg)
    policy=read(parent/'experiment.json');policy.update(version='Q4-adaptive-trained-retention-v1',plus_version='Q4plus-adaptive-trained-retention-v1',
        workers=4,training=True,retention_adapter=str(out/'adapter'),training_root=str(out),training_data_sha256=sha(out/'train_ids.json'),
        evaluation_scope=read(out/'experiment.json')['evaluation_scope'],new_answers_reused=False,
        selection='all200 refit after 180/20 epoch selection; no test metric used')
    write(target/'experiment.json',policy)
    files=[target/n for n in ['cohort.json','evaluation_only.json','qa_expected_artifacts.json','qa_config.json','experiment.json']]
    for root in [out/'runtime',out/'adapter']:
        files.extend(p for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    for uid in read(out/'test_ids.json'):
        files.extend((target/'cases'/uid/n) for n in ['question.json','sentences.json','original_qa.json','archive.json','source_binding.json'])
    write(target/'frozen_hashes.json',{os.path.relpath(p,target):sha(p) for p in files})
    write(target/'queue_status.json',dict(state='prepared',updated_at=now()))
    return target


def run_children(out, action, count=1):
    rows=[]
    for wid in range(count):
        command=[sys.executable,'-u',str(out/'runtime/script/train_q4_retention.py'),action,'--out',str(out),'--worker-id',str(wid)]
        log=out/f'{action}-{wid}.log'
        with log.open('x') as f:
            proc=subprocess.Popen(command,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
        rows.append((proc,dict(pid=proc.pid,identity=pid_identity(proc.pid),command=command,log=str(log))))
    write(out/f'{action}_launch.json',dict(controller_pid=os.getpid(),processes=[r for p,r in rows],at=now()))
    while True:
        for proc,row in rows:
            code=proc.poll()
            if code is not None and code!=0 and not (out/'failure.json').exists():
                write(out/'failure.json',dict(stage=action,message=f'{action} child {proc.pid} exited {code}; see {row["log"]}',at=now()))
        status(out,action,children=[dict(r,exit_code=p.poll()) for p,r in rows])
        if all(p.poll() is not None for p,r in rows):break
        time.sleep(10)
    if (out/'failure.json').exists():raise RuntimeError(read(out/'failure.json')['message'])


def status(out,stage,**extra):
    previous=read(out/'queue_status.json') if (out/'queue_status.json').exists() else {}
    if 'started_at' not in previous:previous['started_at']=now()
    write(out/'queue_status.json',dict(state='running',stage=stage,controller_pid=os.getpid(),started_at=previous['started_at'],
        allowed_gpu_indices=['0','1'],updated_at=now(),**extra))
    teacher_costs=[read(p) for p in (out/'teacher_runs').glob('*/cost_summary.json')]
    teacher_calls=[]
    for p in (out/'teacher_runs').glob('*/calls.jsonl'):
        teacher_calls.extend(json.loads(line) for line in p.read_text().splitlines() if line.strip())
    report=dict(stage=stage,complete=False,teacher_questions_completed=sum(c.get('state')=='complete' for c in read(out/'teacher_claims.json').values()),
        teacher_processing_seconds=sum(c['total'] for c in teacher_costs),
        teacher_input_tokens=sum(c.get('input_tokens',0) for c in teacher_calls),teacher_output_tokens=sum(c.get('output_tokens',0) for c in teacher_calls),
        teacher_setup_seconds=sum(read(p)['seconds'] for p in (out/'teacher_workers').glob('*/setup.json')),
        real_gradient_gate=read(out/'validation/real_gradient_gate/result.json') if (out/'validation/real_gradient_gate/result.json').exists() else None,
        training=read(out/'training/result.json') if (out/'training/result.json').exists() else None,
        training_progress=read(out/'training/status.json') if (out/'training/status.json').exists() else None,
        evaluation=read(out/'evaluation/report.json') if (out/'evaluation/report.json').exists() else None,
        failure=read(out/'failure.json') if (out/'failure.json').exists() else None,updated_at=now())
    write(out/'report.json',report)


def compare(out):
    evaluation=read(out/'evaluation/report.json');parent=Path(read(out/'experiment.json')['parent'])
    ids=read(out/'test_ids.json');gold=read(out/'evaluation/evaluation_only.json')
    current={r['question_id']:r for r in evaluation['cases']};old={u:read(parent/'runs'/u/'result.json') for u in ids}
    if set(current)!=set(ids) or not evaluation['complete'] or evaluation['scheduler_failure']:
        raise ValueError('Trained300 evaluation incomplete')
    groups={}
    # Raw calls prove operation-level adapter isolation for every published case.
    filter_calls=0;other_calls=0
    from script_core.q_evidence import selected_observations
    from script_core.config import digest
    for uid in ids:
        folder=out/'evaluation/runs'/uid
        decisions=read(folder/'selection.json');sources=read(folder/'sources.json')
        expected=selected_observations(sources,decisions)
        if read(folder/'retained.json')!=expected:
            raise ValueError('Trained evaluation keep-only audit failed')
        for arm in ['Q','Qplus']:
            if current[uid]['arms'][arm]['retained_evidence_hash']!=digest(expected):
                raise ValueError('Trained reader evidence identity mismatch')
        for p in (folder/'raw_calls').glob('*.json'):
            if p.name.endswith('.pending.json'):raise ValueError('Uncommitted trained evaluation call')
            raw=read(p)
            if raw.get('error') or not isinstance(raw.get('output'),dict):
                raise ValueError('Failed trained evaluation call')
            inputs=raw['inputs'];output=raw['output']
            active=inputs.get('operation')=='q3_filter'
            audit=output.get('retention_adapter_audit')
            if not audit or audit['active']!=active or audit['weights_sha256']!=read(out/'adapter/adapter_config.json')['weights_sha256']:
                raise ValueError('Adapter leaked into frozen operation or missed a filter call')
            if active:filter_calls+=1
            else:other_calls+=1
    for arm in ['Q','Qplus']:
        better=[];worse=[]
        for uid in ids:
            before=str(old[uid]['arms'][arm]['answer']['selected_option'])==str(gold[uid])
            after=str(current[uid]['arms'][arm]['answer']['selected_option'])==str(gold[uid])
            if after and not before:better.append(uid)
            if before and not after:worse.append(uid)
        groups[arm]=dict(correct=sum(str(current[u]['arms'][arm]['answer']['selected_option'])==str(gold[u]) for u in ids),
            baseline_correct=193,fixed=better,regressed=worse,net=len(better)-len(worse),
            mean_final_input_tokens=sum(current[u]['arms'][arm]['input_tokens'] for u in ids)/300,
            mean_final_answer_seconds=sum(current[u]['arms'][arm]['answer_seconds'] for u in ids)/300)
    write(out/'comparison.json',dict(questions=300,arms=groups,shared_new_processing_seconds=evaluation['processing_seconds'],
        inherited_N_budget_dependency_seconds=evaluation['inherited_source_budgeted_seconds'],
        training=read(out/'training/result.json'),evaluation_scope=read(out/'experiment.json')['evaluation_scope'],
        videotree=read(Path(read(out/'experiment.json')['untrained500'])/'historical_videotree.json')['reports']['300'],at=now()))
    write(out/'validation/completed_evaluation_audit.json',dict(passed=True,questions=300,
        adapter_filter_calls=filter_calls,adapter_disabled_other_calls=other_calls,
        keep_only=True,same_evidence_both_readers=True,test_used_for_training=False,at=now()))


def supervise(out):
    verify(out)
    env=dict(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',CUDA_DEVICE_ORDER='PCI_BUS_ID',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
    os.environ.update(env)
    with (out/'controller.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Already launched; no duplicate training')
        write(out/'launch.json',dict(controller_pid=os.getpid(),identity=pid_identity(os.getpid()),at=now()))
        try:
            run_children(out,'preflight')
            extension=Path(read(out/'experiment.json')['untrained500'])
            while True:
                queue=read(extension/'queue_status.json')
                statuses=list(queue.get('workers',{}).values())
                if queue['state'] in ['complete','failed'] and statuses and all(s['stage'] in ['complete','failed'] for s in statuses):break
                if pid_identity(queue.get('controller_pid',0)) is None:
                    raise RuntimeError('Untrained500 controller disappeared; refuse to start competing jobs before diagnosis')
                status(out,'waiting_for_untrained500',untrained500_completed=read(extension/'report.json')['completed'])
                time.sleep(30)
            run_children(out,'annotate',read(out/'experiment.json')['teacher_workers'])
            run_children(out,'train')
            target=prepare_evaluation(out)
            command=[sys.executable,'-u',str(out/'runtime/script/run_q_v4_experiment.py'),'supervise','--out',str(target)]
            with (out/'evaluation.log').open('x') as log:
                proc=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            write(out/'evaluation_launch.json',dict(pid=proc.pid,identity=pid_identity(proc.pid),command=command,at=now()))
            while proc.poll() is None:
                status(out,'evaluation300',evaluation_controller_pid=proc.pid);time.sleep(10)
            if proc.returncode or not read(target/'report.json')['complete'] or read(target/'report.json')['scheduler_failure']:
                raise RuntimeError('Trained evaluation incomplete; see evaluation failure and logs')
            compare(out);status(out,'complete')
            report=read(out/'report.json');report['complete']=True;report['comparison']=read(out/'comparison.json');write(out/'report.json',report)
            write(out/'queue_status.json',dict(state='complete',stage='complete',controller_pid=os.getpid(),updated_at=now()))
        except BaseException as e:
            if not (out/'failure.json').exists():write(out/'failure.json',dict(message=f'{type(e).__name__}: {e}',traceback=traceback.format_exc(),at=now()))
            status(out,'failed');queue=read(out/'queue_status.json');queue.update(state='failed',error=str(e));write(out/'queue_status.json',queue);raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','preflight','annotate','train','supervise'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--parent',type=Path)
    parser.add_argument('--extension',type=Path)
    parser.add_argument('--worker-id',type=int,default=0)
    args=parser.parse_args();out=args.out.resolve()
    if args.action=='prepare':prepare(out,args.parent.resolve(),args.extension.resolve())
    elif args.action=='annotate':annotate(out,args.worker_id)
    else:globals()[args.action](out)
