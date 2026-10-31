"""Independent, small G-reader RL experiment. No changes to frozen G artifacts."""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

SPLIT_SALT = 'G-RL-internal-20261031-v1'
START = '  Description: '
END = '\n\n###\n\n Questions:'


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def order_key(uid):
    return hashlib.sha256((SPLIT_SALT + ':' + uid).encode()).hexdigest()


def grouped_split(ids, mapping, sizes=None):
    """Group official clips by original Ego4D video, without labels or predictions."""
    sizes = sizes or {'train': 400, 'validation': 100}
    if len(ids) != len(set(ids)) or sum(sizes.values()) != len(ids):
        raise ValueError('Split sizes must partition distinct IDs')
    groups = defaultdict(list)
    for uid in ids:
        groups[mapping[uid]['video_uid']].append(uid)
    result = {s: [] for s in sizes}
    source_groups = {}
    for group in sorted(groups, key=order_key):
        members = sorted(groups[group], key=order_key)
        possible = [s for s in sizes if len(result[s]) + len(members) <= sizes[s]]
        if not possible:
            raise ValueError('Cannot fill requested sizes without splitting a source video')
        split = max(possible, key=lambda s: (sizes[s] - len(result[s])) / sizes[s])
        result[split].extend(members)
        source_groups[group] = split
    for split in result:
        result[split].sort(key=order_key)
        if len(result[split]) != sizes[split]:
            raise ValueError('Incomplete split')
    return result, source_groups


def remaining_200_split(all_ids, original_300, mapping):
    """Honor the user-specified 200/300 split; disclose shared source videos."""
    if len(all_ids) != 500 or len(set(all_ids)) != 500:
        raise ValueError('Require all 500 distinct public questions')
    if len(original_300) != 300 or len(set(original_300)) != 300 or not set(original_300) <= set(all_ids):
        raise ValueError('Require the exact original 300-question cohort')
    remaining = sorted(set(all_ids) - set(original_300), key=order_key)
    train_groups = {mapping[u]['video_uid'] for u in remaining}
    eval_groups = {mapping[u]['video_uid'] for u in original_300}
    shared = train_groups & eval_groups
    return {'train': remaining, 'validation': list(original_300)}, {
        'question_id_overlap': [], 'shared_original_video_ids': sorted(shared),
        'train_clips_from_shared_original_videos': sum(mapping[u]['video_uid'] in shared for u in remaining),
        'validation_clips_from_shared_original_videos': sum(mapping[u]['video_uid'] in shared for u in original_300),
        'policy': 'Exact user-specified 200/300 split retained; source-video overlap disclosed, not silently filtered.'}


def canonical(text):
    return re.sub(r'\s+', ' ', text.replace('**', '').replace('`', '')).strip().casefold().rstrip('.!?')


def parse_answer(text, options):
    """Gold-blind parser: a person named C must never be interpreted as option C."""
    clean = text.replace('**', '').replace('`', '').strip()
    declared = re.findall(r'(?im)^\s*prediction\s*:\s*([^\n]*)$', clean)
    if not declared:
        first = clean.split('\n', 1)[0].strip()
        if re.fullmatch(r'[A-E][.]?', first, re.I):
            declared = [first]
    if len(declared) != 1:
        return {'option': None, 'method': 'unresolved'}
    value = declared[0].strip()
    letter = re.fullmatch(r'([A-E])\s*[.)]?', value, re.I)
    # An explicit "E: <answer>" is unambiguous; "C is ..." is not.
    prefixed = re.fullmatch(r'([A-E])\s*:\s*\S.*', value, re.I)
    if letter or prefixed:
        return {'option': ord((letter or prefixed).group(1).upper()) - 65,
                'method': 'explicit_letter'}
    matches = [i for i, option in enumerate(options) if canonical(option) == canonical(value)]
    if len(matches) == 1:
        return {'option': matches[0], 'method': 'unique_exact_option_text'}
    return {'option': None, 'method': 'unresolved'}


def answer_reward(text, options, label, terminated=True):
    parsed = parse_answer(text, options)
    # No credit for a generation cut off by the token cap, even with an early letter.
    reward = (1.0 if parsed['option'] == label else 0.0) if terminated else 0.0
    if parsed['option'] is None or not terminated:
        reward = -0.1
    return reward, parsed


def fixed_regions(inputs):
    text = inputs['prompts'][0]
    if len(inputs['prompts']) != 1 or text.count(START) != 1 or text.count(END) != 1:
        raise ValueError('Ambiguous original QA template')
    a = text.index(START) + len(START)
    b = text.index(END, a)
    return {**{k: v for k, v in inputs.items() if k != 'prompts'},
            'prefix': text[:a], 'suffix': text[b:]}


def prompt_messages(inputs):
    if len(inputs['prompts']) != 1:
        raise ValueError('This pilot supports one original QA prompt per question')
    return [{'role': 'system', 'content': inputs['head']},
            {'role': 'user', 'content': inputs['prompts'][0]}]


def token_logprobs(model, prompt, completion):
    """Only materialize completion logits; never allocate full-prompt vocabulary logits."""
    import torch
    ids = torch.cat((prompt, completion), dim=1)
    length = completion.shape[1]
    output = model(input_ids=ids, attention_mask=torch.ones_like(ids),
                   use_cache=False, logits_to_keep=length + 1)
    logits = output.logits[:, :-1, :].float()
    if logits.shape[1] != length:
        raise ValueError('Completion alignment changed')
    return logits.log_softmax(-1).gather(-1, completion.unsqueeze(-1)).squeeze(-1)


def relative_advantages(rewards):
    import torch
    r = torch.tensor(rewards, dtype=torch.float32)
    return (r - r.mean()) / (r.std(unbiased=False) + 1e-4)


def grpo_loss(logp, old_logp, ref_logp, advantage, beta=0.02, epsilon=0.2):
    """One PPO epoch, sequence-normalized GRPO with sampled reverse-KL estimator."""
    import torch
    ratio = (logp - old_logp).exp()
    policy = torch.minimum(ratio * advantage, ratio.clamp(1-epsilon, 1+epsilon) * advantage)
    delta = ref_logp - logp
    kl = delta.exp() - delta - 1
    loss = (-policy + beta * kl).mean()
    if not torch.isfinite(loss):
        raise FloatingPointError('Nonfinite GRPO objective')
    return loss, kl.detach().mean()


def paired_summary(rows, labels):
    if set(rows['G']) != set(rows['G_RL']) or set(rows['G']) != set(labels):
        raise ValueError('Evaluation must include exactly the same fixed validation IDs')
    ids = list(labels)
    correct = lambda arm, u: rows[arm][u]['parsed']['option'] == labels[u]
    stats = {arm: {'completed': len(ids), 'correct': sum(correct(arm, u) for u in ids),
                   'accuracy': sum(correct(arm, u) for u in ids) / len(ids),
                   'format_errors': sum(rows[arm][u]['parsed']['option'] is None for u in ids),
                   'mean_input_tokens': sum(rows[arm][u]['input_tokens'] for u in ids) / len(ids),
                   'mean_seconds': sum(rows[arm][u]['seconds'] for u in ids) / len(ids)}
             for arm in rows}
    return {'arms': stats,
            'corrected': [u for u in ids if correct('G_RL', u) and not correct('G', u)],
            'regressed': [u for u in ids if correct('G', u) and not correct('G_RL', u)],
            'delta_percentage_points': 100 * (stats['G_RL']['accuracy'] - stats['G']['accuracy'])}
