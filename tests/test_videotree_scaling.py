"""Nested cohort identity, isolated labels, and complete failure accounting."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('scaling', Path(__file__).resolve().parents[1]/'script/run_videotree_scaling.py')
scaling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scaling)


class CohortTests(unittest.TestCase):
    def test_nested_order_is_fixed_without_resampling_the_original_set(self):
        first = scaling.nested_cohorts(list('abcdef'), ['c', 'a'], seed=0, sizes=(2, 4, 6))
        self.assertEqual(first['2'], ['c', 'a'])
        self.assertEqual(first['4'][:2], first['2'])
        self.assertEqual(first['6'][:4], first['4'])
        self.assertEqual(set(first['6']), set('abcdef'))
        self.assertEqual(first, scaling.nested_cohorts(list('fedcba'), ['c', 'a'], seed=0, sizes=(2, 4, 6)))
        with self.assertRaises(ValueError):
            scaling.nested_cohorts(list('aabbcc'), ['a', 'b'], sizes=(2, 4, 6))


class PreparedScalingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        data, old = self.root/'data', self.root/'old'
        (data/'videos').mkdir(parents=True)
        self.annotations = {f'v{i:03d}': {'question': f'Question {i}', 'truth': i%5,
            **{f'option {j}': f'Choice {j}' for j in range(5)}} for i in range(500)}
        scaling.write(data/'subset_anno.json', self.annotations)
        scaling.write(data/'lavila_subset.json', {uid: ['description'] for uid in self.annotations})
        scaling.write(data/'duration.json', {uid: 1 for uid in self.annotations})
        for uid in self.annotations:
            (data/'videos'/f'{uid}.mp4').write_bytes(b'fixture')
        scaling.write(old/'pilot_spec.json', {'video_ids': list(self.annotations)[:100],
            'shared_caption_sha256': scaling.sha(data/'lavila_subset.json'), 'model': 'fixture-model',
            'feature_model': 'fixture-feature-model', 'feature_batch_size': 64,
            'shared_generation_max_new_tokens': 1024, 'excluded_debug_video_ids': ['v498', 'v499']})
        scaling.write(old/'source_hashes.json', {name: scaling.sha(scaling.ROOT/name) for name in scaling.BASELINE_FILES})
        for uid in list(self.annotations)[:100]:
            row = self.annotations[uid]
            scaling.write(old/'shards/0/videotree_runs'/uid/'summary.json', {'model': 'fixture-model', 'seconds': 1.5,
                'examples': [{'video_id': uid, 'question': row['question'],
                    'options': {chr(65+i): row[f'option {i}'] for i in range(5)}, 'answer': 'A', 'selected_frames': 1}]})
        self.output = self.root/'experiment'
        scaling.prepare(self.output, old, data_root=data)

    def test_all_500_include_debug_ids_and_labels_stay_out_of_inference(self):
        cohorts = scaling.read(self.output/'cohorts.json')
        self.assertEqual([len(cohorts[str(n)]) for n in (100, 300, 500)], [100, 300, 500])
        self.assertTrue({'v498', 'v499'} <= set(cohorts['500']))
        inputs = (self.output/'videotree_input.json').read_text()+(self.output/'questions.jsonl').read_text()
        self.assertNotIn('truth', inputs)
        self.assertNotIn('selected_option', inputs)
        assigned = [uid for p in self.output.glob('shards/*/video_ids.json') for uid in scaling.read(p)]
        self.assertEqual(len(assigned), 400)
        self.assertEqual(set(assigned), set(cohorts['500'])-set(cohorts['100']))
        scaling.verify(self.output)
        report = scaling.score(self.output)
        self.assertEqual(report['groups']['100']['correct'], 20)
        self.assertIsNone(report['groups']['300']['final_accuracy'])
        self.assertIsNone(report['groups']['500']['final_accuracy'])

    def test_errors_count_wrong_and_override_stale_success(self):
        cohorts = scaling.read(self.output/'cohorts.json')
        for uid in cohorts['500'][100:]:
            scaling.write(self.output/'videotree_runs'/uid/'summary.json', {'seconds': 2.,
                'examples': [{'video_id': uid, 'answer': chr(65+self.annotations[uid]['truth']), 'selected_frames': 1}]})
        scaling.write(self.output/'videotree_runs'/cohorts['300'][100]/'failure.json', {'error': 'fixture failure'})
        report = scaling.score(self.output)
        self.assertTrue(report['complete'])
        self.assertEqual(report['groups']['100']['correct'], 20)
        self.assertEqual(report['groups']['300']['correct'], 219)
        self.assertEqual(report['groups']['500']['correct'], 419)
        self.assertEqual(report['groups']['500']['errors'], 1)
        self.assertEqual(report['groups']['500']['final_accuracy'], 419/500)

    def test_frozen_cohort_and_reused_results_are_verified(self):
        path = self.output/'questions.jsonl'
        original = path.read_bytes()
        path.write_text('{}\n')
        with self.assertRaisesRegex(ValueError, 'cohort input'):
            scaling.verify(self.output)
        path.write_bytes(original)
        original_summary = next((self.root/'old').glob('shards/*/videotree_runs/*/summary.json'))
        original_summary.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'data/result'):
            scaling.verify(self.output)

    def test_one_bad_video_does_not_fail_other_questions_in_its_stage(self):
        directory = self.output/'shards/0'
        ids = scaling.read(directory/'video_ids.json')[:2]
        scaling.write(directory/'video_ids.json', ids)
        hashes = scaling.read(self.output/'input_hashes.json')
        hashes['shards/0/video_ids.json'] = scaling.sha(directory/'video_ids.json')
        scaling.write(self.output/'input_hashes.json', hashes)
        def pipeline(args, data, out, examples, captions):
            self.assertTrue(all('truth' not in row for row in examples.values()))
            scaling.write(out/'summary.json', {'seconds': 2.,
                'examples': [{'video_id': next(iter(examples)), 'answer': 'A', 'selected_frames': 1}]})
        with patch('torch.cuda.is_available', return_value=True), patch('torch.cuda.device_count', return_value=1), \
             patch('data_extraction.extract_images.extract_video', side_effect=[RuntimeError('bad video'), {}]), \
             patch('demo.run_pipeline', side_effect=pipeline), patch.object(scaling, 'stage_command', return_value=0):
            returncode = scaling.run_shard(self.output, 0)
        self.assertEqual(returncode, 1)
        self.assertTrue((self.output/'videotree_runs'/ids[0]/'failure.json').exists())
        self.assertTrue((self.output/'videotree_runs'/ids[1]/'summary.json').exists())
        report = scaling.score(self.output)
        self.assertEqual(report['groups']['500']['completed'], 102)
        self.assertEqual(report['groups']['500']['errors'], 1)


if __name__ == '__main__':
    unittest.main()
