"""Isolated LoRA for whole-observation contribution decisions, never the readers.

This module is installed only into a new frozen training runtime. Existing Q4
workers do not import it. Teacher labels are privileged training supervision;
student prompts retain the exact original question-only Q4 contract.
"""
import contextlib
import hashlib
import json
import math
from pathlib import Path
import re

import torch
from torch import nn

LABELS = ('direct', 'context', 'irrelevant', 'uncertain')
TEACHER_PROMPT = '''Training-only evidence contribution annotation. The reference
answer is provided as supervision, NOT as an observed video fact. Apply the same
four contribution classes to every target observation. Use the options to understand
which distinctions matter, but retain relevant competing evidence and counterevidence
even if they do not support the reference answer. Concrete local steps and necessary
context can contribute without independently proving the whole answer. Preserve
negation, uncertainty, actors, object roles and time. Do not turn the reference
answer or question premises into facts, invent observations, or discard conflicting
observations to make the reference look correct. Judge only the literal target
observations with their supplied context. If reference details are unobserved, do
not manufacture support. Output the required ID-to-contribution JSON only.'''


class LowRankLinear(nn.Module):
    def __init__(self, base, rank=8, alpha=16, dropout=.05):
        super().__init__()
        self.base = base
        self.rank, self.alpha = rank, alpha
        self.enabled = False
        self.dropout = nn.Dropout(dropout)
        self.lora_A = nn.Parameter(torch.empty(rank, base.in_features, device=base.weight.device, dtype=torch.float32))
        self.lora_B = nn.Parameter(torch.zeros(base.out_features, rank, device=base.weight.device, dtype=torch.float32))
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
        self.base.requires_grad_(False)

    def forward(self, x):
        value = self.base(x)
        if self.enabled:
            delta = torch.nn.functional.linear(torch.nn.functional.linear(self.dropout(x).float(), self.lora_A), self.lora_B)
            value = value + (delta * (self.alpha/self.rank)).to(value.dtype)
        return value


def attach(model, rank=8, alpha=16, dropout=.05):
    if any(isinstance(m, LowRankLinear) for m in model.modules()):
        raise ValueError('Adapter already attached')
    model.requires_grad_(False)
    targets = [(n, m) for n, m in model.named_modules()
               if 'language_model.layers.' in n and n.endswith(('self_attn.q_proj', 'self_attn.v_proj'))]
    if not targets:
        raise ValueError('No language attention projections found; refuse silent training of wrong modules')
    for name, module in targets:
        parent, attr = name.rsplit('.', 1)
        setattr(model.get_submodule(parent), attr, LowRankLinear(module, rank, alpha, dropout))
    return {n: m for n, m in model.named_modules() if isinstance(m, LowRankLinear)}


def adapter_parameters(model):
    return [p for n, p in model.named_parameters() if n.endswith(('lora_A', 'lora_B'))]


@contextlib.contextmanager
def enabled(model, active):
    modules = [m for m in model.modules() if isinstance(m, LowRankLinear)]
    old = [m.enabled for m in modules]
    try:
        for m in modules:
            m.enabled = active
        yield
    finally:
        for m, v in zip(modules, old):
            m.enabled = v


def save_adapter(model, folder, metadata):
    from safetensors.torch import save_file
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    tensors = {n: p.detach().cpu().contiguous() for n, p in model.named_parameters() if n.endswith(('lora_A', 'lora_B'))}
    if not tensors or not any(torch.count_nonzero(v).item() for n, v in tensors.items() if n.endswith('lora_B')):
        raise ValueError('Refuse an untrained or empty adapter')
    save_file(tensors, str(folder/'adapter.safetensors'))
    modules = {n: dict(rank=m.rank, alpha=m.alpha, dropout=m.dropout.p)
               for n, m in model.named_modules() if isinstance(m, LowRankLinear)}
    cfg = dict(metadata, modules=modules, trainable_parameters=sum(v.numel() for v in tensors.values()),
               allowed_operation='q3_filter', weights_sha256=hashlib.sha256((folder/'adapter.safetensors').read_bytes()).hexdigest())
    (folder/'adapter_config.json').write_text(json.dumps(cfg, indent=2)+'\n')
    return cfg


def load_adapter(model, folder):
    from safetensors.torch import load_file
    folder = Path(folder)
    cfg = json.loads((folder/'adapter_config.json').read_text())
    if cfg['allowed_operation'] != 'q3_filter' or hashlib.sha256((folder/'adapter.safetensors').read_bytes()).hexdigest() != cfg['weights_sha256']:
        raise ValueError('Invalid retention adapter binding')
    settings = list(cfg['modules'].values())
    if not settings or any(x != settings[0] for x in settings):
        raise ValueError('Nonuniform adapter settings')
    modules = attach(model, **settings[0])
    if set(modules) != set(cfg['modules']):
        raise ValueError('Adapter module names do not match base model')
    tensors = load_file(str(folder/'adapter.safetensors'), device='cpu')
    expected = {n: p for n, p in model.named_parameters() if n.endswith(('lora_A', 'lora_B'))}
    if set(expected) != set(tensors):
        raise ValueError('Adapter weights incomplete or unexpected')
    with torch.no_grad():
        for n, p in expected.items():
            if p.shape != tensors[n].shape or not torch.isfinite(tensors[n]).all():
                raise ValueError('Invalid adapter tensor '+n)
            p.copy_(tensors[n])
    model.eval()
    return cfg


