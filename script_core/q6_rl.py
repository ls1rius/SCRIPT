"""Q6: online REINFORCE-LOO on four contribution actions, never reader weights.

At each literal observation the policy chooses one of four distinct first tokens;
remaining label tokens and JSON syntax are forced. The same categorical likelihood
is used for rollout, optimization and deterministic deployment. No gold or options
enter the filter request. All non-filter calls use the frozen base model.
"""
from contextlib import contextmanager
import json
import math
import torch
from . import q4_retention_training as sft
from .config import digest

LABELS = sft.LABELS


def action_tokens(tokenizer):
    tokens = [tokenizer.encode(label, add_special_tokens=False) for label in LABELS]
    if any(not row for row in tokens) or len({row[0] for row in tokens}) != 4:
        raise ValueError('Q6 needs four distinct initial label tokens')
    return tokens


def probabilities(logits, epsilon=.1, temperature=1.):
    if not 0 <= epsilon < 1 or temperature <= 0:
        raise ValueError('Invalid exploration settings')
    return (1-epsilon)*torch.softmax(logits.float()/temperature, dim=-1)+epsilon/4


def loo_advantages(rewards):
    if len(rewards)<2 or any(r not in (0.,1.) for r in rewards):
        raise ValueError('Q6 requires at least two binary terminal rewards')
    total=sum(rewards)
    return [r-(total-r)/(len(rewards)-1) for r in rewards]


def outcome_reward(uid, answer, train_rewards):
    if uid not in train_rewards:
        raise ValueError('Reward requested outside training200')
    selected=str(answer)
    if selected not in {str(i) for i in range(5)}:
        raise ValueError('Malformed answer is a failure, not a negative reward')
    return float(selected==str(train_rewards[uid]))


def policy_terms(logits, reference_logits, actions, epsilon=.1, temperature=1.):
    p=probabilities(logits,epsilon,temperature)
    q=probabilities(reference_logits.detach(),epsilon,temperature)
    logp=p.log().gather(-1,actions[:,None]).squeeze(-1)
    kl=(p*(p.log()-q.log())).sum(-1)
    return logp,kl


@contextmanager
def reference_weights(model, weights):
    parameters={n:p for n,p in model.named_parameters() if n.endswith(('lora_A','lora_B'))}
    if set(parameters)!=set(weights):raise ValueError('Reference adapter mismatch')
    actor={n:p.detach().clone() for n,p in parameters.items()}
    try:
        with torch.no_grad():
            for n,p in parameters.items():p.copy_(weights[n])
        yield
    finally:
        with torch.no_grad():
            for n,p in parameters.items():p.copy_(actor[n])


def decision_logits(model, trace):
    ids=torch.tensor([trace['input_ids']],device=model.device,dtype=torch.long)
    positions=torch.tensor(trace['positions'],device=model.device,dtype=torch.long)
    result=model(input_ids=ids,attention_mask=torch.ones_like(ids),use_cache=False,
                 logits_to_keep=positions-1)
    choices=torch.tensor(trace['choice_ids'],device=model.device,dtype=torch.long)
    return result.logits[0].index_select(-1,choices).float()


def cached_decision_logits(model, trace, checkpoint=False):
    """Replay the exact rollout chunks, cache positions and projection shapes.

    Full-sequence BF16 projection is not numerically identical to incremental
    sampling. Reentrant checkpoint forward runs without gradients, matching the
    behavior computation; backward rebuilds its local KV cache from scratch.
    """
    ids=torch.tensor([trace['input_ids']],device=model.device,dtype=torch.long)
    choices=torch.tensor(trace['choice_ids'],device=model.device,dtype=torch.long)
    def compute(anchor):
        past=None;processed=0;rows=[]
        for position in trace['positions']:
            result=model(input_ids=ids[:,processed:position],
                attention_mask=torch.ones((1,position),device=model.device,dtype=torch.long),
                past_key_values=past,cache_position=torch.arange(processed,position,device=model.device),
                use_cache=True,logits_to_keep=1)
            past=result.past_key_values;processed=position
            rows.append(result.logits[0,-1].index_select(-1,choices).float())
        return torch.stack(rows)+anchor*0
    if checkpoint:
        from torch.utils.checkpoint import checkpoint as replay
        anchor=torch.zeros((),device=model.device,requires_grad=True)
        return replay(compute,anchor,use_reentrant=True,preserve_rng_state=False)
    return compute(torch.zeros((),device=model.device))


