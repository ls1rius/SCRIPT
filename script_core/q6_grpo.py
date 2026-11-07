"""GRPO objective over stochastic contribution actions, excluding forced tokens."""
import torch
from .q6_rl import probabilities


def advantages(rewards, epsilon=1e-4):
    values = torch.as_tensor(rewards, dtype=torch.float64)
    if values.ndim != 1 or len(values) < 2 or not torch.isfinite(values).all():
        raise ValueError('Finite reward group required')
    if not ((values == 0) | (values == 1)).all():
        raise ValueError('Only original binary correctness rewards allowed')
    std = values.std(unbiased=False)
    normalized = (values-values.mean())/(std+epsilon)
    return normalized.tolist(), dict(mean=float(values.mean()), std=float(std),
                                    zero_variance=bool(std == 0))


def objective(logits, reference_logits, actions, old_logps, advantage,
              exploration=.1, temperature=1., clip_epsilon=.2, beta=.02):
    if not 0 < clip_epsilon < 1 or beta < 0:
        raise ValueError('Invalid GRPO clipping or KL coefficient')
    p = probabilities(logits, exploration, temperature)
    q = probabilities(reference_logits.detach(), exploration, temperature)
    actions = torch.as_tensor(actions, device=logits.device, dtype=torch.long)
    old = torch.as_tensor(old_logps, device=logits.device, dtype=torch.float32).detach()
    if logits.ndim != 2 or logits.shape[-1] != 4 or reference_logits.shape != logits.shape or not len(actions) or actions.shape != old.shape or len(actions) != len(logits):
        raise ValueError('GRPO requires aligned four-action traces')
    if not torch.isfinite(old).all():
        raise ValueError('Nonfinite old policy likelihood')
    logp = p.log().gather(-1, actions[:, None]).squeeze(-1)
    ratio = (logp-old).exp()
    clipped = ratio.clamp(1-clip_epsilon, 1+clip_epsilon)
    advantage = torch.as_tensor(advantage, device=logits.device, dtype=torch.float32).detach()
    surrogate = torch.minimum(ratio*advantage, clipped*advantage)
    kl = (p*(p.log()-q.log())).sum(-1)
    loss = -surrogate+beta*kl
    if not torch.isfinite(loss).all():
        raise ValueError('Nonfinite GRPO objective')
    with torch.no_grad():
        diagnostics = dict(pg_sum=float((-surrogate).sum()), kl_sum=float(kl.sum()),
            clipped_actions=int(((ratio < 1-clip_epsilon) | (ratio > 1+clip_epsilon)).sum()),
            actions=len(actions), ratio_min=float(ratio.min()), ratio_max=float(ratio.max()),
            max_behavior_drift=float((logp-old).abs().max()),
            entropy_sum=float((-(p*p.log()).sum(-1)).sum()))
    return loss, diagnostics
