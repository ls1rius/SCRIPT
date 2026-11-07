from copy import deepcopy
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch
import torch
from lmformatenforcer import JsonSchemaParser
from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data,build_transformers_prefix_allowed_tokens_fn
from transformers import AutoTokenizer
from script import controlled_memory_io as io
from script import run_o_sentence_experiment as runner
from script import o_composition_context_eval as matrix
from script_core import o_sentence_filter as core,o_generation
from script_core.config import load_config


class UnicodeAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer=AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        cls.data=build_token_enforcer_tokenizer_data(cls.tokenizer)

    def traverse(self,fn,wire):
        state=torch.tensor([self.tokenizer.eos_token_id])
        # Follow a legal character-wise path. Greedy BPE can combine a JSON
        # escape across LMFE parser states and is not itself a legal-path oracle.
        for token in [t for c in wire for t in self.tokenizer.encode(c,add_special_tokens=False)]:
            allowed=fn(0,state)
            self.assertTrue(self.tokenizer.eos_token_id not in allowed,'premature EOS')
            self.assertTrue(token in allowed,f'blocked token {token} after {self.tokenizer.decode(state[-30:])!r}')
            state=torch.cat([state,torch.tensor([token])])
        self.assertIn(self.tokenizer.eos_token_id,fn(0,state))

    def test_actual_o_schemas_unicode_escapes_and_length_limits(self):
        report=dict(extraction_status='extracted',actions=[dict(actor='张三',verb='搬动',roles=dict(patient='x'*154+'繫'))],
            attributes=[],states=[],relations=[],changes=[],results=[],event_links=[])
        cases=[(core.EXTRACT,dict(sentence='张三搬动箱子。'),report),
            (core.FILTER,dict(question='什么动作？',candidates=[dict(id='U0')]),dict(observation='x'*234+'繫',relevance='引用"动作"\n中文',labels={'U0':0})),
            (core.FILTER_PLAN,dict(question='什么动作？'),dict(option_tests=dict(Q=dict(single_observation='x'*234+'繫',aggregate_requirement='',interpretation='中文')))),
            (core.COMPOSE,dict(question='什么动作？',groups=dict(actions_0=[dict(id='U0',group='actions_0',kind='action_field',field='verb',value='x'*794+'繫')])),dict(groups=dict(actions_0=dict(text='x'*794+'繫'))))]
        for op,req,output in cases:
            with self.subTest(operation=op):
                _,schema,_,aliases=o_generation.decoder_contract(op,req)
                wire=deepcopy(output)
                for gid,mapping in aliases.items():
                    wire['groups'][gid]['text']=next(k for k,v in mapping.items() if v==output['groups'][gid]['text'])
                self.traverse(o_generation.constrained_prefix(self.data,schema),json.dumps(wire,ensure_ascii=True,separators=(',',':')))
                core.validate_output(output,core.schema(op,req))

    def test_o_filter_blocks_the_mn_early_eos_path(self):
        req=dict(question='Which?',candidates=[dict(id='U0')]);schema=core.schema(core.FILTER,req)
        raw='{"observation":"'+'x'*239+'繫'
        for safe in [False,True]:
            parser=JsonSchemaParser(schema)
            fn=o_generation.constrained_prefix(self.data,schema) if safe else build_transformers_prefix_allowed_tokens_fn(self.data,parser)
            if not safe:parser.context.alphabet_without_quotes=parser.config.alphabet.replace('"','')
            state=torch.tensor([self.tokenizer.eos_token_id]);blocked=False
            for char in raw:
                for token in self.tokenizer.encode(char,add_special_tokens=False):
                    if token not in fn(0,state):
                        self.assertTrue(safe);self.assertNotIn(self.tokenizer.eos_token_id,fn(0,state));blocked=True;break
                    state=torch.cat([state,torch.tensor([token])])
                if blocked:break
            self.assertEqual(blocked,safe)
            if not safe:self.assertIn(self.tokenizer.eos_token_id,fn(0,state))

    def test_source_identity_enum_dead_end_and_safe_wire_choices(self):
        for actor in ['繫','张三','D"E','𠮷']:
            req=core.compose_input(dict(question='Who is identified?'),[dict(id='U0',group='actions_0',kind='action_field',field='actor',value=actor)])
            canonical,wire,inputs,aliases=o_generation.decoder_contract(core.COMPOSE,req)
            self.assertEqual(inputs['question'],req['question']);self.assertEqual(inputs['groups'],req['groups'])
            self.assertEqual(set(aliases['actions_0'].values()),{'Actor: '+actor+'.'})
            for text in ['',next(iter(aliases['actions_0']))]:
                output=dict(groups=dict(actions_0=dict(text=text)))
                self.traverse(o_generation.constrained_prefix(self.data,wire),json.dumps(output,separators=(',',':')))
            if actor=='繫':
                fn=o_generation.constrained_prefix(self.data,canonical)
                state=torch.tensor([self.tokenizer.eos_token_id])
                for c in '{"groups":{"actions_0":{"text":"Actor: ':
                    for t in self.tokenizer.encode(c,add_special_tokens=False):
                        self.assertIn(t,fn(0,state));state=torch.cat([state,torch.tensor([t])])
                self.assertEqual(list(fn(0,state)),[])


