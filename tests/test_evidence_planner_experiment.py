from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script import run_evidence_planner_experiment as run
from script_core.qa_evidence_view import compact_description
from tests.test_evidence_planner import fixture


class Tokenizer:
    def apply_chat_template(self, *args, **kwargs):
        return [0] * 100
    def encode(self, text):
        return list(text)


def setup(root):
    parent, after, out = (root / name for name in ('F', 'J_queue', 'K'))
    memory, original = fixture()
    prefix, _, suffix = io.parts(original)
    original['prompts'][0] = prefix + compact_description(memory)[0] + suffix
    io.write(parent / 'experiment.json', dict(arm=run.PARENT_ARM))
    io.write(parent / 'report.json', dict(complete=True, measured_seconds_by_stage=dict(materialized=12.5)))
    io.write(parent / 'validation/functional.json', dict(passed=True))
    io.write(parent / 'cohort.json', ['q'])
    io.write(parent / 'evaluation_only.json', dict(q='0'))
    io.write(parent / 'qa_config.json', dict(models=dict(vlm='local-model', precision='bfloat16',
        max_input_tokens=16384, generation=dict(do_sample=False, max_new_tokens=1024))))
    io.write(parent / 'qa_expected_artifacts.json', dict(vlm=dict(path='local-model', files={})))
    io.write(parent / 'packets/q.json', dict(packet=dict(question=dict(question_id='q',video_id='v',
        question='Which tool?',options=['cloth','brush','knife','spoon','paper']))))
    io.write(parent / 'materialized/q/memory.json', memory)
    io.write(parent / 'materialized/q/sealed.json', {'memory.json':io.sha(parent/'materialized/q/memory.json')})
    path=parent/'final_inputs'/run.PARENT_ARM/'q.json'
    io.write(path,original)
    io.write(parent/'runs'/run.PARENT_ARM/'q/result.json',dict(selected_option='0',error=None,input_sha256=io.sha(path)))
    for name in ('model.py','prompts.py'):
        p=parent/'runtime/qa'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# frozen runtime\n')
    io.write(parent/'frozen_hashes.json',{'experiment.json':io.sha(parent/'experiment.json')})
    io.write(after/'plan.json',dict(output_J=str(root/'J')))
    io.write(after/'frozen_hashes.json',{'plan.json':io.sha(after/'plan.json')})
    io.write(after/'queue_status.json',dict(state='waiting_for_I'))
    return parent,after,out


