"""Method isolation, real call elimination, provenance and unchanged budget charging."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from script_core.budget import BudgetLedger, Meter, ResourceExhausted
from script_core.config import load_config
from script_core.persistent_cache import TextCache


class PersistentCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = load_config()
        self.cfg['models']['revisions']['vlm'] = 'verified-weight-content-hash'
        self.cfg['cache']['persistent_directory'] = str(self.root/'cache')
        self.inp = dict(captions=[dict(id='C0', interval=[0, 1], text='C opens the box.')])
        self.output = dict(data=dict(description='C opens the box.'), usage=dict(input_tokens=20, output_tokens=6))

    def cache(self, arm='adaptive_scheme4'):
        return TextCache(self.cfg, scope=arm)

    def test_same_arm_across_phases_calls_the_backend_once_and_preserves_cost(self):
        outer = self
        class Backend:
            calls = 0
            def call(self, op, inp, timeout):
                self.calls += 1
                return deepcopy(outer.output)
        backend = Backend()
        results = []
        for phase in ('smoke', 'formal'):
            ledger = BudgetLedger(100)
            meter = Meter(backend, ledger, self.cfg,
                self.root/phase/'arms/adaptive_scheme4/runs/q/raw_calls')
            try:
                result, call = meter.json('temporal_summary', self.inp, ('description',))
                results.append((result, ledger.summary(), call))
            finally:
                meter.close()
        self.assertEqual(backend.calls, 1)
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(results[1][1]['text_cache_hits'], 1)
        self.assertGreater(results[1][1]['shared_dependency_seconds'], 0.)
        self.assertEqual(results[1][2]['input_tokens'], 0)
        self.assertEqual(results[1][2]['tool_cache']['source_input_tokens'], 20)

    def test_different_arms_and_experiments_have_separate_entries(self):
        self.cache().put('temporal_summary', self.inp, self.output, 2., {'call_id': 'source'})
        self.assertIsNotNone(self.cache().get('temporal_summary', self.inp))
        self.assertIsNone(self.cache('optimized_scheme4').get('temporal_summary', self.inp))
        cfg = deepcopy(self.cfg)
        cfg['cache']['persistent_directory'] = str(self.root/'another-experiment')
        self.assertIsNone(TextCache(cfg, scope='adaptive_scheme4').get('temporal_summary', self.inp))

    def test_changed_original_text_timestamps_model_and_generation_invalidate(self):
        cache = self.cache()
        cache.put('temporal_summary', self.inp, self.output, 2., {})
        for field, value in [('text', 'C closes the box.'), ('interval', [1, 2])]:
            changed = deepcopy(self.inp)
            changed['captions'][0][field] = value
            self.assertIsNone(cache.get('temporal_summary', changed))
        for key, value in [('vlm', 'other-weight-hash'), ('@online', 'unrelated-meter-code')]:
            cfg = deepcopy(self.cfg)
            cfg['models']['revisions'][key] = value
            found = TextCache(cfg, scope='adaptive_scheme4').get('temporal_summary', self.inp)
            self.assertEqual(found is None, key != '@online')
        cfg = deepcopy(self.cfg)
        cfg['models']['generation']['max_new_tokens'] += 1
        self.assertIsNone(TextCache(cfg, scope='adaptive_scheme4').get('temporal_summary', self.inp))

    def test_corrupt_or_invalid_model_outputs_never_become_cached_evidence(self):
        cache = self.cache()
        self.assertFalse(cache.put('temporal_summary', self.inp, {'data': '{broken'}, 2., {}))
        cache.put('temporal_summary', self.inp, self.output, 2., {})
        path = cache.path(cache.key('temporal_summary', self.inp))
        entry = json.loads(path.read_text())
        entry['body']['output']['data']['description'] = 'corrupt'
        path.write_text(json.dumps(entry))
        self.assertIsNone(cache.get('temporal_summary', self.inp))

    def test_budget_cannot_be_bypassed_by_loading_a_cached_result(self):
        self.cache().put('temporal_summary', self.inp, self.output, 80., {})
        ledger = BudgetLedger(100, answer_reserve=30)
        class Backend:
            def call(self, *args):
                raise AssertionError('Cache hit must not call the model')
        meter = Meter(Backend(), ledger, self.cfg, self.root/'formal/arms/adaptive_scheme4/runs/q/raw_calls')
        try:
            with self.assertRaises(ResourceExhausted):
                meter.call('temporal_summary', self.inp)
        finally:
            meter.close()

    def test_final_answers_visual_calls_and_sampling_are_not_cached(self):
        cache = self.cache()
        for op in ('videotree_qa', 'sparse_visual_observe', 'prepare_vlm'):
            self.assertIsNone(cache.key(op, {}))
        cfg = deepcopy(self.cfg)
        cfg['models']['generation']['do_sample'] = True
        self.assertIsNone(TextCache(cfg, scope='adaptive_scheme4').key('temporal_summary', self.inp))

    def test_bookkeeping_ids_do_not_force_recomputation_but_semantic_fields_do(self):
        cache = self.cache()
        self.assertEqual(cache.key('temporal_summary', dict(self.inp, call_id='a')),
                         cache.key('temporal_summary', dict(self.inp, call_id='b')))
        self.assertNotEqual(cache.key('temporal_summary', dict(self.inp, fields=['color'])),
                            cache.key('temporal_summary', dict(self.inp, fields=['material'])))


if __name__ == '__main__':
    unittest.main()
