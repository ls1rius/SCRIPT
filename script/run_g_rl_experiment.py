"""Freeze and queue G vs G+RL, remaining 200 train / original 300 evaluate, independently of existing runs."""
import argparse
from collections import Counter
from contextlib import nullcontext
from datetime import datetime
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
import traceback
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script_core import g_rl as core


def now():
    return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def jsonl(path, records):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records))


def verify(out):
    for name, expected in read(out/'frozen_hashes.json').items():
        if core.sha(out/name) != expected:
            raise ValueError('Frozen experiment artifact changed: ' + name)


def verify_model(out):
    expected = read(out/'qa_expected_artifacts.json')
    for name, value in expected['vlm']['files'].items():
        if core.sha(Path(expected['vlm']['path'])/name) != value:
            raise ValueError('Frozen base-model/tokenizer artifact changed: ' + name)
    for name in ['torch', 'transformers']:
        if importlib.metadata.version(name) != expected['software'][name]:
            raise ValueError('Base-model runtime version changed: ' + name)


def prepare(args):
    """Exact remaining 200 for training, original 300 for final evaluation."""
    from script.build_g_rl_memories import prepare as prepare_memories
    out, parent, full = args.out.resolve(), args.parent.resolve(), args.full.resolve()
    if out.exists():
        raise ValueError('Use a fresh experiment directory')
    annotations = read(args.data/'subset_anno.json')
    mapping = read(args.mapping)
    cohort = read(parent/'cohort.json')
    split, overlap = core.remaining_200_split(list(annotations), cohort, mapping)
    if set(read(full/'cohorts.json')['additional200']) != set(split['train']):
        raise ValueError('Remaining 200 differ from the previous full-500 experiment')
    if read(parent/'validation/functional.json')['passed'] is not True:
        raise ValueError('Parent G is not functionally qualified')
    out.mkdir(parents=True)
    shutil.copy2(args.mapping, out/'uid_to_ego4d.json')
    write(out/'splits_500.json', {'splits': split, 'source_overlap': overlap,
          'policy': 'User-selected remaining 200 train / unchanged original 300 final evaluation.',
          'independent_generalization': False})
    write(out/'train_labels.json', {u: int(annotations[u]['truth']) for u in split['train']})
    write(out/'validation_labels.json', {u: int(annotations[u]['truth']) for u in split['validation']})
    cfg = read(parent/'qa_config.json')
    if cfg['models']['generation']['do_sample']:
        raise ValueError('Original final QA must be deterministic')
    write(out/'qa_config.json', cfg)
    shutil.copy2(parent/'qa_expected_artifacts.json', out/'qa_expected_artifacts.json')
    prepare_memories(out/'memory200', parent, full, split['train'])
    root = Path(__file__).resolve().parents[1]
    (out/'control').mkdir()
    shutil.copy2(__file__, out/'control/run_g_rl_experiment.py')
    (out/'runtime/script_core').mkdir(parents=True)
    (out/'runtime/script_core/__init__.py').write_text('')
    shutil.copy2(root/'script_core/g_rl.py', out/'runtime/script_core/g_rl.py')
    write(out/'experiment.json', {
        'created_at': now(), 'scope': 'Internal G+RL experiment; all public 500 previously used for development/evaluation; exact user split has shared source videos.',
        'parent_G': str(parent), 'full_500': str(full), 'model': str(Path(cfg['models']['vlm']).resolve()),
        'split_sizes': {'train': 200, 'validation': 300}, 'source_overlap': overlap,
        'selection': 'All remaining 200 train; exact original 300 evaluate. No accuracy selection, no random replacement.',
        'memory': 'Frozen G r3 serial extraction for training 200; parent sealed G inputs for original 300. No new retrieval or images.',
        'training': {'algorithm': 'GRPO, one PPO epoch per group, no critic', 'epochs': 1,
                     'group_size': 4, 'temperature': 1.0, 'top_p': 1.0, 'top_k': 0,
                     'lora_rank': 8, 'lora_alpha': 16, 'lora_dropout': 0.0,
                     'target_modules': ['q_proj','v_proj'], 'learning_rate': 2e-5,
                     'kl_beta': 0.02, 'ppo_epsilon': 0.2, 'max_grad_norm': 1.0,
                     'reward': '1 correct, 0 wrong, -0.1 unresolved or unterminated; same gold-blind parser for both arms.',
                     'seed': 0, 'dtype': 'bfloat16', 'gradient_checkpointing': True,
                     'zero_variance_groups': 'Skip update, preserve rollouts, do not resample.',
                     'max_new_tokens': cfg['models']['generation']['max_new_tokens']},
        'evaluation': {'when': 'Only after fixed training; original 300 never used for updates, hyperparameters or checkpoint selection.',
                       'baseline': 'Fresh same-runtime adapter-disabled G, paired with trained adapter.',
                       'generation': cfg['models']['generation'], 'parser': 'New gold-blind parser shared by both arms; frozen historical scores untouched.',
                       'order': 'Alternate G/G_RL order by fixed original-300 index.'},
        'max_context_tokens': cfg['models']['max_input_tokens'],
        'resource_policy': {'minimum_free_mib': 32768, 'stable_observations': 3, 'poll_seconds': 15,
                            'gpu_preference': ['3','0','1','2'], 'sharing_allowed': True, 'stop_other_tasks': False},
        'python_training': str((root/'workdirs/g-rl-env/bin/python').absolute()),
        'python_extraction': str((root/'workdirs/query-runtime-env/bin/python').absolute()),
        'failure_policy': 'Record and stop. No automatic retry, context truncation, quantization or answer fallback.'})
    fixed = {str(p.relative_to(out)): core.sha(p) for p in out.rglob('*')
             if p.is_file() and 'memory200' not in p.relative_to(out).parts}
    fixed['memory200/frozen_hashes.json'] = core.sha(out/'memory200/frozen_hashes.json')
    write(out/'frozen_hashes.json', fixed)
    verify(out)
    write(out/'queue_status.json', {'state': 'prepared', 'phase': 'training_memory_200', 'updated_at': now()})
    print(json.dumps({'out': str(out), 'train': 200, 'validation': 300,
                      'shared_original_videos': len(overlap['shared_original_video_ids']), 'state': 'prepared'}))