class ExperimentTests(unittest.TestCase):
    def test_strict_reader_accepts_only_complete_valid_prediction_letters(self):
        options = ['C reads a book. Then takes a photo.', 'C reads and sleeps.',
            'C writes in a book.', 'C closes a book.', 'C throws a book away.']
        for index in range(5):
            raw = 'prediction: ' + chr(65+index)
            self.assertEqual(run.parse_strict_answer(raw, options)['option'], index)
            parser = run.strict_answer_parser(options)
            for char in raw:
                self.assertIn(char, parser.get_allowed_characters())
                parser = parser.add_character(char)
            self.assertTrue(parser.can_end())
            self.assertNotIn('\n', parser.get_allowed_characters())
        for raw in ('prediction: F', 'prediction: C reads a book.', 'prediction: A\nexplanation: ...',
                    'prediction: A\nprediction: B', 'prediction: C reads and takes a photo.',
                    'C', 'prediction: ', 'prediction: a', 'prediction: A or B'):
            self.assertIsNone(run.parse_strict_answer(raw, options)['option'])
        for options in ([], [''], ['a']*27, 'abc'):
            with self.assertRaises(ValueError):run.strict_answer_parser(options)

    def test_strict_reader_real_tokens_block_paraphrases_and_early_eos(self):
        import torch
        from transformers import AutoTokenizer
        from lmformatenforcer.integrations.transformers import build_transformers_prefix_allowed_tokens_fn
        project = Path(__file__).resolve().parents[1]
        tokenizer = AutoTokenizer.from_pretrained(project/'checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        for letter in 'ABCDE':
            fn = build_transformers_prefix_allowed_tokens_fn(tokenizer,run.strict_answer_parser(list('abcde')))
            tokens = tokenizer.encode('prompt',add_special_tokens=False)
            allowed = fn(0,torch.tensor(tokens))
            self.assertNotIn(tokenizer.eos_token_id,allowed)
            for token in tokenizer.encode('prediction: '+letter,add_special_tokens=False):
                self.assertIn(token,fn(0,torch.tensor(tokens)))
                tokens.append(token)
            allowed = fn(0,torch.tensor(tokens))
            self.assertIn(tokenizer.eos_token_id,allowed)
            self.assertNotIn(tokenizer.encode(' reads',add_special_tokens=False)[0],allowed)

    def test_nonempty_decoder_blocks_all_skip_at_realistic_sizes(self):
        from script_core import evidence_planner as core
        for count in (1, 3, 115, 130, 315):
            pool = dict(records=[dict(id=f'R{i}') for i in range(count)])
            schema = core.plan_schema(pool)
            def accepts(decisions):
                parser = run.nonempty_plan_parser(schema)
                for char in json.dumps(decisions, separators=(',', ':')):
                    if char not in parser.get_allowed_characters():
                        return False
                    parser = parser.add_character(char)
                return parser.can_end()
            decisions = {r['id']: 'skip' for r in pool['records']}
            self.assertFalse(accepts(decisions))
            for retained in ('keep', 'uncertain', 'group:R0'):
                decisions[f'R{count-1}'] = retained
                self.assertTrue(accepts(decisions))
            decisions = {r['id']: 'skip' for r in pool['records']}
            decisions['R0'] = 'keep'
            self.assertTrue(accepts(decisions))

    def test_nonempty_decoder_masks_skip_tokens_with_real_tokenizer(self):
        import torch
        from transformers import AutoTokenizer
        from lmformatenforcer.integrations.transformers import build_transformers_prefix_allowed_tokens_fn
        from script_core import evidence_planner as core
        project = Path(__file__).resolve().parents[1]
        tokenizer = AutoTokenizer.from_pretrained(project/'checkpoints/Qwen/Qwen2.5-VL-7B-Instruct', local_files_only=True)
        schema = core.plan_schema(dict(records=[dict(id='R0'), dict(id='R1')]))
        fn = build_transformers_prefix_allowed_tokens_fn(tokenizer, run.nonempty_plan_parser(schema))
        tokens = tokenizer.encode('start', add_special_tokens=False)
        fn(0, torch.tensor(tokens))  # establish the prompt length
        prefix = '{"R0":"skip","R1":"'
        for token in tokenizer.encode(prefix, add_special_tokens=False):
            self.assertIn(token, fn(0, torch.tensor(tokens)))
            tokens.append(token)
        allowed = fn(0, torch.tensor(tokens))
        self.assertNotIn(tokenizer.encode('skip', add_special_tokens=False)[0], allowed)
        for token in tokenizer.encode('keep"}', add_special_tokens=False):
            self.assertIn(token, fn(0, torch.tensor(tokens)))
            tokens.append(token)
        self.assertIn(tokenizer.eos_token_id, fn(0, torch.tensor(tokens)))

    def test_k_unique_first_sentence_is_gold_blind_and_conservative(self):
        options = [
            'C is making a cup of coffee. Repetition mixes it evenly.',
            'C is stirring a pot of soup. Repetition cooks it evenly.',
            'C is painting a piece of furniture. Repetition spreads paint evenly.',
            'C is washing the dirty dishes. Repetition cleans every dish.',
            'C is brushing his teeth carefully. Repetition cleans the teeth.']
        raw = 'prediction: C is painting a piece of furniture.\nexplanation: painting\nconfidence: 95'
        self.assertEqual(run.parse_final_answer(raw, options),
            dict(option=2, method='unique_verbatim_first_sentence'))
        # Reordering the choices changes the matched index; no gold is consulted.
        self.assertEqual(run.parse_final_answer(raw, list(reversed(options)))['option'], 2)
        reordered = options[2:] + options[:2]
        self.assertEqual(run.parse_final_answer(raw, reordered)['option'], 0)
        duplicate = options + ['C is painting a piece of furniture. A different reason.']
        self.assertIsNone(run.parse_final_answer(raw, duplicate)['option'])
        for prediction in ('C', 'C is painting', 'C is painting furniture.',
                           'C is not painting a piece of furniture.',
                           'C is painting a piece of furniture. Wrong additional claim.'):
            if prediction == 'C':
                self.assertEqual(run.parse_final_answer('prediction: C', options)['option'], 2)
            else:
                self.assertIsNone(run.parse_final_answer('prediction: ' + prediction, options)['option'])
        self.assertIsNone(run.parse_final_answer(raw + '\nprediction: A', options)['option'])
        # The shared parser used by other experiments keeps its original behavior.
        from script_core.g_rl import parse_answer
        self.assertIsNone(parse_answer(raw, options)['option'])

    def test_preparation_freezes_all_three_controls_and_isolated_entry(self):
        with tempfile.TemporaryDirectory() as td:
            parent,after,out=setup(Path(td))
            run.prepare(parent,out,after,Tokenizer())
            self.assertTrue((out/'pool/q.json').exists())
            self.assertTrue((out/'F_sentence/q.json').exists())
            self.assertTrue((out/'K_all_evidence/q.json').exists())
            self.assertEqual(io.read(out/'report.json')['arms'][run.ARM]['completed'],0)
            p=subprocess.run([sys.executable,str(out/'control/run_evidence_planner_experiment.py'),
                'report','--out',str(out)],cwd=td,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(json.loads(p.stdout)['inherited_F_construction_seconds'],12.5)
            (out/'control/evidence_planner.py').write_text('changed')
            with self.assertRaisesRegex(ValueError,'Frozen'):run.verify(out)

    def test_mismatched_parent_answer_or_source_seal_is_rejected(self):
        for kind in ('answer','seal'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as td:
                parent,after,out=setup(Path(td))
                if kind=='answer':io.write(parent/'runs'/run.PARENT_ARM/'q/result.json',dict(input_sha256='bad'))
                else:io.write(parent/'materialized/q/memory.json',dict(changed=True))
                with self.assertRaises(ValueError):run.prepare(parent,out,after,Tokenizer())
                self.assertFalse(out.exists())

    def test_waits_for_j_and_failure_stops_without_gpu_or_worker(self):
        with tempfile.TemporaryDirectory() as td:
            parent,after,out=setup(Path(td));run.prepare(parent,out,after,Tokenizer())
            def fail_after_wait(seconds):
                self.assertEqual(io.read(out/'queue_status.json')['state'],'waiting_for_J')
                self.assertEqual(seconds,30)
                io.write(after/'queue_status.json',dict(state='failed'))
            with patch.object(run.time,'sleep',side_effect=fail_after_wait),patch.object(run.subprocess,'Popen') as process:
                with self.assertRaisesRegex(RuntimeError,'predecessor failed'):run.queue(out)
                process.assert_not_called()
            self.assertEqual(io.read(out/'queue_status.json')['state'],'failed')
            self.assertFalse((out/'launch.json').exists())

    def test_dependency_cannot_finish_without_complete_j_results(self):
        with tempfile.TemporaryDirectory() as td:
            parent,after,out=setup(Path(td));run.prepare(parent,out,after,Tokenizer())
            policy=io.read(out/'experiment.json')
            self.assertFalse(run.dependency_ready(policy))
            io.write(after/'queue_status.json',dict(state='complete'))
            io.write(Path(td)/'J/queue_status.json',dict(state='complete'))
            io.write(Path(td)/'J/report.json',dict(complete=False))
            with self.assertRaises(RuntimeError):run.dependency_ready(policy)
            io.write(Path(td)/'J/report.json',dict(complete=True))
            self.assertTrue(run.dependency_ready(policy))

    def test_report_uses_completed_paired_denominators_and_separate_planning_cost(self):
        with tempfile.TemporaryDirectory() as td:
            parent,after,out=setup(Path(td));run.prepare(parent,out,after,Tokenizer())
            for arm,option in [(run.ARMS[0],'1'),(run.ARMS[1],'0')]:
                io.write(out/'runs/q'/arm/'result.json',dict(selected_option=option,error=None,input_tokens=100,seconds=2.,reader_protocol=run.READER_PROTOCOL))
            summary=run.report(out)
            self.assertIsNone(summary['arms'][run.ARM]['accuracy_among_completed'])
            self.assertEqual(summary['comparisons'][run.ARM+'_vs_F_sentence']['paired'],0)
            io.write(out/'runs/q'/run.ARM/'result.json',dict(selected_option='0',error=None,input_tokens=40,seconds=1.,reader_protocol=run.READER_PROTOCOL))
            io.write(out/'runs/q/planner_output.json',dict(input_tokens=200,output_tokens=30,seconds=3.))
            summary=run.report(out)
            self.assertTrue(summary['complete'])
            self.assertEqual(summary['comparisons'][run.ARM+'_vs_F_sentence']['corrected'],['q'])
            self.assertEqual(summary['mean_K_planner_plus_final_seconds'],4.)
            self.assertEqual(summary['planner_input_tokens'],200)
            self.assertEqual(summary['arms'][run.ARM]['mean_input_tokens'],40)

    def test_oversized_prompt_fails_before_creating_output(self):
        class Large(Tokenizer):
            def apply_chat_template(self,*args,**kwargs):return [0]*16385
        with tempfile.TemporaryDirectory() as td:
            parent,after,out=setup(Path(td))
            with self.assertRaisesRegex(ValueError,'overflow'):run.prepare(parent,out,after,Large())
            self.assertFalse(out.exists())

    def test_worker_runs_synthetic_gate_planning_and_three_fresh_answers_without_reading_gold(self):
        import torch
        from transformers import AutoTokenizer
        from script_core import evidence_planner as k
        project=Path(__file__).resolve().parents[1]
        tokenizer=AutoTokenizer.from_pretrained(project/'checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        calls=[]
        self_outer=self
        class Network:
            device='cpu'
            generation_config=SimpleNamespace(eos_token_id=[151645,151643])
            def generate(self,input_ids,**kwargs):
                calls.append(kwargs)
                self_outer.assertIn('prefix_allowed_tokens_fn', kwargs)
                text=tokenizer.decode(input_ids[0],skip_special_tokens=True)
                if '{"task":' in text:
                    start=text.index('{"task":')
                    task,_=json.JSONDecoder().raw_decode(text[start:])
                    ids=[r['id'] for r in task['evidence']]
                    skip=['R1'] if task['task']['question'].startswith('Which tool does C use') else []
                    raw=json.dumps({i:'skip' if i in skip else 'keep' for i in ids})
                else:
                    raw='prediction: D' if 'Options: A. brush; B. knife; C. spoon; D. cloth; E. paper' in text else 'prediction: A'
                tail=tokenizer.encode(raw,add_special_tokens=False)+[151643]
                return torch.cat([input_ids,torch.tensor([tail])],dim=1)
        class Model:
            def __init__(self,*args,**kwargs):
                self.tokenizer=tokenizer;self.model=Network();self.max_new_tokens=1024
        with tempfile.TemporaryDirectory() as td:
            parent,after,out=setup(Path(td));run.prepare(parent,out,after,Tokenizer())
            read=run.io.read
            def gold_blind(path):
                if Path(path).name=='evaluation_only.json':
                    raise AssertionError('Generation worker must not read benchmark labels')
                return read(path)
            with patch.dict(sys.modules,{'model':SimpleNamespace(Qwen=Model)}),patch.object(run.io,'read',side_effect=gold_blind):
                self.assertEqual(run.worker(out),0)
            self.assertEqual(len(calls),10)
            self.assertTrue(read(out/'validation/reader_functional.json')['passed'])
            self.assertTrue(read(out/'validation/functional.json')['passed'])
            self.assertTrue(read(out/'report.json')['complete'])
            for arm in run.ARMS:
                self.assertEqual(read(out/'runs/q'/arm/'result.json')['selected_option'],'0')
            self.assertNotIn(k.TEMPORAL,read(out/'runs/q/K_input.json')['prompts'][0])

            # Simulate the old parser stopping at the first arm, after planning.
            for arm in run.ARMS[1:]:
                import shutil
                shutil.rmtree(out/'runs/q'/arm)
            result = read(out/'runs/q/F_sentence/result.json')
            result.update(selected_option=None, error=dict(type='AnswerFormatError'), parser='unresolved')
            io.write(out/'runs/q/F_sentence/result.json', result)
            io.write(out/'runs/q/failure.json', dict(message='old parser rejected answer'))
            io.write(out/'queue_status.json', dict(state='failed', controller_pid=999999999))
            io.write(out/'launch.json', dict(worker_pid=999999999))
            source_hashes = {str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file()}
            resumed = Path(td)/'K_resume'
            with patch.object(run.io,'read',side_effect=gold_blind):
                # Preparation is also label-blind except its separate report subprocess.
                original_report = run.report
                with patch.object(run,'report'):
                    run.prepare_resume(out, resumed)
            policy = read(resumed/'experiment.json')
            self.assertEqual(len(policy['resume_repaired_answers']), 1)
            self.assertEqual(policy['resume_repaired_answers'][0]['model_regenerated'], False)
            self.assertEqual(source_hashes, {str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file()})
            calls.clear()
            with patch.dict(sys.modules,{'model':SimpleNamespace(Qwen=Model)}),patch.object(run.io,'read',side_effect=gold_blind):
                self.assertEqual(run.worker(resumed), 0)
            self.assertEqual(len(calls), 2)  # only the two missing final answers
            self.assertTrue(all('prefix_allowed_tokens_fn' in c for c in calls))
            summary = original_report(resumed)
            self.assertTrue(summary['complete'])
            self.assertEqual(summary['planner_calls'], 1)
            self.assertEqual(io.sha(out/'runs/q/planner_output.json'), io.sha(resumed/'runs/q/planner_output.json'))
            self.assertEqual(io.sha(out/'runs/q/F_sentence/answer_output.json'), io.sha(resumed/'runs/q/F_sentence/answer_output.json'))
            self.assertGreater(summary['prior_runtime_seconds'], 0)
            with self.assertRaisesRegex(ValueError, 'already attempted'):
                run.worker(resumed)

            # Repair builds a uniform new reader cohort: retain the plan, archive
            # every old answer, and charge old reader calls exactly once.
            strict = Path(td)/'K_strict_reader'
            with patch.object(run,'report'):
                run.prepare_strict_reader(out, strict, tokenizer=Tokenizer())
            strict_policy = read(strict/'experiment.json')
            self.assertEqual(strict_policy['reader_protocol'],run.READER_PROTOCOL)
            self.assertTrue(strict_policy['reuse_plans_only'])
            self.assertEqual(strict_policy['prior_reader_calls'],1)
            self.assertFalse(any((strict/'runs').glob('*/*/result.json')))
            self.assertTrue((strict/'resume_history'/out.name/'legacy_runs/q/F_sentence/result.json').exists())
            self.assertEqual(io.sha(strict/'runs/q/planner_output.json'),io.sha(out/'runs/q/planner_output.json'))
            calls.clear()
            with patch.dict(sys.modules,{'model':SimpleNamespace(Qwen=Model)}),patch.object(run.io,'read',side_effect=gold_blind):
                self.assertEqual(run.worker(strict),0)
            self.assertEqual(len(calls),9)  # two planner gates, four reader gates, three fresh answers
            self.assertEqual(original_report(strict)['completed'],1)
            self.assertEqual(original_report(strict)['reader_protocol'],run.READER_PROTOCOL)
            self.assertEqual(original_report(strict)['prior_reader_calls'],1)
            # A stale legacy result cannot silently enter the new primary score.
            path=strict/'runs/q/F_sentence/result.json'
            stale=read(path);stale.pop('reader_protocol');io.write(path,stale)
            with self.assertRaisesRegex(ValueError,'Mixed K reader protocols'):
                original_report(strict)

            # A later revision must clone a manifest that already contains runs,
            # keep successful calls, and archive only the explicit all-skip case.
            io.write(resumed/'cohort.json', ['q', 'bad'])
            io.write(resumed/'evaluation_only.json', dict(q='0', bad='0'))
            for region in ('pool', 'question', 'planner_input', 'F_sentence', 'K_all_evidence'):
                io.write(resumed/region/'bad.json', read(resumed/region/'q.json'))
            pool = read(resumed/'pool/bad.json')
            io.write(resumed/'runs/bad/planner_output.json', dict(raw=json.dumps({r['id']:'skip' for r in pool['records']}),
                ended_normally=True, seconds=7., input_tokens=100, output_tokens=20))
            io.write(resumed/'runs/bad/planner_output_input.json', read(resumed/'planner_input/bad.json'))
            io.write(resumed/'queue_status.json', dict(state='failed', controller_pid=999999999))
            io.write(resumed/'launch.json', dict(worker_pid=999999999))
            manifest = read(resumed/'frozen_hashes.json')
            for name in ('cohort.json', 'evaluation_only.json'):
                manifest[name] = io.sha(resumed/name)
            for region in ('pool', 'question', 'planner_input', 'F_sentence', 'K_all_evidence'):
                name = region + '/bad.json'
                manifest[name] = io.sha(resumed/name)
            io.write(resumed/'frozen_hashes.json', manifest)
            second = Path(td)/'K_second_resume'
            run.prepare_resume(resumed, second, repair_empty_plan=True)
            run.verify(second)
            policy2 = read(second/'experiment.json')
            self.assertEqual(list(policy2['resume_saved_cases']), ['q'])
            self.assertEqual(policy2['resume_replanned_cases'][0]['question_id'], 'bad')
            self.assertEqual(policy2['resume_replanned_cases'][0]['seconds'], 7.)
            self.assertFalse((second/'runs/bad').exists())
            self.assertFalse((second/'functional').exists())
            self.assertTrue(policy2['rerun_functional_gates'])
            self.assertAlmostEqual(policy2['prior_runtime_seconds'],
                policy['prior_runtime_seconds'] + read(resumed/'setup.json')['seconds'] + 7.)
            self.assertEqual(io.sha(second/'runs/q/planner_output.json'), io.sha(resumed/'runs/q/planner_output.json'))

    def test_failed_revision_costs_are_inherited_and_live_worker_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            parent,after,old=setup(Path(td));run.prepare(parent,old,tokenizer=Tokenizer())
            io.write(old/'queue_status.json',dict(state='failed'))
            io.write(old/'launch.json',dict(worker_pid=999999999))
            io.write(old/'setup.json',dict(seconds=5.))
            io.write(old/'functional/planner_output.json',dict(seconds=1.))
            io.write(old/'runs/q/planner_output.json',dict(seconds=23.))
            manifest=io.sha(old/'frozen_hashes.json')
            out=Path(td)/'K2'
            run.prepare(parent,out,tokenizer=Tokenizer(),prior=old)
            self.assertEqual(io.read(out/'report.json')['prior_runtime_seconds'],29.)
            self.assertEqual(io.sha(old/'frozen_hashes.json'),manifest)
            import os
            io.write(old/'launch.json',dict(worker_pid=os.getpid()))
            with self.assertRaisesRegex(ValueError,'still present'):
                run.prepare(parent,Path(td)/'K3',tokenizer=Tokenizer(),prior=old)

    def test_superseded_cpu_preparation_cost_is_carried_forward_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as td:
            parent,after,old=setup(Path(td));run.prepare(parent,old,after,Tokenizer())
            manifest=io.sha(old/'frozen_hashes.json')
            io.write(old/'queue_status.json',dict(state='superseded'))
            out=Path(td)/'K2'
            run.prepare(parent,out,after,Tokenizer(),prior=old)
            self.assertEqual(io.sha(old/'frozen_hashes.json'),manifest)
            self.assertEqual(io.read(out/'report.json')['prior_preparation_seconds'],
                io.read(old/'experiment.json')['cpu_preparation_seconds'])

    def test_ready_queue_uses_shared_gpu_lease_offline_vts_worker_and_completes(self):
        with tempfile.TemporaryDirectory() as td:
            parent,after,out=setup(Path(td));run.prepare(parent,out,after,Tokenizer())
            io.write(after/'queue_status.json',dict(state='complete'))
            io.write(Path(td)/'J/queue_status.json',dict(state='complete'))
            io.write(Path(td)/'J/report.json',dict(complete=True))
            @contextmanager
            def acquire(policy,waiting):
                self.assertEqual(policy['preference'],['0','1','2','3'])
                self.assertEqual(policy['stable_observations'],3)
                self.assertEqual(policy['minimum_free_mib'],32768)
                self.assertEqual(policy['maximum_utilization'],100)
                yield dict(index='2',uuid='GPU-test'),(101,102)
            def launch(command,**kwargs):
                self.assertEqual(kwargs['pass_fds'],(101,102))
                self.assertEqual(kwargs['env']['CUDA_VISIBLE_DEVICES'],'GPU-test')
                self.assertEqual(kwargs['env']['HF_HUB_OFFLINE'],'1')
                self.assertEqual(command[0],sys.executable)
                for arm in run.ARMS:
                    io.write(out/'runs/q'/arm/'result.json',dict(selected_option='0',error=None,input_tokens=30,seconds=1.,reader_protocol=run.READER_PROTOCOL))
                io.write(out/'runs/q/planner_output.json',dict(input_tokens=50,output_tokens=10,seconds=2.))
                return SimpleNamespace(pid=123,returncode=0,poll=lambda:0)
            with patch.dict(sys.modules,{'gpu_queue':SimpleNamespace(acquire=acquire)}),patch.object(run.subprocess,'Popen',side_effect=launch):
                self.assertEqual(run.queue(out),0)
            self.assertEqual(io.read(out/'queue_status.json')['state'],'complete')
            with self.assertRaisesRegex(ValueError,'already attempted'):run.queue(out)

    def test_worker_failure_marks_queue_failed_and_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as td:
            parent,after,out=setup(Path(td));run.prepare(parent,out,after,Tokenizer())
            @contextmanager
            def acquire(*args):yield dict(index='0',uuid='GPU-test'),()
            process=SimpleNamespace(pid=123,returncode=2,poll=lambda:2)
            with patch.object(run,'dependency_ready',return_value=True),patch.dict(sys.modules,{'gpu_queue':SimpleNamespace(acquire=acquire)}),patch.object(run.subprocess,'Popen',return_value=process) as launch:
                with self.assertRaisesRegex(RuntimeError,'worker failed'):run.queue(out)
                with self.assertRaisesRegex(ValueError,'already attempted'):run.queue(out)
                self.assertEqual(launch.call_count,1)
            self.assertEqual(io.read(out/'queue_status.json')['state'],'failed')

    def test_independent_k_has_no_j_dependency_and_keeps_identical_evidence_and_method(self):
        with tempfile.TemporaryDirectory() as td:
            parent,after,old=setup(Path(td));run.prepare(parent,old,after,Tokenizer())
            out=Path(td)/'K_independent'
            io.write(after/'queue_status.json',dict(state='failed'))
            run.prepare(parent,out,tokenizer=Tokenizer())
            policy=io.read(out/'experiment.json')
            self.assertIsNone(policy['after_queue'])
            self.assertIsNone(policy['after_output'])
            self.assertTrue(run.dependency_ready(policy))
            for path in ['control/evidence_planner.py','pool/q.json','question/q.json',
                    'F_sentence/q.json','K_all_evidence/q.json','planner_input/q.json']:
                self.assertEqual(io.sha(old/path),io.sha(out/path))

    def test_independent_queue_goes_directly_to_gpu_wait_without_reading_j(self):
        class GPUWait(Exception):pass
        with tempfile.TemporaryDirectory() as td:
            parent,after,out=setup(Path(td));run.prepare(parent,out,tokenizer=Tokenizer())
            io.write(after/'queue_status.json',dict(state='failed'))
            @contextmanager
            def acquire(policy,waiting):
                waiting(dict(watched_gpu_indices=['0','1','2','3']))
                self.assertEqual(io.read(out/'queue_status.json')['state'],'waiting_for_gpu')
                raise GPUWait()
                yield
            with patch.dict(sys.modules,{'gpu_queue':SimpleNamespace(acquire=acquire)}),patch.object(run.time,'sleep') as sleep,patch.object(run.subprocess,'Popen') as launch:
                with self.assertRaises(GPUWait):run.queue(out)
                sleep.assert_not_called();launch.assert_not_called()


if __name__=='__main__':unittest.main()
