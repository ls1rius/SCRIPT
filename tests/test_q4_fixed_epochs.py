from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import torch
from torch import nn
from script import train_q4_fixed_epochs as fixed
from script import train_q4_retention as runner
from script_core import q4_retention_training as t


class Tiny(nn.Module):
    def __init__(self):
        super().__init__();a=nn.Module();a.q_proj=nn.Linear(4,4);a.v_proj=nn.Linear(4,4)
        layer=nn.Module();layer.self_attn=a;self.language_model=nn.Module();self.language_model.layers=nn.ModuleList([layer])
        self.embed=nn.Embedding(8,4);self.head=nn.Linear(4,8);self.device=torch.device('cpu')
    def forward(self,input_ids,attention_mask=None,use_cache=False,logits_to_keep=0):
        x=self.embed(input_ids);a=self.language_model.layers[0].self_attn;x=a.q_proj(x)+a.v_proj(x)
        return types.SimpleNamespace(logits=self.head(x[:,logits_to_keep,:]))


class FixedEpochTests(unittest.TestCase):
    def test_one_initialization_one_optimizer_six_complete_epochs_no_validation_or_refit(self):
        @contextmanager
        def lease(*a,**kw):yield {'index':'cpu'}
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);ids=['a','b','c','d'];settings=dict(seed=42,rank=2,alpha=4,dropout=0,learning_rate=.01,
                weight_decay=.01,betas=[.9,.999],gradient_clip=1.,accumulation=3,epochs=6,warmup_fraction=.05,max_sequence_length=2048)
            runner.write(out/'training_config.json',settings);runner.write(out/'train_ids.json',ids)
            runner.write(out/'test_ids.json',['scoring_only']);runner.write(out/'teacher_claims.json',{u:{'state':'complete'} for u in ids})
            runner.write(out/'base_qa_config.json',{'models':{'vlm':'tiny'}})
            for u in ids:runner.write(out/'teacher_runs'/u/'samples.json',[dict(question_id=u,request={'id':u},target={'S0':'direct'})])
            def encode(tok,request,target,limit):
                return dict(input_ids=[1,2,3],positions=[1],class_weights=[('direct',1.)],target=target,input_tokens=3,supervised_tokens=1)
            model=Tiny();seen=[];loss=t.loss_for;optimizer=torch.optim.AdamW
            def counted_loss(model,sample,weights):seen.append(sample['question_id']);return loss(model,sample,weights)
            with patch.object(runner,'verify'),patch.object(runner,'lease',lease),\
                 patch.object(runner,'training_model',return_value=(model,None)) as load,\
                 patch.object(runner,'configure_gradients',side_effect=lambda m,s:t.attach(m,2,4,0)) as attach,\
                 patch.object(t,'encode_example',side_effect=encode),patch.object(t,'loss_for',side_effect=counted_loss),\
                 patch('torch.optim.AdamW',wraps=optimizer) as opt,patch('torch.cuda.synchronize'),patch('torch.cuda.max_memory_allocated',return_value=0):
                fixed.train(out)
            self.assertEqual(load.call_count,1);self.assertEqual(attach.call_count,1);self.assertEqual(opt.call_count,1)
            self.assertEqual(Counter(seen),Counter({u:6 for u in ids}))
            history=runner.read(out/'training/history.json');self.assertEqual([h['epoch'] for h in history],list(range(1,7)))
            self.assertTrue(all('validation_loss' not in h for h in history))
            self.assertEqual([h['optimizer_steps'] for h in history],[2,4,6,8,10,12])
            result=runner.read(out/'training/result.json');self.assertTrue(result['complete']);self.assertFalse(result['refit'])
            self.assertEqual(result['final_epoch'],6);self.assertEqual(result['microsteps'],24)
            self.assertEqual(result['validation_forward_input_tokens'],0)
            self.assertTrue(runner.read(out/'training/first_gradient_check.json')['passed'])
            self.assertEqual(runner.read(out/'adapter/adapter_config.json')['epoch'],6)
            self.assertFalse((out/'training/selection.json').exists())
            self.assertFalse(any(p.name.startswith(('refit','selection')) for p in (out/'training').iterdir()))

    def test_train_validation_overlap_is_rejected_before_loading(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);runner.write(out/'training_config.json',{'epochs':6});runner.write(out/'train_ids.json',['same'])
            runner.write(out/'test_ids.json',['same'])
            with patch.object(runner,'verify'),patch.object(runner,'training_model') as load:
                with self.assertRaisesRegex(ValueError,'overlap'):fixed.train(out)
            load.assert_not_called()


if __name__=='__main__':unittest.main()
