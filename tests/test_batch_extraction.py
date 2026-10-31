from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from script_core import batch_extraction as batch
from script_core import backend, context, schemas, lazy_script_memory as memory
from script import run_extraction_batch_benchmark as run


def request(text='C cuts the paper with scissors.', uid='U0'):
    return dict(unit=dict(id=uid, text=text), requested_fields=['actions.base', 'actions.instrument'], source_type='caption')


def runtime():
    return SimpleNamespace(backend=backend, context=context, schemas=schemas, memory=memory,
        prompts=SimpleNamespace(SYSTEM='system'))


class Processor:
    def apply_chat_template(self, messages, **kwargs):
        self.messages = deepcopy(messages)
        return json.dumps(messages, ensure_ascii=False)


class BatchExtractionTests(unittest.TestCase):
    def test_native_prompt_matches_frozen_serial_rendering(self):
        inputs=request(uid='source-17');processor=Processor()
        class Captured(Exception):pass
        class CapturingProcessor(Processor):
            def apply_chat_template(self, messages, **kwargs):
                super().apply_chat_template(messages, **kwargs)
                raise Captured()
        original_processor=CapturingProcessor()
        models=backend.LocalModels(dict(models=dict(max_input_tokens=10000)))
        models.load=lambda _: (None, original_processor)
        with patch.dict(backend.CONTRACTS, {memory.EXTRACT:memory.EXTRACT_PROMPT}), \
                patch.object(schemas, 'output_schema', side_effect=memory.output_schema):
            with self.assertRaises(Captured):models.vlm(memory.EXTRACT, inputs)
        rt=runtime();rt.prompts=SimpleNamespace(SYSTEM=backend.SYSTEM)
        rendered=batch.render_request(inputs,rt,processor)
        self.assertEqual(rendered['messages'],original_processor.messages)
        self.assertEqual(rendered['inverse'],{'ID0':'source-17'})
        self.assertIn('"id": "ID0"',rendered['messages'][1]['content'][0]['text'])

    def test_each_source_has_its_own_schema_and_task_fields_are_rejected(self):
        left=batch.render_request(request('C cuts red paper.'),runtime(),Processor())
        right=batch.render_request(request('C holds blue cloth.'),runtime(),Processor())
        self.assertIn('red',json.dumps(left['schema']))
        self.assertNotIn('blue',json.dumps(left['schema']))
        self.assertNotEqual(left['schema'],right['schema'])
        with self.assertRaisesRegex(ValueError,'source-only'):
            batch.render_request(dict(request(),question='Guess the answer'),runtime(),Processor())

    def test_decoder_routes_independently_and_only_checks_generated_eos(self):
        import torch
        seen=[]
        def first(i,t):seen.append('first');return [10]
        def second(i,t):seen.append('second');return [20]
        route=batch.IndependentPrefixes([first,second],3,{2},0)
        # EOS in the prompt must not finish the generated sequence.
        self.assertEqual(route(1,torch.tensor([2,8,9,40])),[20])
        self.assertEqual(route(0,torch.tensor([2,8,9,40])),[10])
        self.assertEqual(route(0,torch.tensor([2,8,9,40,2])),[0])
        self.assertEqual(seen,['second','first'])
        with self.assertRaises(ValueError):route(2,torch.tensor([1,2,3]))
        self.assertEqual(batch.trim_generated([8,9,2,0,0],{2}),[8,9,2])

    def test_foreign_fact_is_rejected_and_its_source_is_preserved(self):
        data={c:[] for c in memory.joint.COLLECTIONS.values()};data['uncertain']=False
        data['actions']=[dict(actor='C',verb='cuts',roles=[dict(role='instrument',value='hammer')])]
        result=batch.inspect_output(request(),dict(data=data),runtime())
        self.assertIsNotNone(result['error'])
        self.assertEqual(result['retained_original'],request()['unit']['text'])

    def test_valid_empty_extraction_does_not_erase_its_source(self):
        data={c:[] for c in memory.joint.COLLECTIONS.values()};data['uncertain']=False
        result=batch.inspect_output(request(),dict(data=data),runtime())
        self.assertIsNone(result['error']);self.assertFalse(result['complete_simple'])
        self.assertEqual(result['retained_original'],request()['unit']['text'])

    def test_batch_time_is_charged_once_and_only_matched_complete_work_gets_speedup(self):
        requests=[dict(id=f'R{i}') for i in range(4)]
        def item(i):return dict(request_id=f'R{i}',inspection=dict(error=None,data={'x':i},
            accepted={'facts':[],'rejected':[]},retained_original='source'))
        calls=[dict(variant='serial',seconds=1,items=[item(i)],error=None) for i in range(4)]
        calls += [dict(variant='batch2',seconds=1.25,items=[item(i),item(i+1)],error=None) for i in [0,2]]
        result=batch.benchmark_report(requests,calls,['serial','batch2'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['variants']['batch2']['seconds'],2.5)
        self.assertEqual(result['variants']['batch2']['speedup_vs_serial'],1.6)
        self.assertEqual(result['variants']['batch2']['exact_serial_matches'],4)
        result=batch.benchmark_report(requests,calls[:-1],['serial','batch2'])
        self.assertFalse(result['complete'])
        self.assertIsNone(result['variants']['batch2']['speedup_vs_serial'])
        with self.assertRaises(ValueError):batch.benchmark_report(requests,calls+[calls[-1]],['serial','batch2'])

    def test_protected_and_busy_cards_are_excluded_even_with_free_memory(self):
        idle=dict(index='1',uuid='gpu-free',compute_pids=[],utilization=0,used_mib=0,free_mib=90000)
        self.assertTrue(batch.eligible_gpu(idle,['gpu-E','gpu-F'],['1','2']))
        for changed in [dict(uuid='gpu-E'),dict(compute_pids=['42']),dict(utilization=95),
                        dict(used_mib=700),dict(free_mib=1000),dict(index='3')]:
            self.assertFalse(batch.eligible_gpu(dict(idle,**changed),['gpu-E','gpu-F'],['1','2']))

    def test_queue_waits_without_starting_a_model_when_no_card_is_idle(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)
            run.write(out/'frozen_hashes.json',{})
            run.write(out/'experiment.json',dict(protected_gpu_uuids=['gpu-F'],allowed_gpu_indices=['1','2'],
                minimum_free_mib=32768,stable_observations=3,poll_seconds=10))
            row=dict(index='1',uuid='gpu-busy',compute_pids=['99'],utilization=98,used_mib=50000,free_mib=45000)
            with patch.object(run,'inventory',return_value={row['uuid']:row}), \
                    patch.object(run.time,'sleep',side_effect=InterruptedError),patch.object(run.subprocess,'Popen') as launch:
                with self.assertRaises(InterruptedError):run.queue(out)
            launch.assert_not_called()
            self.assertEqual(run.read(out/'queue_status.json')['state'],'waiting_for_idle_gpu')

    def test_preparation_copies_completed_calls_without_writing_reference_or_reading_answers(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);reference=root/'F';out=root/'batch';e=root/'E'
            values={'cohort.json':['q0'],'experiment.json':{'E_run':str(e)},
                'launch.json':{'gpu_uuid':'gpu-F'},'qa_config.json':{},'qa_expected_artifacts.json':{},
                'runs/F_lazy_script/q0/result.json':{'answer':'must not be read'},
                'evaluation_only.json':{'secret':'must not be read'}}
            for name,value in values.items():run.write(reference/name,value)
            run.write(e/'launch.json',{'gpu_uuid':'gpu-E'})
            d=reference/'runtime/qa/script_core';d.mkdir(parents=True);(d/'__init__.py').write_text('')
            inputs=request();raw=reference/'materialized/q0/raw_calls/call.json'
            run.write(raw,dict(inputs=inputs,output=dict(data={}),error=None))
            calls=reference/'materialized/q0/calls.jsonl'
            calls.write_text(json.dumps(dict(operation='f_extract_fields',input_hash=batch.digest(inputs),output_ref=str(raw),error=None))+'\n')
            before={str(p):run.sha(p) for p in reference.rglob('*') if p.is_file()}
            original_read=run.read
            def guarded_read(path):
                self.assertNotIn(Path(path).name,['result.json','evaluation_only.json'])
                return original_read(path)
            with patch.object(run,'read',side_effect=guarded_read):run.prepare(reference,out,1,4)
            self.assertEqual(before,{str(p):run.sha(p) for p in reference.rglob('*') if p.is_file()})
            self.assertEqual(run.read(out/'experiment.json')['protected_gpu_uuids'],['gpu-E','gpu-F'])
            self.assertEqual(len(run.read(out/'requests.json')),1)
            run.verify(out)
            (out/'requests.json').write_text('[]')
            with self.assertRaisesRegex(ValueError,'Frozen benchmark'):run.verify(out)

    def test_stochastic_generation_is_not_silently_used_for_comparison(self):
        with self.assertRaises(ValueError):
            batch.BatchExtractor(dict(models=dict(generation=dict(do_sample=True))),runtime(),models=object())


class RealCPUBatchDecoderTest(unittest.TestCase):
    def test_real_transformers_generation_uses_distinct_lmfe_schemas_with_padding(self):
        """Tiny random CPU model tests decoding mechanics, not extraction accuracy."""
        import torch
        from transformers import GPT2Config,GPT2LMHeadModel,BatchEncoding
        from lmformatenforcer import TokenEnforcerTokenizerData
        old_threads=torch.get_num_threads();torch.set_num_threads(1)
        self.addCleanup(torch.set_num_threads,old_threads)
        model=GPT2LMHeadModel(GPT2Config(vocab_size=128,n_positions=2048,n_ctx=2048,
            n_embd=16,n_layer=1,n_head=2,bos_token_id=1,eos_token_id=2,pad_token_id=0)).eval()
        class Tokenizer:
            padding_side='right';pad_token_id=0;eos_token_id=2
            def decode(self,ids,**kwargs):return ''.join(chr(i) for i in ids if 32<=i<127)
        class TinyProcessor(Processor):
            tokenizer=Tokenizer()
            def __call__(self,text,**kwargs):
                self.seen_padding=self.tokenizer.padding_side
                lengths=[3+len(t)%4 for t in text];width=max(lengths)
                return BatchEncoding(dict(input_ids=torch.tensor([[0]*(width-n)+[80]*n for n in lengths]),
                    attention_mask=torch.tensor([[0]*(width-n)+[1]*n for n in lengths])))
        processor=TinyProcessor()
        data=TokenEnforcerTokenizerData([(i,chr(i),False) for i in range(32,127)],
            lambda ids:''.join(chr(i) for i in ids if 32<=i<127),2,False,128)
        models=SimpleNamespace(load=lambda _: (model,processor),prepare_vlm=lambda:None,
            enforcer_data=data,deadline=None,check_deadline=lambda:None)
        rt=runtime()
        rt.memory=SimpleNamespace(EXTRACT='f_extract_fields',EXTRACT_PROMPT='Extract value only',
            output_schema=lambda op,inp:dict(type='object',properties=dict(value=dict(type='string',enum=[inp['unit']['text']])),
                required=['value'],additionalProperties=False))
        cfg=dict(models=dict(generation=dict(do_sample=False,max_new_tokens=1024),max_input_tokens=32))
        extractor=batch.BatchExtractor(cfg,rt,models=models)
        outputs=extractor.generate([request('red'),request('blue cloth'),request('green'),request('black rod')])
        self.assertEqual([o['data']['value'] for o in outputs],['red','blue cloth','green','black rod'])
        self.assertEqual(processor.seen_padding,'left')
        self.assertEqual(processor.tokenizer.padding_side,'right')


if __name__=='__main__':unittest.main()
