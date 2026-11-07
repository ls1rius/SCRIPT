"""Boundary tests for training isolation, actual gradients and reader freezing."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import torch
from torch import nn

from script_core import q4_retention_training as t
from script_core.q_repair import windows
from script.q_repair_validation import source


class Tiny(nn.Module):
    def __init__(self):
        super().__init__()
        attention=nn.Module()
        attention.q_proj=nn.Linear(4,4)
        attention.v_proj=nn.Linear(4,4)
        layer=nn.Module();layer.self_attn=attention
        self.language_model=nn.Module()
        self.language_model.layers=nn.ModuleList([layer])
        self.visual=nn.Linear(4,4)

    def forward(self,x):
        a=self.language_model.layers[0].self_attn
        return a.q_proj(x)+a.v_proj(x)


class TrainingTests(unittest.TestCase):
    def test_gradients_frozen_base_disable_and_actual_round_trip(self):
        torch.manual_seed(42);model=Tiny();x=torch.randn(3,4)
        base=model(x).detach().clone()
        snapshots={n:p.detach().clone() for n,p in model.named_parameters()}
        modules=t.attach(model,rank=2,alpha=4,dropout=0)
        self.assertEqual(len(modules),2)
        self.assertFalse(model.visual.weight.requires_grad)
        optimizer=torch.optim.AdamW(t.adapter_parameters(model),lr=.01)
        with t.enabled(model,True):
            for _ in range(2):
                optimizer.zero_grad();model(x).square().mean().backward();optimizer.step()
        self.assertTrue(torch.equal(model(x),base))
        for n,p in model.named_parameters():
            if n.endswith(('lora_A','lora_B')):continue
            old=n.replace('.base.','.')
            self.assertTrue(torch.equal(p,snapshots[old]));self.assertIsNone(p.grad)
        with t.enabled(model,True):
            result=model(x).detach().clone()
        self.assertFalse(torch.equal(result,base))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'adapter';t.save_adapter(model,path,dict(base_model='tiny'))
            fresh=Tiny()
            with torch.no_grad():
                for n,p in fresh.named_parameters():p.copy_(snapshots[n])
            t.load_adapter(fresh,path)
            with t.enabled(fresh,True):self.assertTrue(torch.equal(fresh(x),result))
            self.assertTrue(torch.equal(fresh(x),base))
            weights=path/'adapter.safetensors';weights.write_bytes(weights.read_bytes()+b'changed')
            with self.assertRaisesRegex(ValueError,'binding'):t.load_adapter(Tiny(),path)

    def test_adapter_context_restored_after_failure(self):
        model=Tiny();modules=t.attach(model,2,4,0)
        with self.assertRaises(RuntimeError):
            with t.enabled(model,True):
                self.assertTrue(all(m.enabled for m in modules.values()))
                raise RuntimeError('generation interrupted')
        self.assertTrue(all(not m.enabled for m in modules.values()))

    def test_backend_routes_only_filter_to_adapter(self):
        model=Tiny();t.attach(model,2,4,0)
        class Backend:
            def __init__(self):
                self.cfg={'models':{'retention_adapter':'test','vlm':'tiny'}}
                self._retention_adapter_binding={'weights_sha256':'abc'}
            def prepare_vlm(self):pass
            def load(self,kind):return model,None
            def call(self,operation,inputs):
                return {'active_at_call':any(m.enabled for m in model.modules() if isinstance(m,t.LowRankLinear))}
        t.install_backend(Backend);backend=Backend()
        for outer,inner,expected in [('q4_independent_batch','q3_filter',True),
            ('q4_independent_batch','q3_reconsider',False),('q4_independent_batch','q4_pair',False),
            ('q_answer',None,False),('qplus_answer',None,False),('q3_independent_batch','q3_filter',False)]:
            value=backend.call(outer,{'operation':inner})
            self.assertEqual(value['active_at_call'],expected)
            self.assertEqual(value['retention_adapter_audit']['active'],expected)
        self.assertTrue(all(not m.enabled for m in model.modules() if isinstance(m,t.LowRankLinear)))

    def test_teacher_rejects_test_question_before_loading_model(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'ids.json';path.write_text(json.dumps(['train']))
            backend=types.SimpleNamespace(cfg={'models':{'retention_training_ids':str(path)}})
            with self.assertRaisesRegex(ValueError,'200 training'):
                t.teacher_generate(backend,dict(question_id='test',packet={'scope':{'question_id':'test'}}))

    def test_real_tokenizer_supervision_and_no_truncation(self):
        from transformers import AutoTokenizer
        tokenizer=AutoTokenizer.from_pretrained('/mnt/ssd2/hli/work/checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        req=windows('Which object is cleaned?',[source('S0','M cleans a plate.',0),source('S1','M sees a cloud.',1)])[0]
        target={'S0':'direct','S1':'irrelevant'}
        encoded=t.encode_example(tokenizer,req,target)
        self.assertGreater(encoded['input_tokens'],encoded['supervised_tokens'])
        self.assertEqual({label for label,w in encoded['class_weights']},{'direct','irrelevant'})
        self.assertNotIn('options',req);self.assertNotIn('correct_index',req)
        with self.assertRaisesRegex(ValueError,'truncation'):t.encode_example(tokenizer,req,target,max_length=10)
        with self.assertRaises(Exception):t.encode_example(tokenizer,req,{'S0':'direct'})

    def test_disjoint_real_200_300_and_question_level_split(self):
        from script.train_q4_retention import class_balance
        root=Path(__file__).resolve().parents[1]/'workdirs'
        train=json.loads((root/'Q4-adaptive-500-20261105-r1/new_case_ids.json').read_text())
        test=json.loads((root/'Q4-adaptive-300-20261105-r1/cohort.json').read_text())
        self.assertEqual(len(set(train)),200);self.assertEqual(len(set(test)),300)
        self.assertFalse(set(train)&set(test))
        counts,weights=class_balance([{'target':{'S0':'direct','S1':'direct','S2':'irrelevant'}}])
        self.assertEqual(counts['direct'],2);self.assertGreater(weights['irrelevant'],weights['direct'])

    def test_select_epochs_then_refit_from_fresh_adapter_without_test_rows(self):
        """Execute the complete optimizer/selection/refit/checkpoint lifecycle on CPU."""
        from contextlib import contextmanager
        from script import train_q4_retention as runner
        class Causal(Tiny):
            def __init__(self):
                super().__init__();self.embed=nn.Embedding(8,4);self.head=nn.Linear(4,8)
                self.device=torch.device('cpu');self.config=types.SimpleNamespace(use_cache=True)
            def forward(self,input_ids,attention_mask=None,use_cache=False,logits_to_keep=0):
                x=super().forward(self.embed(input_ids))
                return types.SimpleNamespace(logits=self.head(x[:,logits_to_keep,:]))
        @contextmanager
        def cpu_lease(*a,**kw):yield {'index':'cpu'}
        cfg=dict(seed=42,rank=2,alpha=4,dropout=0,learning_rate=.01,weight_decay=.01,betas=[.9,.999],
            gradient_clip=1.,accumulation=2,max_epochs=3,warmup_fraction=.05,max_sequence_length=2048)
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);training=['a','b','c','d'];validation=['v','w'];test=['never-read']
            runner.write(out/'frozen_hashes.json',{})
            runner.write(out/'training_config.json',cfg)
            runner.write(out/'split.json',dict(training=training,validation=validation,refit=training+validation,evaluation=test))
            runner.write(out/'train_ids.json',training+validation)
            runner.write(out/'teacher_claims.json',{u:{'state':'complete'} for u in training+validation})
            runner.write(out/'base_qa_config.json',{'models':{'vlm':'tiny'}})
            for u in training+validation:
                runner.write(out/'teacher_runs'/u/'samples.json',[dict(question_id=u,request={'id':u},target={'S0':'direct'})])
            seen=[]
            def encode(tok,request,target,limit):
                seen.append(request['id'])
                return dict(input_ids=[1,2,3],positions=[1],class_weights=[('direct',1.)],target=target,input_tokens=3,supervised_tokens=1)
            model=Causal()
            with patch.object(runner,'runtime'),patch.object(runner,'lease',cpu_lease),\
                 patch.object(runner,'training_model',return_value=(model,None)),\
                 patch.object(runner,'configure_gradients',side_effect=lambda m,s:t.attach(m,2,4,0)),\
                 patch.object(t,'encode_example',side_effect=encode),\
                 patch('torch.cuda.synchronize'),patch('torch.cuda.max_memory_allocated',return_value=0):
                runner.train(out)
            result=runner.read(out/'training/result.json');selection=runner.read(out/'training/selection.json')
            self.assertEqual(set(seen),set(training+validation));self.assertNotIn(test[0],seen)
            self.assertIn(result['selected_epoch'],[1,2,3]);self.assertEqual(selection['test_questions_used'],0)
            history=runner.read(out/'training/history.json')
            self.assertEqual(len([h for h in history if h['phase']=='selection']),3)
            self.assertEqual(len([h for h in history if h['phase']=='refit']),result['selected_epoch'])
            self.assertEqual(result['adapter']['test_questions_used'],0)
            self.assertTrue((out/'adapter/adapter.safetensors').exists())


if __name__=='__main__':unittest.main()
