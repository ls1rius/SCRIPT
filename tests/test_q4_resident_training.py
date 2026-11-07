import copy
from contextlib import contextmanager
import inspect
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import torch
from torch import nn
from script import resident_train_q4 as r
from script import train_q4_retention as trainer
from script import run_q_v4_experiment as q
from script_core import budget,q4_retention_training as adapters

OUT=Path(__file__).resolve().parents[1]/'workdirs/Q4-trained-200train-300eval-20261105-r1'


class Tiny(nn.Module):
    def __init__(self):
        super().__init__();a=nn.Module();a.q_proj=nn.Linear(4,4);a.v_proj=nn.Linear(4,4)
        layer=nn.Module();layer.self_attn=a
        self.language_model=nn.Module();self.language_model.layers=nn.ModuleList([layer])
        self.config=SimpleNamespace(use_cache=True);self.restored=[]
    def forward(self,x):
        a=self.language_model.layers[0].self_attn
        return a.q_proj(x)+a.v_proj(x)
    def gradient_checkpointing_disable(self):self.restored.append('checkpointing')
    def disable_input_require_grads(self):self.restored.append('input_grads')


class ResidentTrainingTests(unittest.TestCase):
    def test_configuration_switch_preserves_every_frozen_semantic_value(self):
        a=r.read(OUT/'teacher_config.json');b=r.read(OUT/'base_qa_config.json');b['models']['retention_adapter']=str(OUT.resolve()/'adapter')
        r.compatible(a,b,OUT.resolve())
        for section,key,value in [('models','vlm','wrong'),('budget','tool_call_cap',999),('query','answer_order','wrong')]:
            c=copy.deepcopy(b);c[section][key]=value
            with self.assertRaises(ValueError):r.compatible(a,c,OUT.resolve())
        b['models']['retention_adapter']='wrong'
        with self.assertRaises(ValueError):r.compatible(a,b,OUT.resolve())

    def test_teacher_and_evaluation_use_original_calls_and_retain_backend(self):
        a=inspect.getsource(trainer.annotate);b=r.annotate_code(a)
        for line in a.splitlines():
            if any(s in line for s in ['meter.call(', 'teacher_claim(', 'samples.append(', 'ledger=', 'validate_verdict(']):
                self.assertIn(line.strip(),b)
        self.assertNotIn('backend.close(',b);self.assertIn('backend=SHARED_BACKEND',b)
        a=inspect.getsource(q.worker);b=r.evaluation_code(a)
        for line in a.splitlines():
            if any(s in line for s in ['run_case(', 'base.publish(', 'validate_live(', 'base.claim(', 'source_binding.json']):
                self.assertIn(line.strip(),b)
        self.assertNotIn('backend.close(',b);self.assertIn('backend=SHARED_BACKEND',b)

    def test_evaluation_four_gpu_policy_is_frozen_before_manifest(self):
        code=r.prepare_evaluation_code(inspect.getsource(trainer.prepare_evaluation))
        self.assertLess(code.index("policy['resource_policy']['preference']=['0','1','2','3']"),code.index("write(target/'frozen_hashes.json'"))
        compile(code,'prepare','exec')

    def test_generated_evaluation_executes_with_borrowed_gpu_dictionary(self):
        # Exercise the actual generated worker boundary: borrowed leases yield a
        # dictionary whereas a newly acquired shared lease yields (gpu, locks).
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)
            r.write(out/'qa_config.json',{})
            r.write(out/'experiment.json',{'resource_policy':{}})
            events=[];gpu=dict(index='0',uuid='GPU',reservation_id='held')
            @contextmanager
            def borrowed(policy,on_wait):
                events.append('enter');yield gpu;events.append('exit')
            backend=SimpleNamespace(call=lambda *a:events.append('prepare') or {})
            scope=dict(vars(q),verify_manifest=lambda p:None,SHARED_BACKEND=backend,borrowed_evaluation_lease=borrowed)
            code=r.evaluation_code(inspect.getsource(q.worker))
            exec(code,scope)
            with patch('script.run_evidence_planner_experiment.verify_model'),patch('script.q_v4_validation.validate_live',side_effect=lambda *a:events.append('validate')),\
                 patch.object(q.base,'claim',return_value=None),patch.dict(os.environ,{},clear=False):
                scope['worker'](out,4)
            self.assertEqual(events,['enter','prepare','validate','exit'])
            self.assertEqual(r.read(out/'workers/4/status.json')['stage'],'complete')

    def test_label_dependency_is_all200_not_any_finished_worker(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);r.write(p/'train_ids.json',['a','b']);r.write(p/'teacher_claims.json',{'a':{'state':'complete'},'b':{'state':'running'}})
            self.assertFalse(r.complete_labels(p))
            r.write(p/'teacher_claims.json',{u:{'state':'complete'} for u in ['a','b']})
            self.assertTrue(r.complete_labels(p))

    def test_training_reuses_loaded_base_and_restores_inference_without_releasing(self):
        model=Tiny();x=torch.randn(2,4);baseline=model(x).detach().clone()
        backend=SimpleNamespace(loaded={'vlm':(model,SimpleNamespace(tokenizer='tokenizer'))},load=lambda k:backend.loaded[k])
        gpu={'reservation_id':'held','uuid':'fake'};events=[]
        @contextmanager
        def borrow(gpu,minimum=0):events.append(('borrow',minimum));yield gpu;events.append(('return',minimum))
        def fit(out):
            with trainer.lease(out,out/'training',minimum=43008,allocator=40960):
                loaded,tok=trainer.training_model(out)
                self.assertIs(loaded,model);self.assertEqual(tok,'tokenizer')
                adapters.attach(model,2,4,0)
                with torch.no_grad():
                    for n,p in model.named_parameters():
                        if n.endswith('lora_B'):p.fill_(.1)
                model.config.use_cache=False
                adapters.save_adapter(model,out/'adapter',{'base_model':'tiny'})
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);r.write(p/'train_ids.json',['a']);r.write(p/'teacher_claims.json',{'a':{'state':'complete'}})
            original_loader=trainer.training_model;original_lease=trainer.lease
            with patch.object(r,'frozen',return_value=trainer),patch.object(r,'borrowed',borrow),patch.object(trainer,'train',side_effect=fit),\
                patch('torch.cuda.set_per_process_memory_fraction'),patch('torch.cuda.get_device_properties',return_value=SimpleNamespace(total_memory=100*1024**3)):
                result=r.train_loaded(backend,dict(out=str(p),gpu=gpu))
            self.assertIs(trainer.training_model,original_loader);self.assertIs(trainer.lease,original_lease)
            self.assertEqual(result['model_identity'],id(model));self.assertTrue(result['base_model_retained'])
            self.assertEqual(result['pid'],os.getpid());self.assertEqual(events,[('borrow',43008),('return',43008)])
            self.assertEqual(model.restored,['checkpointing','input_grads']);self.assertTrue(model.config.use_cache)
            self.assertFalse(model.training);self.assertTrue(all(not p.requires_grad for p in model.parameters()))
            self.assertTrue(torch.equal(model(x),baseline))
            with adapters.enabled(model,True):self.assertFalse(torch.equal(model(x),baseline))

    def test_training_restores_original_entrypoints_on_failure(self):
        model=Tiny();backend=SimpleNamespace(load=lambda k:(model,SimpleNamespace(tokenizer=None)))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);r.write(p/'train_ids.json',[]);r.write(p/'teacher_claims.json',{})
            loader,lease=trainer.training_model,trainer.lease
            with patch.object(r,'frozen',return_value=trainer),patch.object(trainer,'train',side_effect=RuntimeError('failure')):
                with self.assertRaises(RuntimeError):r.train_loaded(backend,dict(out=str(p),gpu={}))
            self.assertIs(trainer.training_model,loader);self.assertIs(trainer.lease,lease)

    def test_inference_config_keeps_same_model_and_original_allocator(self):
        cfg=r.read(OUT/'teacher_config.json');new=r.read(OUT/'base_qa_config.json');new['models']['retention_adapter']=str(OUT.resolve()/'adapter')
        model=object();backend=SimpleNamespace(cfg=cfg,loaded={'vlm':(model,None)})
        with patch('torch.cuda.empty_cache'),patch('torch.cuda.reset_peak_memory_stats'),patch('torch.cuda.get_device_properties',return_value=SimpleNamespace(total_memory=100*1024**3)),patch('torch.cuda.set_per_process_memory_fraction') as set_cap:
            changed,result=r.set_evaluation_config(backend,cfg,dict(out=str(OUT.resolve()),config=new))
        self.assertIs(backend.loaded['vlm'][0],model);self.assertEqual(changed,new)
        self.assertEqual(result['model_identity'],id(model));set_cap.assert_called_once_with(22528/102400)

    def test_private_process_controls_do_not_change_regular_model_call_path(self):
        original=inspect.getsource(budget._worker);changed=r.process_code(original)
        end=changed.index('        backend.deadline = deadline')
        start=original.index('        backend.deadline = deadline')
        self.assertEqual(changed[end:],original[start:]);compile(changed,'process','exec')

    def test_shared_lease_cannot_be_borrowed_by_unrelated_job_or_under_capacity(self):
        @contextmanager
        def registry():yield {'lease':{'pid':777,'start':'birth','uuid':'GPU','capacity_mib':24576}}
        with patch('script.gpu_shared_queue.locked_registry',registry),patch('script.gpu_shared_queue.process_snapshot',return_value={777:{'start':'birth','parent':1}}):
            with self.assertRaises(ValueError):
                with r.borrowed(dict(reservation_id='lease',uuid='GPU'),43008):pass
            with self.assertRaisesRegex(ValueError,'different process tree'):
                with r.borrowed(dict(reservation_id='lease',uuid='GPU'),24576):pass


if __name__=='__main__':unittest.main()
