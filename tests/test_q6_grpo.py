"""GRPO probability ratios, clipping, action masking and fixed training lifecycle."""
import math
import tempfile
from pathlib import Path
import types
import unittest
from unittest.mock import patch
import torch
from script_core import q6_grpo as grpo, q6_rl as rl, q4_retention_training as sft
from script import train_q6_grpo as run
from script.scale_live_parallel_pool import read,write,sha
from tests.test_q6_rl import TinyPolicy


class GRPOTests(unittest.TestCase):
    def test_group8_population_standardized_advantages_and_zero_groups(self):
        adv,stats=grpo.advantages([1,0,1,0,0,1,1,0])
        self.assertAlmostEqual(sum(adv),0)
        self.assertAlmostEqual(adv[0],.5/(.5+1e-4))
        self.assertEqual(stats['std'],.5)
        for reward in [0,1]:
            adv,stats=grpo.advantages([reward]*8)
            self.assertEqual(adv,[0.]*8);self.assertTrue(stats['zero_variance'])
        for rewards in [[1],[1,.2],[float('nan'),0]]:
            with self.assertRaises(ValueError):grpo.advantages(rewards)

    def test_clipped_surrogate_matches_independent_scalar_equation(self):
        logits=torch.tensor([[.2,.4,-.1,.1],[.1,-.3,.7,.4]],requires_grad=True)
        ref=torch.zeros_like(logits);actions=[1,2]
        p=rl.probabilities(logits).detach();ratios=[1.7,.5]
        old=[math.log(float(p[i,a])/ratio) for i,(a,ratio) in enumerate(zip(actions,ratios))]
        for advantage in [-1.,1.]:
            loss,stats=grpo.objective(logits,ref,actions,old,advantage,beta=0)
            expected=[-min(ratio*advantage,min(1.2,max(.8,ratio))*advantage) for ratio in ratios]
            self.assertTrue(torch.allclose(loss,torch.tensor(expected),atol=1e-6))
            self.assertEqual(stats['clipped_actions'],2)

    def test_clipping_blocks_improvement_beyond_bounds_but_not_corrective_gradient(self):
        for advantage,ratio,blocked in [(1,2.,True),(-1,.4,True),(1,.4,False),(-1,2.,False)]:
            logits=torch.zeros(1,4,requires_grad=True)
            old=[math.log(.25/ratio)]
            losses,_=grpo.objective(logits,torch.zeros_like(logits),[0],old,advantage,beta=0)
            losses.sum().backward()
            self.assertEqual(bool(torch.count_nonzero(logits.grad)==0),blocked)

    def test_old_policy_and_reference_receive_no_gradient(self):
        logits=torch.tensor([[1.,0.,0.,0.]],requires_grad=True)
        ref=torch.zeros_like(logits,requires_grad=True);old=torch.tensor([-1.386],requires_grad=True)
        loss,_=grpo.objective(logits,ref,[0],old,1.,beta=.02);loss.sum().backward()
        self.assertIsNone(old.grad);self.assertIsNone(ref.grad);self.assertIsNotNone(logits.grad)

    def test_two_updates_reuse_old_likelihood_and_only_lora_has_gradient(self):
        model=TinyPolicy();trace=dict(input_ids=[1,2,3,4],positions=[2,4],actions=[0,1],choice_ids=[10,11,12,13])
        reference={n:p.detach().clone() for n,p in model.named_parameters() if n.endswith(('lora_A','lora_B'))}
        with torch.no_grad(),sft.enabled(model,True):
            logits=rl.cached_decision_logits(model,trace)
            trace['old_logps']=rl.policy_terms(logits,logits,torch.tensor(trace['actions']))[0].tolist()
        old=list(trace['old_logps']);opt=torch.optim.AdamW(sft.adapter_parameters(model),lr=.01)
        for i in range(2):
            opt.zero_grad()
            with sft.enabled(model,True):
                with rl.reference_weights(model,reference),torch.no_grad():ref=rl.cached_decision_logits(model,trace)
                logits=rl.cached_decision_logits(model,trace,checkpoint=True)
                loss,stats=grpo.objective(logits,ref,trace['actions'],trace['old_logps'],1.)
                loss.mean().backward()
            if i==0:self.assertLess(stats['max_behavior_drift'],1e-6)
            self.assertTrue(any(p.grad is not None and p.grad.abs().sum()>0 for p in sft.adapter_parameters(model)))
            self.assertTrue(all(p.grad is None for n,p in model.named_parameters() if not n.endswith(('lora_A','lora_B'))))
            opt.step()
        self.assertEqual(trace['old_logps'],old)

    def test_fixed3_lifecycle_600_groups_4800_trajectories_single_optimizer(self):
        model=TinyPolicy();model.gradient_checkpointing_disable=lambda:None;model.config=types.SimpleNamespace(use_cache=True)
        cfg=run.settings();cfg['learning_rate']=.001
        b=types.SimpleNamespace(load=lambda kind:(model,None),cfg={'models':{'vlm':'tiny'}},deadline=None)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);ids=[str(i) for i in range(200)]
            write(root/'training_config.json',cfg);write(root/'train_ids.json',ids);write(root/'train_rewards.json',{uid:0 for uid in ids})
            write(root/'initial_adapter/adapter_config.json',{'weights_sha256':'initial'})
            calls=[]
            def dispatch(out,epoch,uid,r,version,policy,settings,inherited):
                key=f'{epoch}/{uid}/{r}';job=dict(epoch=epoch,uid=uid,rollout=r,version=version,policy=policy)
                calls.append((epoch,uid,r,version));return key,job
            def await_job(out,key,job,backend,version):
                target=root/'training/rollouts'/str(job['epoch'])/job['uid']/str(job['rollout'])
                trace=dict(input_ids=[1,2],positions=[2],actions=[job['rollout']%2],choice_ids=[10,11,12,13])
                with torch.no_grad(),sft.enabled(model,True):
                    logits=rl.cached_decision_logits(model,trace)
                    trace['old_logps']=rl.policy_terms(logits,logits,torch.tensor(trace['actions']))[0].tolist()
                write(target/'policy_trace.json',dict(policy_version=version,policy_weights_sha256=job['policy']['sha256'],traces=[trace]))
                write(target/'result.json',dict(complete=True,error=None,arms={'Qplus':{'answer':{'selected_option':str(job['rollout']%2)}}},cost_summary={'total':1}))
                return dict(folder=str(target),trace_sha256=sha(target/'policy_trace.json'),result_sha256=sha(target/'result.json'))
            actual_adam=torch.optim.AdamW
            with patch.object(run,'trainable_parameters',sft.adapter_parameters),patch('script.q_v4_validation.validate_live'), \
                 patch('script_core.physical_cuda_pool.status',return_value=None),patch.object(run.parallel,'dispatch',side_effect=dispatch), \
                 patch.object(run.parallel,'await_job',side_effect=await_job),patch('torch.optim.AdamW',wraps=actual_adam) as adam:
                result=run.learn(b,dict(out=str(root),gpu={'index':'test'}))
            self.assertEqual(adam.call_count,1);self.assertEqual(result['completed_groups'],600);self.assertEqual(result['rollouts'],4800)
            self.assertEqual(result['epochs'],3);self.assertEqual(len(calls),4800)
            self.assertEqual(len(set((e,u,r) for e,u,r,v in calls)),4800)
            self.assertEqual(len(read(root/'training/history.json')),3)
            self.assertEqual(result['optimizer_steps'],1200)
            self.assertEqual(result['mixed_reward_groups'],600)
            self.assertEqual(read(root/'validation/pilot.json')['rollouts'],32)
            self.assertTrue(read(root/'training/first_gradient_check.json')['passed'])
            self.assertFalse((root/'evaluation/evaluation_only.json').exists())


if __name__=='__main__':unittest.main()
