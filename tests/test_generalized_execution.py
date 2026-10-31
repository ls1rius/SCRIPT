from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from script import benchmark_generalized_execution as run
from script_core import backend, context, generalized_entity_memory as core, schemas
from script_core import generalized_execution as execution


def runtime():
    return SimpleNamespace(backend=backend, context=context, schemas=schemas,
        generalized=core, prompts=SimpleNamespace(SYSTEM=backend.SYSTEM))


def request(text='C wipes the book with a cloth.', uid='R0', source='U0'):
    return dict(id=uid, unit=dict(id=source, text=text))


def empty_data():
    return dict(**{name: [] for name in core.COLLECTIONS.values()}, uncertain=False)


class Processor:
    def apply_chat_template(self, messages, **kwargs):
        self.messages = deepcopy(messages)
        return json.dumps(messages, ensure_ascii=False)


class GeneralizedExecutionTests(unittest.TestCase):
    def test_initial_and_review_prompts_match_original_backend_rendering(self):
        class Captured(Exception):
            pass
        class CapturingProcessor(Processor):
            def apply_chat_template(self, messages, **kwargs):
                super().apply_chat_template(messages, **kwargs)
                raise Captured()
        for review in [None, [dict(record=dict(kind='action', actor='C', verb='wipes'), reason='unsupported')]]:
            inputs = execution.source_inputs(request()['unit'], core, review)
            original = CapturingProcessor()
            models = backend.LocalModels(dict(models=dict(max_input_tokens=10000)))
            models.load = lambda _: (None, original)
            with patch.dict(backend.CONTRACTS, {core.OPERATION: core.PROMPT}), \
                    patch.object(schemas, 'output_schema', side_effect=lambda op, inp: core.output_schema(inp)):
                with self.assertRaises(Captured):
                    models.vlm(core.OPERATION, inputs)
            actual = execution.render_request(inputs, runtime(), Processor())
            self.assertEqual(actual['messages'], original.messages)
            self.assertEqual(actual['inverse'], {'ID0': 'U0'})

    def test_sources_keep_distinct_constraints_and_forbid_question_fields(self):
        a = execution.render_request(execution.source_inputs(request('C holds red paper')['unit'], core), runtime(), Processor())
        b = execution.render_request(execution.source_inputs(request('D holds blue cloth')['unit'], core), runtime(), Processor())
        self.assertNotIn('blue cloth', json.dumps(a['schema']))
        self.assertNotIn('red paper', json.dumps(b['schema']))
        with self.assertRaises(ValueError):
            execution.render_request(dict(execution.source_inputs(request()['unit'], core), question='secret'), runtime(), Processor())

    def test_guarded_sources_never_call_model(self):
        requests = [request('C never wipes the book', 'R0'), request('C may wipe the book', 'R1')]
        calls = []
        def invoke(phase, jobs):
            calls.append((phase, jobs))
            return [dict(data=empty_data()) for _ in jobs]
        result = execution.extract_sources(requests, 2, runtime(), invoke)
        self.assertEqual(calls, [])
        self.assertTrue(all(r.get('guarded_original') for r in result.values()))

    def test_full_role_review_is_once_and_replaces_initial_result(self):
        req = request('C wipes the book with her hand.')
        initial = empty_data()
        initial['actions'] = [dict(actor='C', verb='wipes', roles=[dict(role='instrument', value='hand')])]
        reviewed = empty_data()
        reviewed['actions'] = [dict(actor='C', verb='wipes', roles=[dict(role='body_part', value='hand')])]
        calls = []
        def invoke(phase, jobs):
            calls.append((phase, deepcopy(jobs)))
            return [dict(data=initial if phase == 'initial' else reviewed)]
        results = execution.extract_sources([req], 2, runtime(), invoke)
        self.assertEqual([p for p, _ in calls], ['initial', 'review'])
        self.assertEqual(calls[1][1][0][1]['review'][0]['reason'], 'body_part_is_not_instrument')
        self.assertTrue(results['R0']['role_review'])
        self.assertEqual(results['R0']['accepted'], core.accept(req['unit'], reviewed))

    def test_invalid_json_retains_original_and_is_not_automatically_retried(self):
        calls = []
        def invoke(phase, jobs):
            calls.append(phase)
            return [dict(data='{bad')]
        result = execution.extract_sources([request()], 1, runtime(), invoke)['R0']
        self.assertEqual(calls, ['initial'])
        self.assertEqual(result['error']['type'], 'ToolFailure')
        self.assertFalse(result['complete_simple'])

    def test_failed_batch_is_not_split_or_retried(self):
        calls = []
        def invoke(phase, jobs):
            calls.append(jobs)
            raise RuntimeError('GPU failure')
        with self.assertRaisesRegex(RuntimeError, 'GPU failure'):
            execution.extract_sources([request(uid='R0'), request(uid='R1')], 2, runtime(), invoke)
        self.assertEqual(len(calls), 1)

    def test_busy_gpu_is_allowed_when_memory_is_sufficient(self):
        policy = dict(allowed_gpu_indices=['0'], minimum_free_mib=32768)
        row = dict(index='0', utilization=100, compute_pids=['123'], free_mib=40000)
        self.assertTrue(run.allowed(row, policy))
        self.assertFalse(run.allowed(dict(row, free_mib=32000), policy))

    def test_g_loader_does_not_require_f_runtime_modules(self):
        with tempfile.TemporaryDirectory() as td:
            package = Path(td) / 'script_core'
            package.mkdir()
            (package / '__init__.py').write_text('')
            for name in ['backend', 'context', 'schemas', 'prompts', 'config', 'generalized_entity_memory']:
                (package / (name + '.py')).write_text('marker = ' + repr(name) + '\n')
            loaded = run.load_runtime(package)
            self.assertEqual(loaded.generalized_entity_memory.marker, 'generalized_entity_memory')
            self.assertFalse((package / 'lazy_script_memory.py').exists())

    def test_prepare_does_not_read_answers_or_mutate_reference(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reference, out = root / 'G', root / 'optimization'
            for name, value in {
                'cohort.json': ['q0'], 'experiment.json': dict(arm='G_generalized_entities'),
                'normalization_sources/q0.json': dict(units=[request()['unit']]),
                'materialized/q0/source_results/U0.json': execution.guarded_result(),
                'runs/G_generalized_entities/q0/result.json': dict(answer='do not read'),
                'evaluation_only.json': dict(secret='do not read'),
                'qa_config.json': {}, 'qa_expected_artifacts.json': {}}.items():
                run.write(reference / name, value)
            package = reference / 'runtime/qa/script_core'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('')
            run.write(reference / 'frozen_hashes.json', {'runtime/qa/script_core/__init__.py': run.sha(package / '__init__.py')})
            before = {str(p): run.sha(p) for p in reference.rglob('*') if p.is_file()}
            original_read = run.read
            def guarded_read(path):
                self.assertNotIn(Path(path).name, ['result.json', 'evaluation_only.json'])
                return original_read(path)
            with patch.object(run, 'read', side_effect=guarded_read):
                result = run.prepare(reference, out, 1, 1)
            self.assertEqual(result['sources'], 1)
            self.assertEqual(before, {str(p): run.sha(p) for p in reference.rglob('*') if p.is_file()})
            run.verify(out)

    def test_report_marks_valid_but_changed_facts_as_different(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            run.write(out / 'experiment.json', dict(variants=['serial', 'batch2', 'batch4'], scope='test', timing='shared', prefix_cache='deferred'))
            run.write(out / 'requests.json', [request()])
            for variant in ['serial', 'batch2', 'batch4']:
                result = execution.guarded_result()
                if variant == 'batch2':
                    result['accepted']['uncertain'] = False
                run.write(out / 'results' / variant / 'R0.json', dict(result=result, matches_captured_reference=False))
            report = run.report(out)
            self.assertFalse(report['variants']['batch2']['equivalent_on_sample'])
            self.assertEqual(report['variants']['batch2']['differing_source_ids'], ['R0'])
            self.assertTrue(report['variants']['batch4']['equivalent_on_sample'])


class RealDecoderTest(unittest.TestCase):
    def test_real_generation_uses_source_specific_decoder_with_left_padding(self):
        import torch
        from transformers import GPT2Config, GPT2LMHeadModel, BatchEncoding
        from lmformatenforcer import TokenEnforcerTokenizerData
        previous = torch.get_num_threads()
        torch.set_num_threads(1)
        self.addCleanup(torch.set_num_threads, previous)
        model = GPT2LMHeadModel(GPT2Config(vocab_size=128, n_positions=2048, n_ctx=2048,
            n_embd=16, n_layer=1, n_head=2, bos_token_id=1, eos_token_id=2, pad_token_id=0)).eval()
        class Tokenizer:
            padding_side = 'right'
            pad_token_id = 0
            eos_token_id = 2
            def decode(self, ids, **kwargs):
                return ''.join(chr(i) for i in ids if 32 <= i < 127)
        class TinyProcessor(Processor):
            tokenizer = Tokenizer()
            def __call__(self, text, **kwargs):
                self.seen_padding = self.tokenizer.padding_side
                lengths = [3 + len(t) % 4 for t in text]
                width = max(lengths)
                return BatchEncoding(dict(input_ids=torch.tensor([[0] * (width - n) + [80] * n for n in lengths]),
                    attention_mask=torch.tensor([[0] * (width - n) + [1] * n for n in lengths])))
        processor = TinyProcessor()
        enforcer = TokenEnforcerTokenizerData([(i, chr(i), False) for i in range(32, 127)],
            lambda ids: ''.join(chr(i) for i in ids if 32 <= i < 127), 2, False, 128)
        models = SimpleNamespace(load=lambda _: (model, processor), prepare_vlm=lambda: None,
            enforcer_data=enforcer, deadline=None, check_deadline=lambda: None)
        rt = runtime()
        rt.generalized = SimpleNamespace(OPERATION='g_joint_extract', PROMPT='Extract one value',
            output_schema=lambda inp: dict(type='object', properties=dict(value=dict(type='string', enum=[inp['unit']['text']])),
                required=['value'], additionalProperties=False))
        cfg = dict(models=dict(generation=dict(do_sample=False), max_input_tokens=32))
        extractor = execution.GeneralizedExtractor(cfg, rt, models=models)
        values = ['red', 'blue cloth', 'green', 'black rod']
        outputs = extractor.generate([dict(unit=dict(id=f'U{i}', text=v), source_type='caption') for i, v in enumerate(values)])
        self.assertEqual([o['data']['value'] for o in outputs], values)
        self.assertEqual(processor.seen_padding, 'left')
        self.assertEqual(processor.tokenizer.padding_side, 'right')


if __name__ == '__main__':
    unittest.main()