def teacher_generate(backend, inputs):
    from . import o_batch_generation as batch
    from .q_repair import validate_request
    packet = inputs['packet']
    uid = packet['scope']['question_id']
    allowlist = json.loads(Path(backend.cfg['models']['retention_training_ids']).read_text())
    if uid not in allowlist or inputs['question_id'] != uid:
        raise ValueError('Teacher may annotate only the 200 training questions')
    if packet['operation'] != 'q3_filter':
        raise ValueError('Teacher only annotates contribution labels')
    options, correct = inputs['options'], inputs['correct_index']
    if not isinstance(correct, int) or correct not in range(len(options)):
        raise ValueError('Invalid training reference')
    original = batch.messages_for
    def privileged(operation, request):
        validate_request(operation, request)
        messages, schema, wire, aliases = original(operation, request)
        messages[0]['content'] += '\n'+TEACHER_PROMPT
        messages[1]['content'] = json.dumps(dict(observation_request=request, training_reference=dict(
            options=options, correct_index=correct, correct_option=options[correct])), ensure_ascii=False)
        return messages, schema, wire, aliases
    # One serial worker owns the model; restore the global even if generation fails.
    batch.messages_for = privileged
    try:
        output = batch.generate_batch(backend, packet)
    finally:
        batch.messages_for = original
    output['supervision_scope'] = 'training_only_options_and_gold; not question_only'
    return output


def install_backend(LocalModels):
    original = LocalModels.call
    def call(self, operation, inputs):
        if operation == 'q4_training_teacher_batch':
            return teacher_generate(self, inputs)
        path = self.cfg['models'].get('retention_adapter')
        if not path:
            return original(self, operation, inputs)
        self.prepare_vlm()
        model, _ = self.load('vlm')
        if not hasattr(self, '_retention_adapter_binding'):
            cfg = load_adapter(model, path)
            if Path(cfg['base_model']).resolve() != Path(self.cfg['models']['vlm']).resolve():
                raise ValueError('Adapter base model mismatch')
            self._retention_adapter_binding = cfg
        active = operation == 'q4_independent_batch' and inputs.get('operation') == 'q3_filter'
        with enabled(model, active):
            result = original(self, operation, inputs)
        if isinstance(result, dict):
            result['retention_adapter_audit'] = dict(active=active, allowed_operation='q3_filter',
                weights_sha256=self._retention_adapter_binding['weights_sha256'])
        return result
    LocalModels.call = call


def encode_example(tokenizer, request, target, max_length=2048):
    from .o_batch_generation import messages_for
    from .q_repair import validate_verdict
    validate_verdict('q3_filter', request, target)
    messages = messages_for('q3_filter', request)[0]
    prefix = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    answer = json.dumps(target, ensure_ascii=False, separators=(',', ':'))
    text = prefix+answer+tokenizer.eos_token
    enc = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offsets = enc['input_ids'], enc['offset_mapping']
    if len(ids) > max_length:
        raise ValueError('Training input exceeds length; truncation is forbidden')
    # Only class value tokens are supervised. Copying IDs/format must not dominate.
    spans = [(len(prefix)+m.start(1), len(prefix)+m.end(1), m.group(1))
             for m in re.finditer(r':"(direct|context|irrelevant|uncertain)"', answer)]
    positions, labels = [], []
    for lo, hi, label in spans:
        pos = [i for i, (a, b) in enumerate(offsets) if b > lo and a < hi and b > a]
        if not pos or min(pos) == 0:
            raise ValueError('Missing class token supervision')
        for i in pos:
            if i in positions:
                raise ValueError('Overlapping class token labels')
            positions.append(i)
            labels.append((label, 1/len(pos)))
    if len(spans) != len(target) or not positions:
        raise ValueError('Incomplete target supervision')
    return dict(input_ids=ids, positions=positions, class_weights=labels,
                input_tokens=len(ids), supervised_tokens=len(positions), target=target)


def loss_for(model, example, class_weights):
    device = model.device
    ids = torch.tensor([example['input_ids']], device=device)
    positions = torch.tensor(example['positions'], device=device)
    # Project only logits that predict class tokens; avoid a full vocab matrix for
    # every prompt token. All prompt tokens still participate in self attention.
    result = model(input_ids=ids, attention_mask=torch.ones_like(ids), use_cache=False,
                   logits_to_keep=positions-1)
    losses = torch.nn.functional.cross_entropy(result.logits[0].float(), ids[0, positions], reduction='none')
    weights = torch.tensor([class_weights[label]*w for label, w in example['class_weights']], device=device)
    return (losses*weights).sum()/weights.sum()
