"""Meaningful RL checks: causal likelihood, reward gradients and isolation."""
import json
import types
import unittest
from unittest.mock import patch
import torch
from torch import nn
from script_core import q6_rl as rl,q4_retention_training as sft


class Tokenizer:
    eos_token_id=0
    def encode(self,text,add_special_tokens=False):return [ord(c) for c in text]
    def decode(self,ids,skip_special_tokens=True):return ''.join(chr(i) for i in ids if i)
    def apply_chat_template(self,messages,tokenize=True,add_generation_prompt=True):return [80,58]


class TinyPolicy(nn.Module):
    def __init__(self,device='cpu'):
        super().__init__();torch.manual_seed(12)
        self.embed=nn.Embedding(128,8)
        self.language_model=nn.Module();attention=nn.Module()
        attention.q_proj=nn.Linear(8,8);attention.v_proj=nn.Linear(8,8)
        layer=nn.Module();layer.self_attn=attention;self.language_model.layers=nn.ModuleList([layer])
        self.head=nn.Linear(8,128);self.to(device);sft.attach(self,2,4,0)
    @property
    def device(self):return self.embed.weight.device
    def forward(self,input_ids,attention_mask=None,use_cache=False,past_key_values=None,logits_to_keep=None,cache_position=None):
        positions=torch.arange(input_ids.shape[1],device=self.device) if cache_position is None else cache_position
        hidden=(self.embed(input_ids)+.01*positions.to(self.embed.weight.dtype)[None,:,None]).cumsum(1)
        if past_key_values is not None:hidden=hidden+past_key_values[:,None,:]
        attention=self.language_model.layers[0].self_attn
        logits=self.head(torch.tanh(attention.q_proj(hidden)+attention.v_proj(hidden)))
        if isinstance(logits_to_keep,int):logits=logits[:,-logits_to_keep:]
        elif logits_to_keep is not None:logits=logits[:,logits_to_keep]
        return types.SimpleNamespace(logits=logits,past_key_values=hidden[:,-1,:].detach() if use_cache else None)


def backend(model):
    return types.SimpleNamespace(load=lambda kind:(model,types.SimpleNamespace(tokenizer=Tokenizer())),
        check_deadline=lambda:None,model_tokens={})


def request():
    return dict(question='What is M doing?',observations=[
        dict(source_id='S0',text='M opens a tin.',intervals=[[0,1]]),
        dict(source_id='S1',text='M checks the lid.',intervals=[[1,2]])],context=[])