def seal_dataset(out, split):
    """Seal G inputs when available; training stage never opens validation labels."""
    policy = read(out/'experiment.json')
    ids = read(out/'splits_500.json')['splits'][split]
    parent = out/'memory200' if split == 'train' else Path(policy['parent_G'])
    target = out/f'{split}.jsonl'
    if target.exists():
        raise ValueError('Dataset was already sealed: ' + split)
    labels = read(out/'train_labels.json') if split == 'train' else None
    records = []
    for uid in ids:
        seal_path = parent/'materialized'/uid/'sealed.json'
        for name, expected in read(seal_path).items():
            if core.sha(seal_path.parent/name) != expected:
                raise ValueError('G materialization changed: ' + uid)
        source = parent/'final_inputs/G_generalized_entities'/f'{uid}.json'
        inputs = read(source)
        if core.fixed_regions(inputs) != core.fixed_regions(read(parent/'final_inputs/A_original'/f'{uid}.json')):
            raise ValueError('G fixed prompt regions changed')
        question = read(parent/'packets'/f'{uid}.json')['packet']['question']
        rec = {'question_id': uid, 'inputs': inputs, 'options': question['options'],
               'parent_input_sha256': core.sha(source)}
        if labels is not None:
            rec['label'] = labels[uid]
        records.append(rec)
    jsonl(target, records)
    write(out/f'{split}_dataset_seal.json', {target.name: core.sha(target)})


def verify_dataset(out, split):
    for name, expected in read(out/f'{split}_dataset_seal.json').items():
        if core.sha(out/name) != expected:
            raise ValueError('Sealed dataset changed: ' + split)


def gpu_info():
    result = subprocess.run(['nvidia-smi', '--query-gpu=index,uuid,memory.free,utilization.gpu',
                             '--format=csv,noheader,nounits'], capture_output=True, text=True, check=True)
    return [{'index': i.strip(), 'uuid': u.strip(), 'free_mib': int(f), 'utilization': int(v)}
            for line in result.stdout.splitlines() for i,u,f,v in [line.split(',')]]


