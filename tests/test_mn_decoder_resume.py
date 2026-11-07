from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import torch
from transformers import AutoTokenizer
from lmformatenforcer import JsonSchemaParser
from lmformatenforcer.integrations.transformers import (
    build_token_enforcer_tokenizer_data, build_transformers_prefix_allowed_tokens_fn)
from script_core.backend import constrained_prefix, navigation_tokenizer_data
from script import run_mn_decoder_resume as resume
from script import controlled_memory_io as io


class NavigationUnicodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        cls.data=build_token_enforcer_tokenizer_data(cls.tokenizer)

    def test_saved_failure_reproduces_false_eos_and_fixed_decoder_blocks_byte_fragment(self):
        fixture=io.read(Path(__file__).parent/'fixtures/mn_navigation_unclosed_call.json')
        raw=fixture['output']['raw_text']; text=raw.rsplit('"reason": "',1)[-1]
        self.assertEqual(len(text),160)
        self.assertEqual(text[-1],'扥')
        schema=dict(type='object',properties=dict(reason=dict(type='string',maxLength=160)),
                    required=['reason'],additionalProperties=False)
        raw='{"reason":"'+text
        for fixed in [False,True]:
            if fixed:
                prefix=constrained_prefix(self.data,schema,'m_transition_next')
            else:
                parser=JsonSchemaParser(schema)
                prefix=build_transformers_prefix_allowed_tokens_fn(self.data,parser)
                parser.context.alphabet_without_quotes=parser.config.alphabet.replace('"','')
            state=torch.tensor([self.tokenizer.eos_token_id]);blocked=False
            for char in raw:
                for token in self.tokenizer.encode(char,add_special_tokens=False):
                    if token not in prefix(0,state):
                        self.assertTrue(fixed);self.assertEqual(char,'扥');blocked=True;break
                    state=torch.cat([state,torch.tensor([token])])
                if blocked:break
            if fixed:
                self.assertTrue(blocked)
                self.assertNotIn(self.tokenizer.eos_token_id,prefix(0,state))
                for token in self.tokenizer.encode('"}',add_special_tokens=False):
                    self.assertIn(token,prefix(0,state));state=torch.cat([state,torch.tensor([token])])
            self.assertIn(self.tokenizer.eos_token_id,prefix(0,state))

    def test_chinese_escapes_and_length_boundary_close_normally_all_navigation_ops(self):
        for operation in resume.REPAIR_OPERATIONS:
            schema=dict(type='object',properties=dict(reason=dict(type='string',minLength=1,maxLength=30)),
                        required=['reason'],additionalProperties=False)
            for text in ['{"reason":"需要读取后续场景"}',r'{"reason":"\u6265"}', '{"reason":"'+'x'*30+'"}']:
                prefix=constrained_prefix(self.data,schema,operation)
                state=torch.tensor([self.tokenizer.eos_token_id])
                for token in self.tokenizer.encode(text,add_special_tokens=False):
                    self.assertNotIn(self.tokenizer.eos_token_id,prefix(0,state))
                    self.assertIn(token,prefix(0,state));state=torch.cat([state,torch.tensor([token])])
                self.assertIn(self.tokenizer.eos_token_id,prefix(0,state))
        safe=navigation_tokenizer_data(self.data)
        self.assertIs(safe,navigation_tokenizer_data(self.data))
        self.assertTrue(all('\ufffd' not in text for _,text,_ in safe.regular_tokens))
        self.assertTrue(any('\ufffd' in text for _,text,_ in self.data.regular_tokens))

    def test_saved_candidate_failure_false_eos_is_blocked_without_changing_schema(self):
        fixture=io.read(Path(__file__).parent/'fixtures/mn_candidate_unclosed_call.json')
        text=fixture['output']['raw_text'].rsplit('"retrieval_question": "',1)[-1]
        self.assertEqual((len(text),text[-1]),(240,'繫'))
        self.assertEqual(fixture['output']['usage']['output_tokens'],353)
        schema=dict(type='object',properties=dict(retrieval_question=dict(type='string',maxLength=240)),
                    required=['retrieval_question'],additionalProperties=False)
        for operation in ['old_candidate_decoder','m_candidate_query']:
            prefix=constrained_prefix(self.data,schema,operation)
            # The old candidate path already refreshed this cache, but still
            # used partial UTF-8 tokens and the incomplete token alphabet.
            if operation=='old_candidate_decoder':
                parser=JsonSchemaParser(schema)
                prefix=build_transformers_prefix_allowed_tokens_fn(self.data,parser)
                parser.context.alphabet_without_quotes=parser.config.alphabet.replace('"','')
            state=torch.tensor([self.tokenizer.eos_token_id]);blocked=False
            for char in '{"retrieval_question":"'+text:
                for token in self.tokenizer.encode(char,add_special_tokens=False):
                    if token not in prefix(0,state):
                        self.assertEqual(operation,'m_candidate_query')
                        self.assertEqual(char,'繫');blocked=True;break
                    state=torch.cat([state,torch.tensor([token])])
                if blocked:break
            if operation=='old_candidate_decoder':
                self.assertFalse(blocked)
                self.assertIn(self.tokenizer.eos_token_id,prefix(0,state))
            else:
                self.assertTrue(blocked)
                self.assertNotIn(self.tokenizer.eos_token_id,prefix(0,state))
                for token in self.tokenizer.encode('"}',add_special_tokens=False):
                    self.assertIn(token,prefix(0,state));state=torch.cat([state,torch.tensor([token])])
                self.assertIn(self.tokenizer.eos_token_id,prefix(0,state))
                json.loads(self.tokenizer.decode(state[1:],skip_special_tokens=True))

    def test_candidate_query_length_boundary_and_unchanged_rank_chinese_source(self):
        from script_core import m_candidate_selection as selection, schemas
        request=dict(question='Which activity?',options=['repair','wash'],
            directory=[dict(id='S0',description='C 修理绳子。')])
        outputs={
            'm_candidate_query':dict(option_claims={f'O{i}':dict(
                observable_claim='修理工具' if i==0 else 'x'*180,
                proposed_explanation='') for i in range(2)},retrieval_question='x'*240),
            'm_candidate_rank':dict(scene_reviews=dict(S0=dict(
                anchor='C 修理绳子。',evidence_level='task_evidence')))}
        for operation,output in outputs.items():
            schema=selection.output_schema(operation,request)
            wire=json.dumps(output,ensure_ascii=False,separators=(',',':'))
            prefix=constrained_prefix(self.data,schemas.decoder_schema(schema),operation)
            state=torch.tensor([self.tokenizer.eos_token_id])
            for token in self.tokenizer.encode(wire,add_special_tokens=False):
                self.assertNotIn(self.tokenizer.eos_token_id,prefix(0,state))
                self.assertIn(token,prefix(0,state));state=torch.cat([state,torch.tensor([token])])
            self.assertIn(self.tokenizer.eos_token_id,prefix(0,state))
            schemas.validate_output(json.loads(wire),schema)

    def test_rank_rare_literal_limitation_is_unchanged_from_legacy_decoder(self):
        # LMFE's legacy enum decoder cannot traverse this split-byte literal.
        # Record that limitation; do not claim the free-text fix repairs enums.
        from script_core import m_candidate_selection as selection
        request=dict(options=['repair'],directory=[dict(id='S0',description='C 修理繫绳。')])
        schema=selection.output_schema('m_candidate_rank',request)
        legacy=JsonSchemaParser(schema)
        baseline=build_transformers_prefix_allowed_tokens_fn(self.data,legacy)
        legacy.context.alphabet_without_quotes=legacy.config.alphabet.replace('"','')
        legacy.config.force_json_field_order=True
        legacy.config.max_consecutive_whitespaces=1
        current=constrained_prefix(self.data,schema,'m_candidate_rank')
        wire=json.dumps(dict(scene_reviews=dict(S0=dict(anchor='C 修理繫绳。',
            evidence_level='task_evidence'))),ensure_ascii=False,separators=(',',':'))
        state=torch.tensor([self.tokenizer.eos_token_id]);blocked=False
        for token in self.tokenizer.encode(wire,add_special_tokens=False):
            old_allowed=baseline(0,state);new_allowed=current(0,state)
            self.assertEqual(set(old_allowed),set(new_allowed))
            if token not in old_allowed:
                blocked=True;self.assertEqual(old_allowed,[]);break
            state=torch.cat([state,torch.tensor([token])])
        self.assertTrue(blocked)


