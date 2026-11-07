from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import torch
from transformers import AutoTokenizer
from lmformatenforcer.integrations.transformers import build_token_enforcer_tokenizer_data
from script_core import o_sentence_filter as core
from script_core import o_batch_generation as generation
from script_core.config import digest
from script import o_batched_matrix as matrix
from script import controlled_memory_io as io
from tests.test_o_runtime_audit import Backend, RunnerAuditTests


class BatchBackend(Backend):
    def call(self, operation, request, timeout):
        if operation != generation.OPERATION: return super().call(operation, request, timeout)
        self.calls.append((operation, deepcopy(request)))
        generation.validate_packet(request)
        outputs = []
        for item in request['items']:
            row = super().call(request['operation'], item['request'], timeout)
            row.update(source_id=item['source_id'], scope=item['scope'], request_hash=digest(item['request']))
            outputs.append(row)
        return dict(items=outputs, usage=dict(input_tokens=10*len(outputs), output_tokens=10*len(outputs)))


class IsolationTests(unittest.TestCase):
    def packet(self):
        return matrix.packet_for('q1', 'source_only', core.EXTRACT,
                                 [('S0', dict(sentence='D opens a drawer.')), ('S1', dict(sentence='E reads a book.'))])

    def test_reject_foreign_question(self):
        packet = self.packet(); packet['items'][1]['scope']['question_id'] = 'q2'
        with self.assertRaisesRegex(ValueError, 'Cross-question'): generation.validate_packet(packet)

    def test_reject_foreign_condition(self):
        packet = self.packet(); packet['items'][1]['scope']['condition'] = 'question_options'
        with self.assertRaises(ValueError): generation.validate_packet(packet)

    def test_reject_duplicate_sources_and_oversized_batches(self):
        packet = self.packet(); packet['items'][1]['source_id'] = 'S0'
        with self.assertRaises(ValueError): generation.validate_packet(packet)
        packet = self.packet(); packet['items'] *= 3
        with self.assertRaises(ValueError): generation.validate_packet(packet)

    def test_reject_question_or_options_in_extraction(self):
        for key, value in [('question', 'Foreign?'), ('options', ['foreign'])]:
            packet = self.packet()
            for item in packet['items']: item['request'][key] = value
            with self.assertRaises(ValueError): generation.validate_packet(packet)

    def test_reject_different_questions_or_options_with_same_scope(self):
        for key in ['question', 'options', 'evidence_task']:
            requests = [('S0', dict(question='Q', options=['A'], candidates=[dict(id='U0')])),
                        ('S1', dict(question='Q', options=['A'], candidates=[dict(id='U0')]))]
            requests[1][1][key] = 'foreign'
            with self.assertRaises(ValueError): matrix.packet_for('q', 'question_options', core.FILTER, requests)

    def test_left_padding_keeps_individual_prompt_tokens(self):
        encoded = generation.left_pad([[1, 2], [3, 4, 5, 6]], 0, 'cpu')
        self.assertEqual(encoded['input_ids'].tolist(), [[0, 0, 1, 2], [3, 4, 5, 6]])
        self.assertEqual(encoded['attention_mask'].tolist(), [[0, 0, 1, 1], [1, 1, 1, 1]])

    def test_reject_reordered_outputs(self):
        class Meter:
            def call(self, op, packet):
                return dict(items=[dict(source_id=r['source_id'], scope=r['scope'], request_hash=digest(r['request']))
                                  for r in reversed(packet['items'])]), {}
        with self.assertRaisesRegex(ValueError, 'identity'):
            matrix.batch_call(Meter(), 'q', 'source_only', core.EXTRACT, [('S0', {'sentence':'a'}), ('S1', {'sentence':'b'})])

    def test_ledger_recovery_cost_once_and_reject_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'calls.jsonl'; p.write_text(json.dumps(dict(actual_cost=3, error=None))+'\n')
            self.assertEqual(matrix.restore_ledger(p, 100, 10).actual, 3)
            p.write_text(json.dumps(dict(actual_cost=3, error='failure'))+'\n')
            with self.assertRaises(ValueError): matrix.restore_ledger(p, 100, 10)

    def test_recovery_does_not_reuse_identical_text_at_another_source_ordinal(self):
        req = dict(sentence='D opens a drawer.')
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td); io.write(raw/'id-0.json', dict(inputs=req, output=dict(data='saved'), error=None))
            meter = SimpleNamespace(raw_directory=raw, ledger=SimpleNamespace(calls=[dict(
                operation=core.EXTRACT, input_hash=digest(req), call_id='id', attempt=0)]))
            self.assertEqual(matrix.prior_output(meter, core.EXTRACT, req, 0), dict(data='saved'))
            self.assertIsNone(matrix.prior_output(meter, core.EXTRACT, req, 1))
            with self.assertRaises(ValueError): matrix.prior_output(meter, core.EXTRACT, dict(sentence='foreign'), 0)


class TokenizerBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer = AutoTokenizer.from_pretrained('checkpoints/Qwen/Qwen2.5-VL-7B-Instruct', local_files_only=True)
        cls.enforcer = build_token_enforcer_tokenizer_data(cls.tokenizer)

    def test_real_tokenizer_independent_schema_eos_and_prompt_isolation(self):
        tokenizer = self.tokenizer
        requests = [(f'S{i}', dict(question='What is observed?', sentence=text,
                    candidates=[dict(id=f'U{i}')])) for i, text in enumerate(['ALPHA', 'BETA BETA', 'GAMMA', 'DELTA'])]
        packet = matrix.packet_for('q', 'question_only', core.FILTER, requests)
        expected = [dict(observation='x'*(i+1), relevance='', labels={f'U{i}':i%2}) for i in range(4)]
        class Model:
            device = 'cpu'; config = SimpleNamespace(max_position_embeddings=32768)
            generation_config = SimpleNamespace(eos_token_id=tokenizer.eos_token_id)
            def generate(self, input_ids, attention_mask, prefix_allowed_tokens_fn, **kwargs):
                generated = []
                for i in range(4):
                    own = tokenizer.apply_chat_template(generation.messages_for(core.FILTER, requests[i][1])[0], tokenize=True, add_generation_prompt=True)
                    self_test.assertEqual(input_ids[i][attention_mask[i].bool()].tolist(), own)
                    state = input_ids[i].clone()
                    wire = json.dumps(expected[i], separators=(',', ':'))
                    for char in wire:
                        for token in tokenizer.encode(char, add_special_tokens=False):
                            self_test.assertIn(token, prefix_allowed_tokens_fn(i, state))
                            state = torch.cat([state, torch.tensor([token])])
                    self_test.assertIn(tokenizer.eos_token_id, prefix_allowed_tokens_fn(i, state))
                    generated.append(torch.cat([state, torch.tensor([tokenizer.eos_token_id])]))
                    self_test.assertEqual(prefix_allowed_tokens_fn(i, generated[-1]), [kwargs['pad_token_id']])
                width = max(map(len, generated))
                return torch.stack([torch.cat([row, torch.full((width-len(row),), kwargs['pad_token_id'])]) for row in generated])
        self_test = self; model = Model()
        backend = SimpleNamespace(prepare_vlm=lambda:None, load=lambda name:(model, SimpleNamespace(tokenizer=tokenizer)),
            cfg=dict(models=dict(max_input_tokens=16384)), enforcer_data=self.enforcer, model_tokens={}, deadline=None, check_deadline=lambda:None)
        output = generation.generate_batch(backend, packet)
        self.assertEqual([o['data'] for o in output['items']], expected)
        self.assertTrue(all(o['ended_normally'] and not o['parse_error'] for o in output['items']))
        self.assertEqual(len({id(o['prompt']) for o in output['items']}), 4)