def sample_window(backend, request, settings, sample, generator):
    from .q_repair import validate_request,validate_verdict
    from .o_batch_generation import messages_for
    validate_request('q3_filter',request)
    model,processor=backend.load('vlm');tok=processor.tokenizer
    labels=action_tokens(tok);choice_ids=[row[0] for row in labels]
    messages=messages_for('q3_filter',request)[0]
    prompt=tok.apply_chat_template(messages,tokenize=True,add_generation_prompt=True)
    ids=list(prompt);positions=[];actions=[];old_logps=[];verdict={};response=[]
    past=None;processed=0;forward_tokens=0
    model.eval()
    with torch.inference_mode(),sft.enabled(model,True):
        for i,observation in enumerate(request['observations']):
            fragment=('{' if i==0 else '\",')+json.dumps(observation['source_id'])+':"'
            forced=tok.encode(fragment,add_special_tokens=False)
            response.extend(forced);ids.extend(forced)
            backend.check_deadline()
            if len(ids)+64>settings['max_sequence_length']:
                raise ValueError('Q6 policy context overflow; truncation forbidden')
            new=ids[processed:]
            tensor=torch.tensor([new],device=model.device,dtype=torch.long)
            mask=torch.ones((1,len(ids)),device=model.device,dtype=torch.long)
            result=model(input_ids=tensor,attention_mask=mask,past_key_values=past,
                         cache_position=torch.arange(processed,len(ids),device=model.device),
                         use_cache=True,logits_to_keep=1)
            past=result.past_key_values;processed=len(ids);forward_tokens+=len(new)
            raw=result.logits[0,-1,choice_ids].float()
            p=probabilities(raw,settings['epsilon'],settings['temperature'])
            action=int(torch.multinomial(p,1,generator=generator).item()) if sample else int(p.argmax().item())
            positions.append(len(ids));actions.append(action);old_logps.append(float(p[action].log()))
            verdict[observation['source_id']]=LABELS[action]
            emitted=labels[action]
            ids.extend(emitted);response.extend(emitted)
        emitted=tok.encode('"}',add_special_tokens=False)+[tok.eos_token_id]
        ids.extend(emitted);response.extend(emitted)
    if len(ids)>settings['max_sequence_length']:raise ValueError('Q6 trace overflow')
    validate_verdict('q3_filter',request,verdict)
    raw=tok.decode(response,skip_special_tokens=True)
    if json.loads(raw)!=verdict:raise ValueError('Q6 forced serialization mismatch')
    trace=dict(input_ids=ids,positions=positions,actions=actions,old_logps=old_logps,
        choice_ids=choice_ids,request_hash=digest(request),request=request,
        input_tokens=len(ids),action_count=len(actions))
    backend.model_tokens['vlm']=backend.model_tokens.get('vlm',0)+forward_tokens
    return dict(data=verdict,raw=raw,parse_error=None,ended_normally=True,
        generated_token_ids=response,input_token_ids=prompt,prompt=messages,
        decoder='Q6-four-class-first-token-policy-v1',trace=trace,
        usage=dict(input_tokens=forward_tokens,output_tokens=len(response),stochastic_actions=len(actions) if sample else 0))


def install_backend(LocalModels):
    original=LocalModels.call
    def call(self,operation,inputs):
        settings=self.cfg['models'].get('q6_rl')
        active=bool(settings and operation=='q4_independent_batch' and inputs.get('operation')=='q3_filter')
        if not active:
            output=original(self,operation,inputs)
            if settings and isinstance(output,dict):output['q6_policy_audit']=dict(active=False,operation=operation)
            return output
        from .o_batch_generation import validate_packet
        rows,inner=validate_packet(inputs)
        results=[]
        for row in rows:
            item=sample_window(self,row['request'],settings,getattr(self,'_q6_sampling',False),getattr(self,'_q6_generator',None))
            trace=item.pop('trace')
            item['q6_action_trace']=trace
            if getattr(self,'_q6_sampling',False):
                if getattr(self,'_q6_traces',None) is None:raise ValueError('Missing on-policy trace owner')
                self._q6_traces.append(trace)
            item.update(source_id=row['source_id'],scope=row['scope'],request_hash=digest(row['request']))
            results.append(item)
        return dict(items=results,batch_size=len(rows),scope=inputs['scope'],operation=inner,
            usage={key:sum(item['usage'][key] for item in results) for key in ['input_tokens','output_tokens','stochastic_actions']},
            q6_policy_audit=dict(active=True,operation=inner,sampling=getattr(self,'_q6_sampling',False),
                version=getattr(self,'_q6_policy_version',0),policy='categorical four label initial tokens; remaining syntax/label tokens forced'))
    LocalModels.call=call
