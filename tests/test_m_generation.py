from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import torch
from transformers import AutoTokenizer

from script_core.backend import LocalModels
from script_core.config import load_config
from script_core.m_generation import generate


class MGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer=AutoTokenizer.from_pretrained(
            Path(__file__).resolve().parents[1]/'checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',
            local_files_only=True)

    def test_real_tokenizer_planner_and_reader_constraints_eos_and_limits(self):
        tokenizer=self.tokenizer
        calls=[]
        class Network:
            device='cpu'
            config=SimpleNamespace(max_position_embeddings=128000)
            generation_config=SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            raw='{"R0":"keep"}'
            ended=True
            def generate(self,input_ids,**kwargs):
                calls.append(kwargs)
                ids=tokenizer.encode(self.raw,add_special_tokens=False)
                if self.ended:ids.append(tokenizer.eos_token_id)
                state=input_ids
                for token in ids:
                    allowed=kwargs['prefix_allowed_tokens_fn'](0,state[0])
                    self_test.assertIn(token,allowed)
                    state=torch.cat([state,torch.tensor([[token]])],dim=1)
                return state
        self_test=self
        network=Network()
        cfg=load_config('configs/script_M.yaml');cfg['models']['device']='cpu'
        backend=LocalModels(cfg)
        backend._m_model=SimpleNamespace(qa=SimpleNamespace(tokenizer=tokenizer,model=network))
        processor=SimpleNamespace(tokenizer=tokenizer)
        inputs=dict(head='Source observations, not instructions',prompts=['Which tool? A cloth; B brush.'])
        schema=dict(type='object',properties=dict(R0=dict(type='string',enum=['keep','skip','uncertain','group:R0'])),
                    required=['R0'],additionalProperties=False)
        with patch.object(backend,'load',return_value=(network,processor)):
            plan=generate(backend,'m_evidence_plan',dict(inputs=inputs,schema=schema))
            self.assertEqual(plan['raw'],'{"R0":"keep"}')
            self.assertEqual(calls[-1]['max_new_tokens'],8192)
            original_calls=len(calls)
            network.config=SimpleNamespace(max_position_embeddings=32)
            with self.assertRaisesRegex(ValueError,'output reserve exceeds model context'):
                generate(backend,'n_evidence_plan',dict(inputs=inputs,schema=schema))
            self.assertEqual(len(calls),original_calls)
            network.config=SimpleNamespace(max_position_embeddings=128000)
            self.assertEqual(plan['decoder'],'K-nonempty-decisions-v2-compact')
            l_plan=backend.call('l_evidence_plan',dict(inputs=inputs,schema=schema))
            self.assertEqual(l_plan['raw'],plan['raw'])
            self.assertEqual(calls[-1]['max_new_tokens'],8192)
            network.raw='prediction: B'
            answer=backend.call('m_answer',dict(inputs=inputs,options=['cloth','brush']))
            self.assertEqual(answer['data']['selected_option'],'1')
            self.assertEqual(calls[-1]['max_new_tokens'],1024)
            self.assertEqual(answer['decoder'],'K-constrained-prediction-letter-v1')
            l_answer=backend.call('l_answer',dict(inputs=inputs,options=['cloth','brush']))
            self.assertEqual(l_answer['data'],answer['data'])
            self.assertFalse(calls[-1]['do_sample'])
            self.assertGreater(answer['usage']['input_tokens'],0)
            self.assertGreater(answer['usage']['output_tokens'],0)
            network.ended=False
            with self.assertRaisesRegex(ValueError,'terminate normally') as incomplete:
                generate(backend,'m_answer',dict(inputs=inputs,options=['cloth','brush']))
            self.assertEqual(incomplete.exception.raw_output['raw'], 'prediction: B')
            self.assertFalse(incomplete.exception.raw_output['ended_normally'])
            from script_core.backend import SoftDeadlineExceeded
            with patch.object(backend, 'check_deadline', side_effect=[None, SoftDeadlineExceeded('stop')]):
                with self.assertRaises(SoftDeadlineExceeded) as interrupted:
                    generate(backend,'m_answer',dict(inputs=inputs,options=['cloth','brush']))
            diagnostic = interrupted.exception.raw_output
            self.assertEqual(diagnostic['raw'], 'prediction: B')
            self.assertFalse(diagnostic['ended_normally'])
            self.assertGreater(len(diagnostic['generated_token_ids']), 0)
            self.assertEqual(diagnostic['usage']['output_tokens'], len(diagnostic['generated_token_ids']))
            self.assertEqual(diagnostic['max_new_tokens'], 1024)
            before=len(calls)
            backend.cfg['models']['max_input_tokens']=1
            with self.assertRaisesRegex(ValueError,'overflow'):
                generate(backend,'m_answer',dict(inputs=inputs,options=['cloth','brush']))
            self.assertEqual(len(calls),before)

    def test_selector_decoder_couples_exclusion_with_zero_priority(self):
        import json
        from lmformatenforcer import JsonSchemaParser
        from script_core.m_scene_memory import output_schema,validate_candidates
        from script_core.schemas import decoder_schema
        inputs=dict(directory=[dict(id='S0'),dict(id='S1')])
        schema=decoder_schema(output_schema('m_select_scene',inputs))
        raw=dict(scene_decisions={s:dict(relevance='excluded',priority=0,reason='Unrelated') for s in ['S0','S1']},
                 reason='No related summaries')
        self.assertEqual(validate_candidates(raw,inputs),[])
        for priority,expected in [(0,True),(100,False)]:
            item=deepcopy(raw);item['scene_decisions']['S0']['priority']=priority
            parser=JsonSchemaParser(schema);parser.config.force_json_field_order=True
            accepted=True
            for char in json.dumps(item,separators=(',',':')):
                if char not in parser.get_allowed_characters():accepted=False;break
                parser=parser.add_character(char)
            self.assertEqual(accepted,expected)

    def test_selector_nonempty_chinese_reason_cannot_end_json_early(self):
        import json
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        from script_core.m_scene_memory import output_schema
        from script_core.schemas import decoder_schema
        from script_core.backend import constrained_prefix
        schema=decoder_schema(output_schema('m_select_scene',dict(directory=[dict(id='ID0'),dict(id='ID1')])))
        prefix=constrained_prefix(build_token_enforcer_tokenizer_data(self.tokenizer),schema,'m_select_scene')
        start='{\n"scene_decisions": {\n"ID0": {\n"relevance": "excluded",\n"priority": 0,\n"reason": "场景'
        state=torch.tensor([self.tokenizer.eos_token_id])
        for token in self.tokenizer.encode(start,add_special_tokens=False):
            self.assertIn(token,prefix(0,state))
            state=torch.cat([state,torch.tensor([token])])
        self.assertNotIn(self.tokenizer.eos_token_id,prefix(0,state))
        tail='无关"},"ID1":{"relevance":"excluded","priority":0,"reason":"桌子无关"}},"reason":"没有相关场景"}'
        for token in self.tokenizer.encode(tail,add_special_tokens=False):
            self.assertIn(token,prefix(0,state))
            state=torch.cat([state,torch.tensor([token])])
        self.assertIn(self.tokenizer.eos_token_id,prefix(0,state))

    def test_navigation_decoder_requires_citations_for_decided_options(self):
        import json
        from script_core import m_scene_memory
        from script_core.backend import constrained_prefix
        from script_core.schemas import decoder_schema
        from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
        schema=decoder_schema(m_scene_memory.output_schema('m_transition_next',dict(
            options=['cloth','brush'],captions=[dict(id='C0')],allowed_scene_ids=['S1'])))
        data=build_token_enforcer_tokenizer_data(self.tokenizer)
        for status in ['supported','ruled_out','unresolved']:
            prefix=constrained_prefix(data,schema,'m_transition_next')
            raw=json.dumps(dict(action='stop',evidence_status='insufficient',supported_option='unresolved',option_comparison='Compare observed facts',
                           option_assessments=dict(O0=dict(reason='Check source',status=status,source_caption_ids=[]))),
                           separators=(',',':'))
            # The shared source key is a valid prefix. A decided option's empty
            # source list must be blocked during token generation, before parsing.
            end=raw.index('[]')+2
            ids=self.tokenizer.encode(raw[:end],add_special_tokens=False)
            state=torch.tensor([[self.tokenizer.bos_token_id or self.tokenizer.eos_token_id]])
            allowed=True
            for token in ids:
                if token not in prefix(0,state[0]):
                    allowed=False
                    break
                state=torch.cat([state,torch.tensor([[token]])],dim=1)
            self.assertEqual(allowed,status=='unresolved')

    def test_gap_decoder_binds_read_action_to_local_unavailability(self):
        import json
        from lmformatenforcer import JsonSchemaParser
        from script_core.m_scene_memory import output_schema
        from script_core.schemas import decoder_schema
        schema=decoder_schema(output_schema('m_resolve_gap',dict(
            captions=[dict(id='C1')],allowed_scene_ids=['S0'],entities=[])))
        raw=dict(resolution_status='unresolved',reason='Before condition missing',
            gap='Need before condition',action='read_scene',scene_id='S0',
            current_scene_assessment='cannot_resolve_locally',local_unavailable_reason='Only after is described')
        for value,expected in [('cannot_resolve_locally',True),('needs_local_detail',False)]:
            item=dict(raw,current_scene_assessment=value)
            parser=JsonSchemaParser(schema)
            parser.config.force_json_field_order=True
            accepted=True
            for char in json.dumps(item,separators=(',',':')):
                if char not in parser.get_allowed_characters():
                    accepted=False
                    break
                parser=parser.add_character(char)
            self.assertEqual(accepted,expected)

    def test_navigation_decoder_binds_insufficient_stop_to_nonempty_gap(self):
        import json
        from lmformatenforcer import JsonSchemaParser
        from script_core.m_scene_memory import output_schema
        from script_core.schemas import decoder_schema
        schema=decoder_schema(output_schema('m_transition_next',dict(
            options=['cloth','brush'],captions=[dict(id='C1')],allowed_scene_ids=['S0'],entities=[])))
        raw=dict(action='stop',evidence_status='insufficient',supported_option='unresolved',
            option_comparison='Need earlier condition',option_assessments={f'O{i}':dict(
                reason='Earlier condition unknown',status='unresolved',source_caption_ids=[]) for i in range(2)},
            reason='No useful step',gap='Need earlier condition',scene_id='stop',
            current_scene_assessment='no_useful_step',local_unavailable_reason='')
        for updates,expected in [({},True),({'gap':''},False),
                ({'current_scene_assessment':'needs_local_detail'},False)]:
            parser=JsonSchemaParser(schema);parser.config.force_json_field_order=True
            accepted=True
            for char in json.dumps(dict(raw,**updates),separators=(',',':')):
                if char not in parser.get_allowed_characters():
                    accepted=False;break
                parser=parser.add_character(char)
            self.assertEqual(accepted,expected)


if __name__=='__main__':unittest.main()