class MatrixTests(unittest.TestCase):
    def setup_run(self, out):
        cfg = RunnerAuditTests().setup_run(out)
        import shutil
        shutil.rmtree(out/'cases/case/selection_replay')
        sources = io.read(out/'cases/case/sentences.json')
        for i in range(1, 5): sources.append(dict(sources[0], id=f'S{i}', text=f'D opens drawer {i}.'))
        io.write(out/'cases/case/sentences.json', sources)
        policy = io.read(out/'experiment.json'); policy['independent_sentence_batch_size'] = 4
        io.write(out/'experiment.json', policy)
        return cfg

    def test_four_arms_shared_extraction_and_no_cross_condition_batches(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td); cfg = self.setup_run(out); backend = BatchBackend()
            matrix.run(out, backend, cfg)
            result = io.read(out/'runs/case/result.json')
            self.assertTrue(result['complete']); self.assertEqual(len(result['arms']), 4)
            batches = [req for op, req in backend.calls if op == generation.OPERATION]
            self.assertEqual([len(r['items']) for r in batches if r['operation'] == core.EXTRACT], [4, 1])
            self.assertTrue(all(r['scope']['question_id'] == 'case' for r in batches))
            for r in batches:
                if r['operation'] != core.EXTRACT:
                    for item in r['items']:
                        self.assertEqual('options' in item['request'], r['scope']['condition'] == 'question_options')
            for arm in result['arms']:
                self.assertIn('A. D', io.read(out/'runs/case'/arm/'final_input.json')['prompts'][0])
            before = deepcopy(backend.calls); matrix.run(out, backend, cfg)
            self.assertEqual(before, backend.calls)

    def test_resume_serial_prefix_without_regenerating_saved_extraction(self):
        from script.run_o_sentence_experiment import extract_source
        from script_core.budget import Meter, BudgetLedger
        with tempfile.TemporaryDirectory() as td:
            out = Path(td); cfg = self.setup_run(out); backend = BatchBackend()
            target = out/'runs/case/shared_extraction'; source = io.read(out/'cases/case/sentences.json')[0]
            ledger = BudgetLedger(240, answer_reserve=0, calls_path=target/'calls.jsonl')
            meter = Meter(backend, ledger, cfg, target/'raw_calls')
            extract_source(source, meter, target/'sentences/S0'); meter.close()
            inherited = ledger.actual
            matrix.run(out, backend, cfg)
            batches = [req for op, req in backend.calls if op == generation.OPERATION and req['operation'] == core.EXTRACT]
            self.assertEqual([item['source_id'] for b in batches for item in b['items']], ['S1','S2','S3','S4'])
            result = io.read(out/'runs/case/result.json')
            self.assertGreaterEqual(result['shared_extraction_seconds'], inherited)
            self.assertEqual(result['conditions']['question_only']['cost_summary']['shared_dependency_seconds'], result['shared_extraction_seconds'])

    def test_batch_failure_saved_without_retry(self):
        class Fail(BatchBackend):
            def call(self, op, request, timeout):
                if op == generation.OPERATION:
                    self.calls.append((op, request)); raise RuntimeError('injected batch failure')
                return super().call(op, request, timeout)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td); cfg = self.setup_run(out); backend = Fail()
            with self.assertRaisesRegex(Exception, 'injected batch failure'): matrix.run(out, backend, cfg)
            result = io.read(out/'runs/case/result.json')
            self.assertTrue(result['error']); self.assertFalse(result['complete'])
            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(result['shared_extraction_cost_summary']['call_count'], 1)

    def test_identical_source_ids_in_two_questions_do_not_share_context(self):
        import shutil
        with tempfile.TemporaryDirectory() as td:
            out = Path(td); cfg = self.setup_run(out); backend = BatchBackend()
            shutil.copytree(out/'cases/case', out/'cases/other')
            q = io.read(out/'cases/other/question.json'); q['question'] = 'Which person handles a book?'
            q['options'] = ['F', 'G']; io.write(out/'cases/other/question.json', q)
            policy = io.read(out/'experiment.json'); policy['case_ids'].append('other'); io.write(out/'experiment.json', policy)
            io.write(out/'evaluation_only.json', {'case':'0', 'other':'0'})
            matrix.run(out, backend, cfg)
            for op, packet in backend.calls:
                if op == generation.OPERATION and packet['operation'] != core.EXTRACT:
                    expected = q['question'] if packet['scope']['question_id'] == 'other' else 'Who opens the drawer?'
                    self.assertTrue(all(item['request']['question'] == expected for item in packet['items']))


if __name__ == '__main__': unittest.main()