def queue(out):
    verify(out)
    policy = read(out/'experiment.json')
    resource = policy['resource_policy']
    (out/'logs').mkdir(exist_ok=True)
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def run_stage(stage, command, runtime):
            launch_path = out/'launches'/f'{stage}.json'
            if launch_path.exists():
                raise ValueError('Stage already attempted: ' + stage)
            stable = Counter()
            while True:
                observations = gpu_info()
                for g in observations:
                    stable[g['uuid']] = stable[g['uuid']] + 1 if g['free_mib'] >= resource['minimum_free_mib'] else 0
                eligible = [g for g in observations if stable[g['uuid']] >= resource['stable_observations']]
                if eligible:
                    gpu = min(eligible, key=lambda g: resource['gpu_preference'].index(g['index']))
                    break
                write(out/'queue_status.json', {'state': 'waiting_for_gpu', 'phase': stage,
                      'controller_pid': os.getpid(), 'minimum_free_mib': resource['minimum_free_mib'],
                      'gpus': observations, 'updated_at': now()})
                time.sleep(resource['poll_seconds'])
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=gpu['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                       PYTHONUNBUFFERED='1', TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='4', MKL_NUM_THREADS='4',
                       HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONPATH=str(runtime))
            with (out/'logs'/f'{stage}.log').open('a') as log:
                proc = subprocess.Popen(command, cwd=runtime, env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT)
                launch = {'phase': stage, 'controller_pid': os.getpid(), 'worker_pid': proc.pid,
                          'gpu': gpu, 'command': command, 'started_at': now()}
                write(launch_path, launch)
                while proc.poll() is None:
                    progress = out/'memory200/progress.json' if stage == 'memory200' else out/'progress.json'
                    details = read(progress) if progress.exists() else {}
                    write(out/'queue_status.json', dict(launch, state='running', progress=details, updated_at=now()))
                    time.sleep(5)
                write(out/'stage_results'/f'{stage}.json', dict(launch, returncode=proc.returncode, ended_at=now()))
                if proc.returncode:
                    raise RuntimeError(f'{stage} failed with exit code {proc.returncode}; no automatic retry')

        run_stage('memory200', [policy['python_extraction'], '-u',
                  str(out/'memory200/control/build_g_rl_memories.py'), '--out', str(out/'memory200')],
                  out/'memory200/runtime/qa')
        seal_dataset(out, 'train')
        run_stage('training', [policy['python_training'], '-u', str(out/'control/run_g_rl_experiment.py'),
                               'train', '--out', str(out)], out/'runtime')
        parent = Path(policy['parent_G'])
        ids = read(out/'splits_500.json')['splits']['validation']
        while True:
            ready = [u for u in ids if (parent/'materialized'/u/'sealed.json').exists()
                     and (parent/'runs/G_generalized_entities'/u/'result.json').exists()]
            if len(ready) == len(ids):
                break
            parent_state = read(parent/'queue_status.json')
            if parent_state['state'] == 'failed':
                raise RuntimeError('Original G did not complete; evaluation dependency failed')
            write(out/'queue_status.json', {'state': 'waiting_for_original_G', 'phase': 'validation300',
                  'controller_pid': os.getpid(), 'ready': len(ready), 'planned': len(ids), 'updated_at': now()})
            time.sleep(30)
        seal_dataset(out, 'validation')
        run_stage('evaluation', [policy['python_training'], '-u', str(out/'control/run_g_rl_experiment.py'),
                                 'evaluate', '--out', str(out)], out/'runtime')
        write(out/'queue_status.json', {'state': 'complete', 'updated_at': now(), 'report': str(out/'report.json')})
        return 0