class ResumeTests(unittest.TestCase):
    def test_saved_gate_dispatches_original_operation_and_checks_union_schema(self):
        cases=[('m_candidate_query',dict(question='Which activity?',options=['wash']),
                dict(option_claims=dict(O0=dict(observable_claim='wash',proposed_explanation='')),
                     retrieval_question='Which activity?')),
               ('m_sufficiency_check',dict(captions=[]),dict(reason='Need earlier evidence',
                    verdict='unresolved',source_caption_ids=[],missing_detail='Earlier condition'))]
        for operation,request,result in cases:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as td:
                out=Path(td)
                io.write(out/'experiment.json',dict(experiment_label='M'))
                io.write(out/'validation/failed_navigation_input.json',request)
                io.write(out/'validation/failed_decoder_call.json',dict(operation=operation))
                with patch('script_core.budget.Meter') as meter, patch.object(resume.common,'functional') as full:
                    meter.return_value.json.return_value=(result,{})
                    full.return_value=dict(passed=True)
                    gate=resume.functional(out,object(),{})
                    self.assertTrue(gate['saved_navigation_decoder_passed'])
                    self.assertEqual(gate['saved_decoder_operation'],operation)
                    self.assertEqual(meter.return_value.json.call_args.args[:2],(operation,request))
                    self.assertEqual(meter.return_value.json.call_count,1)
                    full.assert_called_once()
                    meter.return_value.close.assert_called_once()
                # A schema violation must preserve costs and prevent the full
                # gate/benchmark from starting, with no implicit second call.
                with patch('script_core.budget.Meter') as meter, patch.object(resume.common,'functional') as full:
                    meter.return_value.json.return_value=({}, {})
                    with self.assertRaises(ValueError):resume.functional(out,object(),{})
                    full.assert_not_called()
                    self.assertEqual(meter.return_value.json.call_count,1)
                    self.assertFalse(io.read(out/'validation/functional.json')['passed'])
                    self.assertTrue((out/'functional/decoder/cost_summary.json').exists())

    def test_resume_preserves_success_and_failure_and_counts_cost_once(self):
        for operation in ('m_transition_next','m_candidate_query'):
            with self.subTest(operation=operation):
                self.check_resume(operation)

    def test_controlled_upgrade_preserves_interrupted_calls_and_separates_cost(self):
        self.check_resume('m_candidate_query',controlled_upgrade=True)

    def test_controlled_upgrade_at_query_boundary_preserves_completed_prefix(self):
        self.check_resume('m_candidate_query',controlled_upgrade=True,clean_boundary=True)

    def check_resume(self,operation,controlled_upgrade=False,clean_boundary=False):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);prior=root/'prior';out=root/'new'
            for name,value in [('cohort.json',['ok','bad','pending']),('evaluation_only.json',dict(ok=0,bad=1,pending=1)),
                ('qa_config.json',dict(query=dict(temporal_qa_evidence='scene_planned_script'))),
                ('qa_expected_artifacts.json',{}),('experiment.json',dict(experiment_label='M',cpu_preparation_seconds=2.)),
                ('queue_status.json',dict(state='failed')),('setup.json',dict(seconds=3.)),('report.json',{}),
                ('functional/cost_summary.json',dict(seconds=4.)),('validation/functional.json',dict(passed=True)),
                ('launch.json',dict(worker_pid=999999999)),('controller_launch.json',dict(pid=999999998)),
                ('runs/ok/result.json',dict(answer=dict(selected_option='0'))),
                ('runs/ok/partial_temporal/result.json',dict(memory_variant='M')),
                ('runs/ok/partial_search/result.json',dict(opened_scenes=['S0'],retrieval=dict(candidate_order=['S0']))),
                ('runs/ok/partial_search/navigation_trace.json',dict(original=True)),
                ('runs/bad/failure.json',dict(error='invalid JSON')),('inputs/ok.json',dict(source='original')),
                ('selection_validation/report.json',dict(passed=True)),('selection_validation/semantic_review.json',dict(passed=True))]:
                io.write(prior/name,value)
            io.write(prior/'runs/bad/raw.json',dict(inputs={},output=dict(raw_text='{"reason":"unfinished')))
            for uid,op,cost in [('ok','m_answer',5.),('bad',operation,6.)]:
                p=prior/'runs'/uid/'partial_search/calls.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
                p.write_text(json.dumps(dict(operation=op,actual_cost=cost,
                    output_ref=str(prior/'runs/bad/raw.json')))+'\n')
            (prior/'runtime/script').mkdir(parents=True)
            (prior/'runtime/script_core').mkdir(parents=True)
            (prior/'runtime/script_core/unchanged.py').write_text('original')
            old_tuple="('m_transition_next', 'm_sufficiency_check', 'm_resolve_gap')"
            old_backend=(resume.ROOT/'script_core/backend.py').read_text().replace(
                "('m_transition_next', 'm_sufficiency_check', 'm_resolve_gap', 'm_candidate_query')",
                old_tuple).replace(resume.DECODER,'MN-navigation-complete-unicode-v1').replace(
                'MN-navigation-unicode-scalar-v3','MN-navigation-complete-unicode-v1')
            old_backend+='\n# Frozen-only sentinel, must survive continuation.\n'
            (prior/'runtime/script_core/backend.py').write_text(old_backend)
            io.write(prior/'frozen_hashes.json',{'inputs/ok.json':io.sha(prior/'inputs/ok.json')})
            before=resume.tree_hashes(prior)
            kwargs={}
            if controlled_upgrade:
                probe=root/'probe.json'
                io.write(probe,dict(inputs={},output=dict(raw_text='{"retrieval_question":"unfinished')))
                record=root/'upgrade.json'
                io.write(record,dict(prior_run=str(prior),between_calls_no_pending=True,
                    reason='user_requested_decoder_upgrade'))
                kwargs=dict(upgrade_record=record,decoder_probe=probe)
                # An unlogged in-flight call must not disappear from costs.
                pending=prior/'runs/bad/call.pending.json';io.write(pending,{})
                with self.assertRaisesRegex(ValueError,'explicit accounting'):
                    resume.prepare(prior,out,**kwargs)
                pending.unlink()
                if clean_boundary:
                    import shutil
                    shutil.rmtree(prior/'runs/bad')
                before=resume.tree_hashes(prior)
            resume.prepare(prior,out,**kwargs)
            self.assertEqual(before,resume.tree_hashes(prior))
            policy=io.read(out/'experiment.json');report=io.read(out/'report.json')
            self.assertEqual(policy['resume_successful_ids'],['ok'])
            self.assertEqual(policy['resume_question_id'],'bad')
            self.assertEqual(policy['failed_decoder_operation'],operation)
            self.assertEqual(io.read(out/'validation/failed_decoder_call.json')['operation'],operation)
            self.assertEqual(policy['prior_preparation_seconds'],2.)
            self.assertEqual((out/'runtime/script_core/backend.py').read_text(),
                             resume.patch_frozen_backend(old_backend))
            self.assertEqual(report['measured_query_seconds'],5.)
            self.assertEqual(report['prior_runtime_seconds'],7. if clean_boundary else 13.)
            self.assertEqual(report['prior_failed_query_seconds'],0. if controlled_upgrade else 6.)
            self.assertEqual(report['prior_interrupted_query_seconds'],6. if controlled_upgrade and not clean_boundary else 0.)
            self.assertEqual((report['successful'],report['errors'],report['pending']),(1,0,2))
            self.assertFalse((out/'runs/bad').exists())
            self.assertEqual(resume.tree_hashes(prior/'runs/ok'),resume.tree_hashes(out/'runs/ok'))
            archive_name='interrupted_query' if controlled_upgrade else 'failed_query'
            if not clean_boundary:
                self.assertEqual(resume.tree_hashes(prior/'runs/bad'),resume.tree_hashes(out/'archived_attempt'/archive_name))
            else:
                self.assertFalse((out/'archived_attempt/interrupted_query').exists())
            resume.common.verify(out)
            with self.assertRaisesRegex(ValueError,'fresh directory'):resume.prepare(prior,out)
            (out/'runs/ok/result.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'Frozen'):resume.common.verify(out)


if __name__=='__main__':unittest.main()
