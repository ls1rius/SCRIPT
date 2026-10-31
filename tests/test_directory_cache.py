"""An experiment reuses its source directory across phases without model calls."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from script_core.config import load_config
from script_core.directory_cache import load_or_build, identity
from script_core.temporal_entity import SETTINGS


class DirectoryCacheTests(unittest.TestCase):
    def setUp(self):
        import torch
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        feature = self.root/'features.pt'
        torch.save(torch.tensor([[1., 0.]]*8), feature)
        self.packet = dict(question=dict(video_id='v'), feature_uri=str(feature), feature_hash='verified-features',
            captions=[dict(id=f'C{i}', interval=[i, i+1], text='C opens the box.') for i in range(8)])
        self.cfg = load_config()
        self.settings = dict(SETTINGS)

    def test_second_phase_uses_the_same_directory_and_original_processing_cost(self):
        class Backend:
            calls = 0
            def call(self, op, inp, timeout):
                self.calls += 1
                return dict(data=dict(description='C opens the box.'))
        backend = Backend()
        first = load_or_build(self.root/'experiment', self.packet, self.cfg, self.settings, backend)
        calls = backend.calls
        second = load_or_build(self.root/'experiment', self.packet, self.cfg, self.settings, backend)
        self.assertGreater(calls, 0)
        self.assertEqual(backend.calls, calls)
        self.assertEqual(first['scenes'], second['scenes'])
        self.assertTrue(second['cache_reuse']['hit'])
        self.assertEqual(first['cost_summary'], second['cost_summary'])

    def test_summary_features_settings_and_model_invalidate_the_directory(self):
        original = identity(self.packet, self.cfg, self.settings)
        packet = deepcopy(self.packet)
        packet['captions'][0]['text'] = 'C closes the box.'
        self.assertNotEqual(original, identity(packet, self.cfg, self.settings))
        packet = dict(self.packet, feature_hash='changed-features')
        self.assertNotEqual(original, identity(packet, self.cfg, self.settings))
        self.assertNotEqual(original, identity(self.packet, self.cfg, dict(self.settings, penalty=2.)))
        cfg = deepcopy(self.cfg)
        cfg['models']['revisions']['vlm'] = 'changed-weights'
        self.assertNotEqual(original, identity(self.packet, cfg, self.settings))


if __name__ == '__main__':
    unittest.main()
