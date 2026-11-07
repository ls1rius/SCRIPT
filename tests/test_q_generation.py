import json
from types import SimpleNamespace
import unittest
import torch
from transformers import AutoTokenizer
from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data

from script_core import q_evidence as q
from script_core import o_batch_generation as generation
from script.run_q_experiment import packet


class QGenerationTests(unittest.TestCase):
    def test_real_tokenizer_batch_schema_eos_padding_and_context_isolation(self):
        tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        sources=[dict(id=f'S{i}',text=f'#J J moves box {i}.',source_intervals=[[i,i+1]]) for i in range(4)]
        requests=[q.request_for(dict(question='What is being moved?'),sources,i) for i in range(4)]
        expected=[dict(decision=d) for d in ['keep','uncertain','drop','keep']]
        case=self
        class Model:
            device='cpu';config=SimpleNamespace(max_position_embeddings=32768)
            generation_config=SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            def generate(self,input_ids,attention_mask,prefix_allowed_tokens_fn,**kwargs):
                outputs=[]
                case.assertEqual(kwargs['max_new_tokens'],64)
                for i in range(4):
                    messages=generation.messages_for(q.FILTER,requests[i])[0]
                    own=tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=True)
                    case.assertEqual(input_ids[i][attention_mask[i].bool()].tolist(),own)
                    state=input_ids[i].clone()
                    for char in json.dumps(expected[i],separators=(',',':')):
                        for token in tokenizer.encode(char,add_special_tokens=False):
                            case.assertIn(token,prefix_allowed_tokens_fn(i,state))
                            state=torch.cat([state,torch.tensor([token])])
                    case.assertIn(tokenizer.eos_token_id,prefix_allowed_tokens_fn(i,state))
                    state=torch.cat([state,torch.tensor([tokenizer.eos_token_id])])
                    case.assertEqual(prefix_allowed_tokens_fn(i,state),[kwargs['pad_token_id']])
                    outputs.append(state)
                width=max(map(len,outputs))
                return torch.stack([torch.cat([row,torch.full((width-len(row),),kwargs['pad_token_id'])]) for row in outputs])
        backend=SimpleNamespace(prepare_vlm=lambda:None,load=lambda name:(Model(),SimpleNamespace(tokenizer=tokenizer)),
            cfg=dict(models=dict(max_input_tokens=32768)),enforcer_data=build_token_enforcer_tokenizer_data(tokenizer),
            model_tokens={},deadline=None,check_deadline=lambda:None)
        result=generation.generate_batch(backend,packet('one-question',requests))
        self.assertEqual([r['data'] for r in result['items']],expected)
        self.assertTrue(all(r['ended_normally'] and not r['parse_error'] for r in result['items']))


if __name__=='__main__':unittest.main()