def train_worker(out):
    # The frozen core takes precedence over the live workspace.
    verify(out)
    verify_model(out)
    policy = read(out/'experiment.json')
    train = rows(out/'train.jsonl')
    verify_dataset(out, 'train')
    if len(train) != 200:
        raise ValueError('Require all 200 training questions')
    import torch
    from transformers import AutoModelForImageTextToText, AutoTokenizer
    from peft import LoraConfig, TaskType, get_peft_model
    random.seed(0)
    torch.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    torch.backends.cuda.matmul.allow_tf32 = False
    versions = {p: importlib.metadata.version(p) for p in ['torch','transformers','peft','accelerate']}
    write(out/'environment.json', {'versions': versions, 'python': sys.version,
                                 'device': torch.cuda.get_device_name(0), 'started_at': now()})
    begin = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(policy['model'], local_files_only=True)
    encoded = {}
    for rec in train:
        tokens = tokenizer.apply_chat_template(core.prompt_messages(rec['inputs']), tokenize=True,
                                              add_generation_prompt=True, return_tensors='pt')
        if tokens.shape[1] + policy['training']['max_new_tokens'] > policy['max_context_tokens']:
            raise ValueError('Prompt plus completion exceeds frozen context cap; no truncation')
        encoded[rec['question_id']] = tokens
    raw_model = AutoModelForImageTextToText.from_pretrained(policy['model'], local_files_only=True,
                    torch_dtype=torch.bfloat16, device_map={'': 'cuda:0'}, attn_implementation='sdpa')
    model = get_peft_model(raw_model, LoraConfig(task_type=TaskType.CAUSAL_LM,
                    r=8, lora_alpha=16, lora_dropout=0.0, target_modules=['q_proj','v_proj']))
    trainable = [(n,p) for n,p in model.named_parameters() if p.requires_grad]
    if not trainable or any('lora_' not in n or 'visual' in n for n,p in trainable):
        raise ValueError('Unexpected trainable base/vision weights')
    if any(isinstance(m, torch.nn.Dropout) and m.p != 0 for m in model.modules()):
        raise ValueError('Nonzero dropout would invalidate identical behavior-policy logprobs')
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    model.enable_input_require_grads()
    model.config.use_cache = False
    optimizer = torch.optim.AdamW([p for n,p in trainable], lr=policy['training']['learning_rate'], weight_decay=0.0)
    write(out/'setup.json', {'seconds': time.monotonic()-begin, 'versions': versions,
          'trainable_parameters': sum(p.numel() for n,p in trainable),
          'total_parameters': sum(p.numel() for p in model.parameters()), 'trainable_names': [n for n,p in trainable],
          'input_token_counts': {u: t.shape[1] for u,t in encoded.items()},
          'validation_data_read': False})
    eos_config = raw_model.generation_config.eos_token_id
    eos = set(eos_config if isinstance(eos_config, list) else [eos_config])
    eos.discard(None)
    generation_base = {'max_new_tokens': policy['training']['max_new_tokens'], 'use_cache': True,
                       'pad_token_id': tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id,
                       'eos_token_id': eos_config}

    def generate(rec, sample=False, disabled=False):
        model.eval()
        tokens = encoded[rec['question_id']].to('cuda')
        kwargs = dict(generation_base, do_sample=sample)
        if sample:
            kwargs.update(temperature=1.0, top_p=1.0, top_k=0)
        context = model.disable_adapter() if disabled else nullcontext()
        start = time.monotonic()
        with context, torch.no_grad():
            sequence = model.generate(input_ids=tokens, attention_mask=torch.ones_like(tokens), **kwargs)
        torch.cuda.synchronize()
        completion = sequence[:, tokens.shape[1]:]
        text = tokenizer.decode(completion[0], skip_special_tokens=True).strip()
        return tokens, completion, {'text': text, 'input_tokens': tokens.shape[1],
                  'output_tokens': completion.shape[1], 'seconds': time.monotonic()-start,
                  'terminated': bool(completion.numel() and completion[0,-1].item() in eos)}

    # Real-model preflight: identical enabled/disabled zero-initialized adapter logits.
    probe = train[0]
    prompt = encoded[probe['question_id']].to('cuda')
    completion = tokenizer(' A', add_special_tokens=False, return_tensors='pt')['input_ids'].to('cuda')
    model.eval()
    with torch.no_grad():
        enabled = core.token_logprobs(model, prompt, completion)
        with model.disable_adapter():
            disabled = core.token_logprobs(model, prompt, completion)
    if not torch.equal(enabled, disabled):
        raise ValueError('Zero-initialized adapter changed baseline logits')
    del enabled, disabled
    model.train()
    logp = core.token_logprobs(model, prompt, completion)
    (-logp.mean()).backward()
    if not any(p.grad is not None and p.grad.abs().sum().item() > 0 for n,p in trainable):
        raise ValueError('Real-model adapter gradient preflight failed')
    optimizer.zero_grad(set_to_none=True)
    write(out/'validation/real_model_preflight.json', {'passed': True, 'adapter_zero_logits_equal': True,
          'nonzero_adapter_gradient': True, 'optimizer_steps': 0, 'peak_allocated_bytes': torch.cuda.max_memory_allocated()})
    torch.cuda.empty_cache()
    initial = {n:p.detach().cpu().clone() for n,p in trainable}
    effective = 0
    train_begin = time.monotonic()
    order = list(train)
    random.Random(0).shuffle(order)
    for index, rec in enumerate(order):
        write(out/'progress.json', {'stage': 'training', 'groups_completed': index, 'planned_groups': len(order),
                                  'optimizer_steps': effective, 'question_id': rec['question_id'], 'updated_at': now()})
        samples = []
        for j in range(policy['training']['group_size']):
            prompt, completion, info = generate(rec, sample=True)
            reward, parsed = core.answer_reward(info['text'], rec['options'], rec['label'], info['terminated'])
            write(out/'training/rollouts'/f'{index:03}-{j}.json',
                  dict(info, question_id=rec['question_id'], reward=reward, parsed=parsed,
                       completion_token_ids=completion[0].tolist(), updated_at=now()))
            with torch.no_grad():
                old_logp = core.token_logprobs(model, prompt, completion).detach()
                with model.disable_adapter():
                    ref_logp = core.token_logprobs(model, prompt, completion).detach()
            samples.append((completion.detach(), old_logp, ref_logp, reward, info, parsed))
        advantages = core.relative_advantages([s[3] for s in samples]).to('cuda')
        mixed = bool(advantages.abs().sum().item())
        losses, kls = [], []
        optimizer.zero_grad(set_to_none=True)
        if mixed:
            model.train()
            for j, (completion, old_logp, ref_logp, reward, info, parsed) in enumerate(samples):
                logp = core.token_logprobs(model, prompt, completion)
                loss, kl = core.grpo_loss(logp, old_logp, ref_logp, advantages[j],
                            beta=policy['training']['kl_beta'], epsilon=policy['training']['ppo_epsilon'])
                (loss/len(samples)).backward()
                losses.append(float(loss.detach())); kls.append(float(kl))
                del logp, loss, kl
            norm = torch.nn.utils.clip_grad_norm_([p for n,p in trainable], 1.0, error_if_nonfinite=True)
            optimizer.step()
            effective += 1
        else:
            norm = torch.tensor(0.0)
        record = {'index': index, 'question_id': rec['question_id'], 'optimizer_step': effective,
                  'mixed_reward_group': mixed, 'advantages': advantages.cpu().tolist(),
                  'losses': losses, 'sampled_kl': kls, 'grad_norm': float(norm),
                  'samples': [{'reward': s[3], 'parsed': s[5], **s[4]} for s in samples], 'updated_at': now()}
        write(out/'training'/f'{index:03}.json', record)
        print('G_RL_TRAIN', index+1, '/', len(order), 'optimizer_steps', effective,
              'rewards', [s[3] for s in samples], flush=True)
        del samples, advantages, prompt, completion, old_logp, ref_logp
    changed = {n:float((p.detach().cpu()-initial[n]).abs().max()) for n,p in trainable}
    if effective == 0 or max(changed.values()) == 0:
        raise ValueError('No effective RL learning occurred; do not report a trained comparison')
    model.save_pretrained(out/'adapter', safe_serialization=True, save_embedding_layers=False)
    write(out/'adapter_seal.json', {str(p.relative_to(out)): core.sha(p)
                                   for p in (out/'adapter').rglob('*') if p.is_file()})
    write(out/'training_summary.json', {'groups': len(order), 'optimizer_steps': effective,
          'seconds': time.monotonic()-train_begin, 'adapter_max_abs_changes': changed,
          'peak_allocated_bytes': torch.cuda.max_memory_allocated(),
          'checkpoint_selection': 'Fixed final checkpoint. Validation labels not yet loaded.'})
    write(out/'progress.json', {'stage': 'training_complete', 'optimizer_steps': effective, 'updated_at': now()})
    verify_dataset(out, 'train')
    verify(out)
    return 0


