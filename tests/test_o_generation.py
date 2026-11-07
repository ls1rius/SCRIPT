from pathlib import Path
import json
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import torch
from transformers import AutoTokenizer
from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
from script_core.backend import LocalModels
from script_core.config import load_config
from script_core import o_generation


class OGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        cls.data=build_token_enforcer_tokenizer_data(cls.tokenizer)

    def test_real_tokenizer_all_zero_empty_composition_and_no_hidden_retry(self):
        test=self;tokenizer=self.tokenizer;calls=[]
        class Network:
            device='cpu';config=SimpleNamespace(max_position_embeddings=32768)
            generation_config=SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            raw='{"observation":"a wall","relevance":"unrelated","labels":{"U0":0,"U1":0}}';ended=True
            def generate(self,input_ids,**kwargs):
                calls.append(kwargs);state=input_ids
                ids=tokenizer.encode(self.raw,add_special_tokens=False)
                if self.ended:ids.append(tokenizer.eos_token_id)
                for token in ids:
                    test.assertIn(token,kwargs['prefix_allowed_tokens_fn'](0,state[0]))
                    state=torch.cat([state,torch.tensor([[token]])],dim=1)
                return state
        model=Network();cfg=load_config('configs/script_N.yaml');cfg['models']['device']='cpu'
        backend=LocalModels(cfg);backend.enforcer_data=self.data
        request=dict(question='Which?',options=['A','B'],sentence='C moves.',candidates=[dict(id='U0'),dict(id='U1')])
        with patch.object(backend,'prepare_vlm'),patch.object(backend,'load',return_value=(model,SimpleNamespace(tokenizer=tokenizer))):
            result=backend.call('o_filter',request)
            self.assertEqual(result['data']['labels'],{'U0':0,'U1':0});self.assertTrue(result['ended_normally'])
            plan=dict(option_tests=dict(O0=dict(single_observation='opens drawer',aggregate_requirement='repeated',interpretation=''),
                O1=dict(single_observation='reads book',aggregate_requirement='',interpretation='studies')))
            model.raw=json.dumps(plan,separators=(',',':'))
            result=backend.call('o_filter_plan',dict(question='Which?',options=['Repeated opening','Reading to study']))
            self.assertEqual(result['data'],plan)
            question_plan=dict(option_tests=dict(Q=dict(single_observation='repair actions',aggregate_requirement='repeated',interpretation='')))
            model.raw=json.dumps(question_plan,separators=(',',':'))
            result=backend.call('o_filter_plan',dict(question='Which action repeats during the repair?'))
            self.assertEqual(result['data'],question_plan)
            model.raw='{"groups":{"actions_0":{"text":""}}}'
            result=backend.call('oplus_compose',dict(question='Who is acting?',groups={'actions_0':[dict(id='U0',kind='action_field',group='actions_0',field='actor',value='someone')]}))
            self.assertEqual(result['data'],{'groups':{'actions_0':{'text':''}}})
            from script_core import o_sentence_filter as core
            for actor in ['繫','张三','D"E','𠮷']:
                req=core.compose_input(dict(question='Who is identified?'),[
                    dict(id='U0',kind='action_field',group='actions_0',field='actor',value=actor)])
                _,_,_,aliases=o_generation.decoder_contract(core.COMPOSE,req)
                model.raw=json.dumps(dict(groups=dict(actions_0=dict(text=next(iter(aliases['actions_0']))))),separators=(',',':'))
                result=backend.call(core.COMPOSE,req)
                self.assertIsNone(result['parse_error'])
                self.assertEqual(result['data']['groups']['actions_0']['text'],'Actor: '+actor+'.')
                self.assertEqual(result['identity_aliases'],aliases)
                self.assertNotEqual(result['raw'],json.dumps(result['data'],separators=(',',':')))
            report=dict(extraction_status='extracted',actions=[dict(actor='C',verb='puts',roles=dict(patient='cup',destination='drawer'))],
                attributes=[],states=[],relations=[],changes=[],results=[],event_links=[])
            model.raw=json.dumps(report,separators=(',',':'))
            result=backend.call('o_extract',dict(sentence='C puts a cup into a drawer.'))
            self.assertEqual(result['data']['actions'][0]['roles'],{'patient':'cup','destination':'drawer'})
            self.assertEqual(result['data']['states'],[])
            model.ended=False;before=len(calls)
            result=backend.call('o_extract',dict(sentence='C puts a cup into a drawer.'))
            self.assertIsNotNone(result['parse_error']);self.assertEqual(len(calls),before+1)
            backend.cfg['models']['max_input_tokens']=1;before=len(calls)
            with self.assertRaisesRegex(ValueError,'overflow'):backend.call('o_filter',request)
            self.assertEqual(len(calls),before)

    def test_O_and_Oplus_readers_require_complete_ascii_answer(self):
        tokenizer=self.tokenizer;test=self;calls=[]
        class Network:
            device='cpu';ended=True
            generation_config=SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            def generate(self,input_ids,**kwargs):
                calls.append(kwargs);state=input_ids
                ids=tokenizer.encode('prediction: A',add_special_tokens=False)
                if self.ended:ids.append(tokenizer.eos_token_id)
                for token in ids:
                    test.assertTrue(token in kwargs['prefix_allowed_tokens_fn'](0,state[0]))
                    state=torch.cat([state,torch.tensor([[token]])],dim=1)
                return state
        network=Network();backend=LocalModels(load_config('configs/script_N.yaml'));backend.enforcer_data=self.data
        backend._m_model=SimpleNamespace(qa=SimpleNamespace(tokenizer=tokenizer,model=network))
        request=dict(inputs=dict(head='Answer.',prompts=['D opens a drawer. Who opens it? A. D B. E']),options=['D','E'])
        with patch.object(backend,'prepare_vlm'),patch.object(backend,'load',return_value=(network,SimpleNamespace(tokenizer=tokenizer))):
            for operation in ['o_answer','oplus_answer','p_answer']:
                self.assertEqual(backend.call(operation,request)['data']['selected_option'],'0')
                network.ended=False;before=len(calls)
                with self.assertRaisesRegex(ValueError,'terminate normally'):backend.call(operation,request)
                self.assertEqual(len(calls),before+1);network.ended=True


if __name__=='__main__':unittest.main()