class RLTests(unittest.TestCase):
    def test_resident_leaf_call_has_independent_hard_timeout(self):
        import subprocess,sys,tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            code='''
import time,types,sys
from script.train_q6_rl import LocalProxy
b=types.SimpleNamespace(cfg={'models':{'retention_adapter':sys.argv[1]+'/adapter'}},deadline=None,
    call=lambda operation,inputs:time.sleep(10))
LocalProxy(b).call('test_timeout',{},.05)
'''
            p=subprocess.run([sys.executable,'-c',code,directory],capture_output=True,text=True,timeout=20)
            self.assertEqual(p.returncode,124,p.stderr)
            result=json.loads((Path(directory)/'hard_deadline_failure.json').read_text())
            self.assertEqual(result['operation'],'test_timeout')

    def test_loo_rewards_and_invalid_outcomes(self):
        self.assertEqual(rl.loo_advantages([1.,0.]),[1.,-1.])
        self.assertEqual(rl.loo_advantages([1.,1.]),[0.,0.])
        self.assertEqual(rl.loo_advantages([0.,0.]),[0.,0.])
        for rewards in [[1.],[1.,float('nan')],[1.,.5]]:
            with self.assertRaises(ValueError):rl.loo_advantages(rewards)
        self.assertEqual(rl.outcome_reward('train','2',{'train':2}),1.)
        with self.assertRaises(ValueError):rl.outcome_reward('validation','2',{'train':2})
        with self.assertRaises(ValueError):rl.outcome_reward('train','bad',{'train':2})

    def test_terminal_reward_increases_good_action_and_decreases_bad(self):
        logits=nn.Parameter(torch.zeros(2,4));reference=logits.detach().clone()
        before=rl.probabilities(logits).detach().clone()
        opt=torch.optim.SGD([logits],lr=.2)
        logp,kl=rl.policy_terms(logits,reference,torch.tensor([0,1]))
        loss=-(logp*torch.tensor([1.,-1.])).mean()+.02*kl.mean()
        loss.backward();opt.step();after=rl.probabilities(logits)
        self.assertGreater(after[0,0],before[0,0]);self.assertLess(after[1,1],before[1,1])
        self.assertTrue(torch.equal(reference,torch.zeros_like(reference)))

    def test_reference_restores_actor_after_failure(self):
        model=TinyPolicy();parameters={n:p for n,p in model.named_parameters() if n.endswith(('lora_A','lora_B'))}
        actor={n:p.detach().clone() for n,p in parameters.items()};ref={n:torch.ones_like(p) for n,p in parameters.items()}
        with self.assertRaises(RuntimeError):
            with rl.reference_weights(model,ref):raise RuntimeError('test')
        self.assertTrue(all(torch.equal(p,actor[n]) for n,p in parameters.items()))

    def test_cached_sampling_and_full_causal_likelihood_are_identical(self):
        model=TinyPolicy();b=backend(model)
        with patch('script_core.o_batch_generation.messages_for',return_value=([{'role':'user','content':'no gold'}],None,None,None)):
            item=rl.sample_window(b,request(),dict(epsilon=.1,temperature=1.,max_sequence_length=2048),True,
                                  torch.Generator().manual_seed(5))
        trace=item['trace']
        with sft.enabled(model,True):logits=rl.decision_logits(model,trace)
        actions=torch.tensor(trace['actions']);logp,_=rl.policy_terms(logits,logits.detach(),actions)
        self.assertTrue(torch.allclose(logp,torch.tensor(trace['old_logps']),atol=2e-6))
        self.assertEqual(json.loads(item['raw']),item['data']);self.assertEqual(len(trace['actions']),2)
        self.assertEqual(set(trace['request']),{'question','observations','context'})
        (-logp.sum()).backward()
        self.assertTrue(any(p.grad is not None and p.grad.abs().sum()>0 for p in sft.adapter_parameters(model)))
        self.assertTrue(all(p.grad is None for n,p in model.named_parameters() if not n.endswith(('lora_A','lora_B'))))
        self.assertTrue(all(not m.enabled for m in model.modules() if isinstance(m,sft.LowRankLinear)))

    def test_checkpointed_incremental_replay_matches_behavior_and_has_gradient(self):
        model=TinyPolicy();b=backend(model)
        with patch('script_core.o_batch_generation.messages_for',return_value=([{'role':'user','content':'no gold'}],None,None,None)):
            item=rl.sample_window(b,request(),dict(epsilon=.1,temperature=1.,max_sequence_length=2048),True,torch.Generator().manual_seed(7))
        trace=item['trace'];model.eval()
        with sft.enabled(model,True):
            logits=rl.cached_decision_logits(model,trace,checkpoint=True)
            logp,_=rl.policy_terms(logits,logits.detach(),torch.tensor(trace['actions']))
            self.assertTrue(torch.allclose(logp.detach(),torch.tensor(trace['old_logps']),atol=2e-6))
            (-logp.sum()).backward()
        self.assertTrue(any(p.grad is not None and p.grad.abs().sum()>0 for p in sft.adapter_parameters(model)))
        self.assertTrue(all(p.grad is None for n,p in model.named_parameters() if not n.endswith(('lora_A','lora_B'))))

    def test_exploration_and_kl_are_actual_categorical_distributions(self):
        logits=torch.tensor([[1000.,-1000.,-1000.,-1000.]])
        p=rl.probabilities(logits)
        self.assertTrue(torch.allclose(p.sum(-1),torch.ones(1)))
        self.assertTrue((p>=.025).all())
        logp,kl=rl.policy_terms(logits,logits,torch.tensor([1]))
        self.assertTrue(torch.isfinite(logp).all());self.assertEqual(float(kl),0.)

    def test_gold_and_options_rejected_at_filter_boundary(self):
        for key in ['options','correct_index','reward']:
            bad=dict(request(),**{key:0})
            with self.assertRaises(ValueError):rl.sample_window(backend(TinyPolicy()),bad,{},False,None)

    def test_actual_tokenizer_has_distinct_class_tokens_and_exact_json(self):
        from transformers import AutoTokenizer
        tok=AutoTokenizer.from_pretrained('/mnt/ssd2/hli/work/checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        tokens=rl.action_tokens(tok)
        self.assertEqual(len(set(row[0] for row in tokens)),4)
        for i,label in enumerate(rl.LABELS):
            ids=tok.encode('{"S0":"',add_special_tokens=False)+tokens[i]+tok.encode('"}',add_special_tokens=False)
            self.assertEqual(json.loads(tok.decode(ids)),{'S0':label})
        from itertools import product
        for actions in product(range(4),repeat=2):
            ids=[];target={}
            for i,action in enumerate(actions):
                target[f'S{i}']=rl.LABELS[action]
                ids+=tok.encode(('{' if i==0 else '\",')+json.dumps(f'S{i}')+':"',add_special_tokens=False)+tokens[action]
            ids+=tok.encode('"}',add_special_tokens=False)
            self.assertEqual(ids,tok.encode(json.dumps(target,separators=(',',':')),add_special_tokens=False))


if __name__=='__main__':unittest.main()

class RLLifecycleTests(unittest.TestCase):
    def test_six_complete_passes_one_optimizer_no_validation_reward(self):
        import tempfile
        from pathlib import Path
        from script import train_q6_rl as runner
        from script.scale_live_parallel_pool import write,sha
        model=TinyPolicy()
        # The lifecycle enforces56 adapters; add27 frozen toy attention layers.
        for _ in range(27):
            attention=nn.Module();attention.q_proj=sft.LowRankLinear(nn.Linear(8,8),2,4,0)
            attention.v_proj=sft.LowRankLinear(nn.Linear(8,8),2,4,0)
            layer=nn.Module();layer.self_attn=attention;model.language_model.layers.append(layer)
        model.enable_input_require_grads=lambda:None;model.disable_input_require_grads=lambda:None
        model.gradient_checkpointing_enable=lambda **kwargs:None;model.gradient_checkpointing_disable=lambda:None
        model.config=types.SimpleNamespace(use_cache=True)
        b=backend(model);b.cfg={'models':{'vlm':'tiny'}};b.deadline=None
        forward_ids=[1,2,3,100,4];positions=[3];choices=[100,99,105,117]
        calls=[];optimizer_builds=[]
        original_optimizer=torch.optim.AdamW
        def optimizer(*args,**kwargs):optimizer_builds.append(1);return original_optimizer(*args,**kwargs)
        def fake_run_case(proxy,cfg,question,sources,original,target,archive,mode):
            target.mkdir(parents=True)
            action=int(target.name)
            trace=dict(input_ids=forward_ids,positions=positions,actions=[action],choice_ids=choices)
            with torch.no_grad(),sft.enabled(model,True):
                logits=rl.decision_logits(model,trace)
                trace['old_logps']=[float(rl.probabilities(logits)[0,action].log())]
            b._q6_traces.append(trace);calls.append(question['question_id'])
            return dict(arms={'Qplus':{'answer':{'selected_option':str(action)}}},
                        cost_summary={'total':.01},source_dependency_seconds=1.)
        def gate_call(proxy,operation,inputs,timeout):
            trace=dict(input_ids=forward_ids,positions=positions,actions=[0],choice_ids=choices)
            with torch.no_grad(),sft.enabled(model,True):
                logits=rl.decision_logits(model,trace)
                trace['old_logps']=[float(rl.probabilities(logits)[0,0].log())]
            return dict(items=[dict(q6_action_trace=trace)])
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);ids=[f'train-{i}' for i in range(200)]
            write(out/'train_ids.json',ids);write(out/'train_rewards.json',{uid:0 for uid in ids})
            write(out/'experiment.json',{'initial_adapter_sha256':'initial'})
            write(out/'training_config.json',dict(seed=9,epochs=6,group_size=2,learning_rate=1e-4,betas=[.9,.999],
                epsilon=.1,temperature=1.,kl_coefficient=.02,policy_gradient_normalizer=256.,gradient_clip=1.,algorithm='REINFORCE-LOO'))
            for uid in ids:
                case=out/'train_cases'/uid
                for name,value in [('question.json',{'question_id':uid}),('sentences.json',[]),('original_qa.json',{}),('archive.json',{})]:write(case/name,value)
                write(case/'source_binding.json',{'files':{n:sha(case/n) for n in ['question.json','sentences.json','original_qa.json','archive.json']}})
            with patch('script.run_q_v4_experiment.run_case',fake_run_case),patch('script.q_v4_validation.validate_live'),patch.object(runner.LocalProxy,'call',gate_call),patch('script_core.physical_cuda_pool.status',return_value=None),patch('torch.optim.AdamW',optimizer),patch('torch.cuda.max_memory_allocated',return_value=0):
                result=runner.train_loaded(b,{'out':str(out),'gpu':{'index':'cpu'}})
            self.assertTrue(result['complete']);self.assertEqual(result['epochs'],6)
            self.assertEqual(len(calls),2400);self.assertEqual(result['optimizer_steps'],1200)
            self.assertEqual(result['policy_gradient_updates'],1200);self.assertEqual(len(optimizer_builds),1)
            self.assertEqual(set(calls),set(ids));self.assertFalse((out/'evaluation').exists())
            self.assertTrue((out/'training/first_gradient_check.json').exists())
            self.assertTrue((out/'adapter/adapter.safetensors').exists())
            self.assertTrue(all(p.grad is None for n,p in model.named_parameters() if not n.endswith(('lora_A','lora_B'))))