def evaluate_worker(out):
    """Fresh paired G and final G+RL on exactly the original 300, with one parser."""
    verify(out)
    verify_model(out)
    for name, expected in read(out/'adapter_seal.json').items():
        if core.sha(out/name) != expected:
            raise ValueError('Final adapter checkpoint changed')
    verify_dataset(out, 'validation')
    policy = read(out/'experiment.json')
    validation = rows(out/'validation.jsonl')
    if len(validation) != 300 or any('label' in r for r in validation):
        raise ValueError('Require original 300 label-free evaluation inputs')
    if not (out/'training_summary.json').exists():
        raise ValueError('Training must finish before evaluation')
    import torch
    from transformers import AutoModelForImageTextToText, AutoTokenizer
    from peft import PeftModel
    torch.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    torch.backends.cuda.matmul.allow_tf32 = False
    start = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(policy['model'], local_files_only=True)
    raw = AutoModelForImageTextToText.from_pretrained(policy['model'], local_files_only=True,
              torch_dtype=torch.bfloat16, device_map={'': 'cuda:0'}, attn_implementation='sdpa')
    model = PeftModel.from_pretrained(raw, out/'adapter', is_trainable=False).eval()
    encoded = {}
    parent = Path(policy['parent_G'])
    historical = {}
    for rec in validation:
        uid = rec['question_id']
        t = tokenizer.apply_chat_template(core.prompt_messages(rec['inputs']), tokenize=True,
                                         add_generation_prompt=True, return_tensors='pt')
        if t.shape[1] + policy['training']['max_new_tokens'] > policy['max_context_tokens']:
            raise ValueError('Evaluation context overflow; no truncation')
        old = read(parent/'runs/G_generalized_entities'/uid/'result.json')
        if t.shape[1] != old['input_tokens']:
            raise ValueError('Evaluation tokenization differs from original G')
        historical[uid] = old
        encoded[uid] = t
    write(out/'evaluation_setup.json', {'seconds': time.monotonic()-start, 'prompt_token_counts_match_parent': True})
    validation_rows = {'G': {}, 'G_RL': {}}
    for index, rec in enumerate(validation):
        write(out/'progress.json', {'stage': 'validation', 'completed': index, 'planned': len(validation), 'updated_at': now()})
        for arm in (['G','G_RL'] if index % 2 == 0 else ['G_RL','G']):
            t = encoded[rec['question_id']].to('cuda')
            start = time.monotonic()
            context = model.disable_adapter() if arm == 'G' else nullcontext()
            with context, torch.inference_mode():
                seq = model.generate(input_ids=t, attention_mask=torch.ones_like(t), do_sample=False,
                                     max_new_tokens=policy['training']['max_new_tokens'], use_cache=True)
            torch.cuda.synchronize()
            completion = seq[:,t.shape[1]:]
            result = {'text': tokenizer.decode(completion[0], skip_special_tokens=True).strip(),
                      'input_tokens': t.shape[1], 'output_tokens': completion.shape[1],
                      'seconds': time.monotonic()-start}
            result['parsed'] = core.parse_answer(result['text'], rec['options'])
            result['question_id'] = rec['question_id']
            validation_rows[arm][rec['question_id']] = result
            write(out/'evaluation'/arm/f"{rec['question_id']}.json", result)
            print('G_RL_EVAL', index+1, arm, result['parsed'], flush=True)
    labels = read(out/'validation_labels.json')
    report = core.paired_summary(validation_rows, labels)
    training = read(out/'training_summary.json')
    disagreements = [u for u in historical if core.parse_answer(historical[u]['raw_response'],
                     next(r['options'] for r in validation if r['question_id']==u))['option'] != validation_rows['G'][u]['parsed']['option']]
    report.update(complete=True, updated_at=now(), split_sizes=policy['split_sizes'],
                  optimizer_steps=training['optimizer_steps'], training_seconds=training['seconds'],
                  scope=policy['scope'], memory_frozen=True, validation_used_for_training_or_selection=False,
                  baseline_answer_mismatches_with_historical_G=disagreements,
                  source_overlap=policy['source_overlap'],
                  timing_policy='Shared GPU observations; not a controlled speed comparison.')
    extraction_cost = sum(float(json.loads(line).get('actual_cost') or 0)
                          for group in ['setup','materialized']
                          for path in (out/'memory200'/group).rglob('calls.jsonl')
                          for line in path.read_text().splitlines())
    report['measured_costs'] = {
        'new_training_G_memory_model_seconds': extraction_cost,
        'reader_training_setup_seconds': read(out/'setup.json')['seconds'],
        'reader_training_wall_seconds': training['seconds'],
        'evaluation_setup_seconds': read(out/'evaluation_setup.json')['seconds'],
        'evaluation_generation_seconds': sum(v['seconds'] for arm in validation_rows.values() for v in arm.values()),
        'inherited_G300_extraction_cost_included': False,
        'note': 'Stages are measured separately; queue waits excluded. Inherited G300 memory is reused, not claimed free.'}
    write(out/'report.json', report)
    write(out/'progress.json', {'stage': 'complete', 'updated_at': now()})
    verify(out)
    verify_dataset(out, 'validation')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare','queue','train','evaluate'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--mapping', type=Path)
    parser.add_argument('--full', type=Path)
    parser.add_argument('--data', type=Path, default=Path('data/EgoSchema/egoschema'))
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args)
        return 0
    out = args.out.resolve()
    start = time.monotonic()
    try:
        if args.command == 'queue':
            return queue(out)
        return train_worker(out) if args.command == 'train' else evaluate_worker(out)
    except Exception as exc:
        write(out/'failures'/f'{args.command}-{os.getpid()}.json',
              {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc(),
               'seconds': time.monotonic()-start, 'updated_at': now()})
        if args.command == 'queue':
            write(out/'queue_status.json', {'state': 'failed', 'message': str(exc), 'updated_at': now()})
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