class Backend:
    def __init__(self,fail=None):self.calls=[];self.fail=fail
    def call(self,operation,request,timeout):
        self.calls.append((operation,deepcopy(request)))
        if operation==self.fail:raise RuntimeError('injected '+operation)
        if operation=='prepare_vlm':return {}
        if operation==core.EXTRACT:
            data=dict(extraction_status='extracted',actions=[dict(actor='D',verb='opens',roles=dict(patient='drawer'))],attributes=[],states=[],relations=[],changes=[],results=[],event_links=[])
        elif operation==core.FILTER_PLAN:
            keys=[f'O{i}' for i in range(len(request['options']))] if 'options' in request else ['Q']
            data=dict(option_tests={k:dict(single_observation='opens drawer',aggregate_requirement='',interpretation='') for k in keys})
        elif operation==core.FILTER:data=dict(observation='D opens drawer',relevance='action',labels={u['id']:1 for u in request['candidates']})
        elif operation==core.COMPOSE:data=dict(groups={g:dict(text=core.compose_renderings(units)[-1]) for g,units in request['groups'].items()})
        else:data=dict(selected_option='0',raw='prediction: A')
        return dict(data=data,usage=dict(input_tokens=10,output_tokens=10))
    def close(self,**kwargs):pass


class RunnerAuditTests(unittest.TestCase):
    def test_expanded_cohort_uses_order_without_outcomes(self):
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td)
            io.write(parent/'cohort.json',['q0',runner.CASES[1]]+[f'q{i}' for i in range(1,12)]+[runner.CASES[0]])
            ids,additional=runner.diagnostic_cohort(parent,10)
            self.assertEqual(additional,[f'q{i}' for i in range(8)])
            self.assertEqual(ids,additional+runner.CASES)
            self.assertEqual(runner.diagnostic_cohort(parent,2),(runner.CASES,[]))

    def setup_run(self,out,*,matrix_mode=True):
        cfg=load_config('configs/script_N.yaml');cfg['query']['tool_transient_retries']=0;cfg['query']['json_repair_retries']=0
        io.write(out/'qa_config.json',cfg)
        io.write(out/'experiment.json',dict(case_ids=['case'],case_budget_seconds=240,composition_context_eval=matrix_mode,selection_replay_from='frozen',functional_only=False))
        io.write(out/'evaluation_only.json',dict(case='0'))
        q=dict(question='Who opens the drawer?',options=['D','E']);source=dict(id='S0',text='D opens a drawer.',source_intervals=[[0,1]])
        units=[dict(id='U0',group='actions_0',kind='action_field',field='actor',value='D'),dict(id='U1',group='actions_0',kind='action_field',field='verb',value='opens')]
        decision=dict(source=source,candidates=units,selected=units,labels={'U0':1,'U1':1},model_labels={'U0':1,'U1':1},program_overrides=[],fallback=None)
        inputs=out/'cases/case';io.write(inputs/'question.json',q);io.write(inputs/'sentences.json',[source])
        io.write(inputs/'original_qa.json',dict(head='Answer.',prompts=['  Description: old\n\n###\n\n Questions: Who opens the drawer?\nA. D\nB. E']))
        io.write(inputs/'selection_replay/selection.json',dict(S0=decision))
        io.write(inputs/'selection_replay/result.json',dict(arms={},cost_summary=dict(by_operation={'oplus_compose':2}),inherited_extraction_filter_costs={'o_extract':4,'o_filter':3,'o_filter_plan':2}))
        io.write(inputs/'parent_N/result.json',dict(answer=dict(selected_option='0')))
        return cfg

    def test_matrix_preserves_first_answer_and_failed_cost_without_retry(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);cfg=self.setup_run(out);backend=Backend(fail='oplus_answer')
            with patch.object(matrix,'probes'):
                with self.assertRaisesRegex(RuntimeError,'injected'):matrix.run(out,backend,cfg)
            result=io.read(out/'runs/case/result.json');self.assertFalse(result['complete'])
            self.assertIn('O_question_only',result['arms']);self.assertNotIn('Oplus_question_only',result['arms'])
            failed=result['conditions']['question_only'];self.assertFalse(failed['complete']);self.assertIsNotNone(failed['error'])
            self.assertIn('oplus_answer',failed['cost_summary']['by_operation'])
            self.assertEqual([op for op,_ in backend.calls].count('oplus_answer'),1)
            self.assertEqual(matrix.report(out)['errors'],1)
            self.assertTrue(list((out/'runs/case/question_only/raw_calls').glob('*.json')))

    def test_matrix_shared_budget_context_and_replay_cost_chain(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);cfg=self.setup_run(out);backend=Backend()
            with patch.object(matrix,'probes'):matrix.run(out,backend,cfg)
            result=io.read(out/'runs/case/result.json');self.assertTrue(result['complete']);self.assertEqual(len(result['arms']),4)
            a,b=[result['conditions'][k]['cost_summary'] for k in matrix.CONDITIONS]
            self.assertEqual(a['shared_dependency_seconds'],4)
            self.assertAlmostEqual(b['shared_dependency_seconds'],4+a['total'])
            self.assertEqual((a['total_cap'],b['total_cap']),(240,240))
            filters=[req for op,req in backend.calls if op==core.FILTER];compositions=[req for op,req in backend.calls if op==core.COMPOSE]
            for requests in [filters,compositions]:
                self.assertEqual([r['question'] for r in requests],['Who opens the drawer?']*2)
                self.assertNotIn('options',requests[0]);self.assertEqual(requests[1]['options'],['D','E'])
            self.assertEqual(runner.extraction_filter_costs(io.read(out/'cases/case/selection_replay/result.json')),{'o_extract':4,'o_filter':3,'o_filter_plan':2})

    def test_matrix_new_sources_extracted_once_and_shared_between_conditions(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);cfg=self.setup_run(out);backend=Backend()
            shutil.rmtree(out/'cases/case/selection_replay')
            policy=io.read(out/'experiment.json');policy['additional_case_ids']=['case'];policy['known_case_ids']=[]
            io.write(out/'experiment.json',policy)
            with patch.object(matrix,'probes'):matrix.run(out,backend,cfg)
            result=io.read(out/'runs/case/result.json')
            self.assertTrue(result['new_extraction']);self.assertTrue(result['complete'])
            extracts=[req for op,req in backend.calls if op==core.EXTRACT]
            self.assertEqual(extracts,[{'sentence':'D opens a drawer.'}])
            filters=[req for op,req in backend.calls if op==core.FILTER]
            self.assertEqual(len(filters),2);self.assertEqual(filters[0]['candidates'],filters[1]['candidates'])
            self.assertEqual(len(result['arms']),4)
            cost=result['shared_extraction_cost_summary']['total']
            self.assertEqual(result['conditions']['question_only']['cost_summary']['shared_dependency_seconds'],cost)
            report=matrix.report(out);self.assertEqual(report['subgroups']['additional']['completed'],1)
            self.assertEqual(report['subgroups']['known_controls']['completed'],0)

    def test_shared_extraction_failure_is_saved_and_stops_without_filtering(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);cfg=self.setup_run(out);backend=Backend(fail=core.EXTRACT)
            shutil.rmtree(out/'cases/case/selection_replay')
            with patch.object(matrix,'probes'):
                with self.assertRaisesRegex(RuntimeError,'injected'):matrix.run(out,backend,cfg)
            result=io.read(out/'runs/case/result.json')
            self.assertFalse(result['complete']);self.assertIsNotNone(result['error'])
            self.assertIn(core.EXTRACT,result['shared_extraction_cost_summary']['by_operation'])
            self.assertEqual([op for op,_ in backend.calls],[core.EXTRACT])

    def test_paired_worker_uses_real_question_not_probe_and_saves_startup_failure(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);self.setup_run(out,matrix_mode=False);backend=Backend()
            class Worker:
                def __new__(cls,*args):return backend
            with patch.object(runner,'verify'),patch('script.run_evidence_planner_experiment.verify_model'),patch.object(runner,'functional'),patch('script_core.budget.ModelWorker',Worker):
                runner.worker(out)
            compose=[req for op,req in backend.calls if op==core.COMPOSE]
            self.assertEqual(compose[0]['question'],'Who opens the drawer?')
            self.assertNotIn('Which movement',str(compose))
            result=io.read(out/'runs/case/result.json');self.assertTrue(result['complete'])
            self.assertEqual(result['cost_summary']['shared_dependency_seconds'],9)
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);self.setup_run(out);backend=Backend(fail='prepare_vlm')
            class Worker:
                def __new__(cls,*args):return backend
            with patch.object(runner,'verify'),patch('script.run_evidence_planner_experiment.verify_model'),patch('script_core.budget.ModelWorker',Worker):
                with self.assertRaisesRegex(RuntimeError,'injected'):runner.worker(out)
            self.assertTrue((out/'failure.json').exists());self.assertEqual(matrix.report(out)['errors'],1)
