import json
from types import SimpleNamespace
import unittest
import torch
from transformers import AutoTokenizer
from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data

from script_core import o_batch_generation as g
from script_core import q_v4 as r
from script.q_repair_validation import source


class QV4GenerationTests(unittest.TestCase):
    def test_joint_option_letters_empty_pool_and_independent_decoders(self):
        tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        scope=dict(question_id='synthetic',condition='evidence_audit_with_options')
        from script.q_v4_validation import probe_requests
        from tests.test_q_v4 import pair
        import copy
        request=dict(probe_requests()[0][1],candidate_index=2,alternative_index=0)
        requests=[request,copy.deepcopy(request)]
        requests[1]['observations']=[]
        expected=[pair(requests[0],True),pair(requests[1],False)]
        packet=dict(scope=scope,operation='q4_pair',items=[dict(source_id=f'W{i}',scope=scope,request=req) for i,req in enumerate(requests)])
        case=self
        class Model:
            device='cpu';config=SimpleNamespace(max_position_embeddings=128000)
            generation_config=SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            def generate(self,input_ids,attention_mask,prefix_allowed_tokens_fn,**kwargs):
                results=[]
                for i in range(2):
                    messages=g.messages_for('q4_pair',requests[i])[0]
                    wire=json.loads(messages[1]['content'])
                    case.assertEqual(wire['option_A'],requests[i]['options'][2]);case.assertNotIn('option_index',wire)
                    encoded=tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=True)
                    case.assertEqual(input_ids[i][attention_mask[i].bool()].tolist(),encoded)
                    state=input_ids[i].clone()
                    for char in json.dumps(expected[i],separators=(',',':')):
                        for token in tokenizer.encode(char,add_special_tokens=False):
                            case.assertIn(token,prefix_allowed_tokens_fn(i,state))
                            state=torch.cat([state,torch.tensor([token])])
                    case.assertIn(tokenizer.eos_token_id,prefix_allowed_tokens_fn(i,state))
                    state=torch.cat([state,torch.tensor([tokenizer.eos_token_id])]);results.append(state)
                width=max(map(len,results))
                return torch.stack([torch.cat([v,torch.full((width-len(v),),kwargs['pad_token_id'])]) for v in results])
        backend=SimpleNamespace(prepare_vlm=lambda:None,load=lambda name:(Model(),SimpleNamespace(tokenizer=tokenizer)),
            cfg=dict(models=dict(max_input_tokens=32768)),enforcer_data=build_token_enforcer_tokenizer_data(tokenizer),
            model_tokens={},deadline=None,check_deadline=lambda:None)
        output=g.generate_batch(backend,packet)
        self.assertEqual([x['data'] for x in output['items']],expected)
        self.assertTrue(all(x['ended_normally'] and not x['parse_error'] for x in output['items']))


if __name__=='__main__':unittest.main()
