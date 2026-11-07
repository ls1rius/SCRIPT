from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from script_core import p_sentence_filter as p
from script_core.o_batch_generation import generate_batch,messages_for
from script.o_batched_matrix import packet_for


def wire(actions=1):
    return dict(extraction_status='extracted',actions=[dict(subject='Observer A1',predicate='waits',arguments={k:None for k in p.ARGUMENT_ROLES}) for _ in range(actions)],attributes=[],states=[],relations=[],changes=[],results=[],event_links=[])


class RoleContractTests(unittest.TestCase):
    def test_required_unique_slots_and_canonical_projection(self):
        x=wire();x['actions'][0]['arguments'].update(tool_used='with a wrench',anatomical_part='with a hand')
        before=deepcopy(x);out=p.normalize_extraction(x)
        self.assertEqual(out['actions'][0]['roles'],dict(instrument='with a wrench',body_part='with a hand'))
        self.assertEqual(x,before)
        args=p.extraction_wire_schema()['properties']['actions']['items']['properties']['arguments']
        self.assertEqual(set(args['required']),set(p.ARGUMENT_ROLES));self.assertFalse(args['additionalProperties'])

    def test_null_omits_candidate_without_auto_repair(self):
        x=wire();self.assertEqual(p.normalize_extraction(x)['actions'][0]['roles'],{})
        x['actions'][0]['arguments']['tool_used']='with a hand'
        self.assertEqual(p.normalize_extraction(x)['actions'][0]['roles'],dict(instrument='with a hand'))

    def test_missing_unknown_empty_wrong_typed_roles_rejected(self):
        for name in ['missing','extra','empty','integer','array','object','old_array']:
            with self.subTest(name=name):
                x=wire();args=x['actions'][0]['arguments']
                if name=='missing':del args['tool_used']
                elif name=='extra':args['foreign']='x'
                elif name=='old_array':x['actions'][0]['arguments']=[dict(text='x',role='tool_used')]
                else:args['tool_used']={'empty':'','integer':3,'array':[],'object':{}}[name]
                with self.assertRaises(ValueError):p.normalize_extraction(x)

    def test_key_order_is_semantically_irrelevant(self):
        x=wire();x['actions'][0]['arguments']['object_acted_on']='a parcel'
        y=deepcopy(x);y['actions'][0]['arguments']=dict(reversed(list(y['actions'][0]['arguments'].items())))
        self.assertEqual(p.normalize_extraction(x),p.normalize_extraction(y))

    def test_duplicate_keys_never_overwrite(self):
        for raw in ['{"a":1,"a":2}','{"outer":{"tool_used":"x","tool_used":"y"}}','{"items":[{"x":null,"x":"y"}]}']:
            with self.subTest(raw=raw),self.assertRaisesRegex(ValueError,'Duplicate'):p.decode_extraction(raw)

    def test_status_and_event_count_boundaries(self):
        self.assertEqual(len(p.normalize_extraction(wire(4))['actions']),4)
        for x in [wire(5),wire(0),dict(wire(),extraction_status='unavailable')]:
            with self.assertRaises(ValueError):p.normalize_extraction(x)
        x=wire(0);x['extraction_status']='unavailable';self.assertEqual(p.normalize_extraction(x)['actions'],[])

    def test_empty_subject_predicate_and_overlong_values_fail(self):
        for field,value in [('subject',''),('predicate',''),('predicate','x'*161)]:
            x=wire();x['actions'][0][field]=value
            with self.assertRaises(ValueError):p.normalize_extraction(x)
        x=wire();x['actions'][0]['subject']=None;self.assertIsNone(p.normalize_extraction(x)['actions'][0]['actor'])

    def test_escaped_values_remain_literal_and_separate_events(self):
        x=wire(2);x['actions'][0]['arguments']['object_acted_on']='a "tag" \\ Ω';x['actions'][1]['arguments']['object_acted_on']='研究员'
        self.assertEqual(p.decode_extraction(json.dumps(x)),x)
        y=p.normalize_extraction(x);self.assertNotEqual(y['actions'][0]['roles'],y['actions'][1]['roles'])

    def test_real_tokenizer_four_events_and_truncation_rejection(self):
        import torch
        from transformers import AutoTokenizer
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        test=self;expected=wire(4);raw=json.dumps(expected,separators=(',',':'));tokens=tokenizer.encode(raw,add_special_tokens=False)
        class Model:
            device='cpu';config=SimpleNamespace(max_position_embeddings=128000);generation_config=SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            truncate=False
            def generate(self,input_ids,attention_mask,prefix_allowed_tokens_fn,**kw):
                test.assertEqual(kw['max_new_tokens'],8192);state=input_ids[0].clone()
                prefix_allowed_tokens_fn(0,state)
                for token in tokens:
                    test.assertIn(token,prefix_allowed_tokens_fn(0,state));state=torch.cat([state,torch.tensor([token])])
                test.assertIn(tokenizer.eos_token_id,prefix_allowed_tokens_fn(0,state))
                return torch.stack([state if self.truncate else torch.cat([state,torch.tensor([tokenizer.eos_token_id])])])
        model=Model();backend=SimpleNamespace(prepare_vlm=lambda:None,load=lambda name:(model,SimpleNamespace(tokenizer=tokenizer)),cfg=dict(models=dict(max_input_tokens=32768)),enforcer_data=build_token_enforcer_tokenizer_data(tokenizer),model_tokens={},deadline=None,check_deadline=lambda:None)
        packet=packet_for('q','source_only',p.EXTRACT,[('S0',dict(sentence='Observer A1 waits.'))])
        out=generate_batch(backend,packet)['items'][0];self.assertIsNone(out['parse_error']);self.assertEqual(out['wire_data'],expected)
        model.truncate=True;bad=generate_batch(backend,packet)['items'][0]
        self.assertIn('did not terminate',bad['parse_error']);self.assertIsNone(bad['data'])
        long=wire(4)
        for event in long['actions']:
            for key in event['arguments']:event['arguments'][key]='材料'*80
        p.normalize_extraction(long);count=len(tokenizer.encode(json.dumps(long,ensure_ascii=False,separators=(',',':')),add_special_tokens=False))
        self.assertLess(count,8192);self.assertGreater(count,2048)

if __name__=='__main__':unittest.main()
