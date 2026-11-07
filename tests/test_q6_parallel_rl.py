"""Parallel jobs must retain on-policy barriers and immutable replay provenance."""
import json
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import types
import unittest
import torch
from script import q6_parallel_rl as p
from script.scale_live_parallel_pool import read,write,sha
from script_core import q4_retention_training as sft
from tests.test_q6_rl import TinyPolicy


class ParallelTests(unittest.TestCase):
    def test_only_optional_actor_pool_failure_before_model_load_is_retriable(self):
        self.assertTrue(p.startup_allocation_busy(RuntimeError('CUDA out of memory at videoagent_install_pool'),1))
        self.assertFalse(p.startup_allocation_busy(RuntimeError('CUDA out of memory at videoagent_install_pool'),0))
        self.assertFalse(p.startup_allocation_busy(RuntimeError('CUDA out of memory in policy rollout'),1))
        self.assertFalse(p.startup_allocation_busy(RuntimeError('Other tool failure'),1))

    def test_optimizer_resume_matches_uninterrupted_next_update(self):
        model=TinyPolicy();params=sft.adapter_parameters(model)
        optimizer=torch.optim.AdamW(params,lr=.001)
        x=torch.tensor([[1,2,3]])
        def step(model,opt):
            opt.zero_grad()
            with sft.enabled(model,True):model(input_ids=x).logits.square().mean().backward()
            opt.step()
        step(model,optimizer)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);binding=sft.save_adapter(model,root/'policy',{'base_model':'tiny'})
            torch.save(optimizer.state_dict(),root/'optimizer.pt')
            resumed=TinyPolicy();reference={n:v.detach().clone() for n,v in resumed.named_parameters() if n.endswith(('lora_A','lora_B'))}
            b=types.SimpleNamespace(load=lambda kind:(resumed,None))
            p.load_policy(b,dict(path=str(root/'policy'),sha256=binding['weights_sha256'],version=1))
            restored=torch.optim.AdamW(sft.adapter_parameters(resumed),lr=.001)
            restored.load_state_dict(torch.load(root/'optimizer.pt',weights_only=False))
            step(model,optimizer);step(resumed,restored)
            self.assertTrue(all(torch.equal(a,b) for a,b in zip(sft.adapter_parameters(model),sft.adapter_parameters(resumed))))
            self.assertTrue(any(not torch.equal(v,reference[n]) for n,v in resumed.named_parameters() if n in reference))

    def test_atomic_claims_and_completion_no_duplicate_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write(root/'jobs.json',{str(i):{'state':'pending'} for i in range(8)})
            def two(wid):return [p.claim_job(root,wid),p.claim_job(root,wid)]
            with ThreadPoolExecutor(4) as pool:claimed=[row for pair in pool.map(two,range(4)) for row in pair]
            self.assertEqual(len({key for key,job in claimed}),8)
            for key,job in claimed:p.publish_job(root,key,{'folder':key})
            self.assertTrue(all(job['state']=='complete' for job in read(root/'jobs.json').values()))
            with self.assertRaises(ValueError):p.publish_job(root,'0',{})

    def test_inherited_rollout_reused_only_at_original_policy_version(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);saved=root/'saved';saved.mkdir()
            write(saved/'policy_trace.json',{'policy_version':0});write(saved/'result.json',{'complete':True})
            entry=dict(folder=str(saved),trace_sha256=sha(saved/'policy_trace.json'),result_sha256=sha(saved/'result.json'))
            write(root/'jobs.json',{})
            key,job=p.dispatch(root,1,'train',0,0,{},dict(seed=1),{'1/train/0':entry})
            self.assertTrue(job['inherited']);self.assertEqual(read(root/'jobs.json'),{})
            key,job=p.dispatch(root,1,'train',0,1,{},dict(seed=1),{'1/train/0':entry})
            self.assertEqual(job['state'],'pending');self.assertEqual(job['version'],1)
            (saved/'result.json').write_text('changed')
            with self.assertRaisesRegex(ValueError,'changed'):p.dispatch(root,1,'train',0,0,{},dict(seed=1),{'1/train/0':entry})

    def test_policy_broadcast_copies_weights_without_initializing_another_lora(self):
        model=TinyPolicy();params={n:v for n,v in model.named_parameters() if n.endswith(('lora_A','lora_B'))}
        original_ids={n:id(v) for n,v in params.items()};base={n:v.detach().clone() for n,v in model.named_parameters() if n not in params}
        with torch.no_grad():
            for n,v in params.items():v.fill_(.15)
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)/'policy';binding=sft.save_adapter(model,folder,{'base_model':'tiny','policy_version':7})
            with torch.no_grad():
                for v in params.values():v.zero_()
            backend=types.SimpleNamespace(load=lambda kind:(model,None))
            result=p.load_policy(backend,dict(path=str(folder),sha256=binding['weights_sha256'],version=7))
            self.assertEqual(result['version'],7);self.assertEqual(backend._q6_policy_version,7)
            self.assertEqual(original_ids,{n:id(v) for n,v in model.named_parameters() if n in params})
            self.assertTrue(all(torch.equal(v,base[n]) for n,v in model.named_parameters() if n in base))
            self.assertTrue(all(torch.allclose(v,torch.full_like(v,.15)) for v in params.values()))
            with self.assertRaisesRegex(ValueError,'hash'):p.load_policy(backend,dict(path=str(folder),sha256='bad',version=8))

    def test_waiter_rejects_reply_from_another_policy(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write(root/'jobs.json',{'key':{'state':'complete','version':3,'result':{}}})
            with self.assertRaisesRegex(ValueError,'Off-policy'):p.await_job(root,'key',{'state':'pending'},None,4)

    def test_failure_stops_new_claims_and_preserves_first_reason(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write(root/'jobs.json',{'key':{'state':'pending'}})
            p.fail(root,1,RuntimeError('first'));p.fail(root,2,RuntimeError('second'))
            self.assertEqual(read(root/'failure.json')['message'],'first')
            self.assertIsNone(p.claim_job(root,3))


if __name__=='__main__':unittest.main()
