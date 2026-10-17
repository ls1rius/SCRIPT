"""Nested SCRIPT scaling keeps the fixed 100 and executes each added question once."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('script_scaling', Path(__file__).resolve().parents[1]/'script/run_script_scaling.py')
scaling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scaling)
pilot = scaling.pilot


class ScriptScalingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        data = self.root/'data'
        (data/'videos').mkdir(parents=True)
        annotations = {f'v{i:03d}': {'question': f'Question {i}', 'truth': i%5,
            **{f'option {j}': f'Choice {j}' for j in range(5)}} for i in range(500)}
        annotations['0074f737-11cb-497d-8d07-77c3a8127391'] = annotations.pop('v499')
        pilot.write(data/'subset_anno.json', annotations)
        pilot.write(data/'lavila_subset.json', {uid: ['caption'] for uid in annotations})
        for uid in annotations:
            (data/'videos'/f'{uid}.mp4').write_bytes(b'fixture')
        self.previous = self.root/'previous'
        old = pilot.prepare(self.previous, count=100, shards=4, data_root=data,
                            feature_batch_size=64, embedding_batch_size=16, streams_per_gpu=2)
        for path in self.previous.glob('shards/*/questions.jsonl'):
            for line in path.read_text().splitlines():
                self.result(path.parent/'script_runs', json.loads(line))
        from run_videotree_scaling import nested_cohorts
        self.cohorts = nested_cohorts(list(annotations), old['video_ids'])
        self.baseline = self.root/'baseline'
        pilot.write(self.baseline/'cohorts.json', self.cohorts)
        pilot.write(self.baseline/'scaling_spec.json', {'model': old['model']})
        for uid in self.cohorts['500']:
            pilot.write(self.baseline/'videotree_runs'/uid/'summary.json',
                        {'seconds': 1., 'examples': [{'answer': 'A', 'selected_frames': 1}]})
        self.output = self.root/'scale'
        scaling.prepare(self.output, self.previous, self.baseline)

    @staticmethod
    def result(runs, query):
        from script_qa import safe_directory_id
        directory = runs/safe_directory_id(query['question_id'])
        pilot.write(directory/'query.json', dict(query, id=query['question_id'], answer_mode='choice'))
        pilot.write(directory/'result.json', {'answer': {'selected_option': '0', 'text': query['options'][0]},
                    'error': None, 'cost_summary': {}, 'wall_seconds': 2., 'diagnostics': {}})

    def test_preparation_reuses_exact_100_and_preserves_nested_groups(self):
        scaling.verify(self.output)
        summary = pilot.read(self.output/'run_summary.json')
        self.assertEqual(summary['groups']['100']['groups']['SCRIPT']['completed_questions'], 100)
        self.assertEqual(summary['groups']['300']['groups']['SCRIPT']['completed_questions'], 100)
        self.assertIsNone(summary['groups']['300']['groups']['SCRIPT']['final_accuracy'])
        self.assertEqual(summary['groups']['500']['groups']['VideoTree']['completed_questions'], 500)
        self.assertEqual(len(list(self.output.glob('shards/*/script_runs/*/result.json'))), 100)
        self.assertEqual(pilot.read(self.output/'pilot_spec.json')['gpu_jobs'], 2)
        self.assertEqual(pilot.read(self.output/'pilot_spec.json')['included_debug_video_ids'],
                         ['0074f737-11cb-497d-8d07-77c3a8127391'])
        source = self.output/'shards/0/questions.jsonl'
        source.write_text('{}\n')
        with self.assertRaisesRegex(ValueError, 'Frozen scaling input'):
            scaling.verify(self.output)

    def test_worker_builds_and_queries_only_new_questions_in_two_stages(self):
        seen = []
        def build(output, directory, spec):
            ids = [r['video_id'] for r in pilot.read(directory/'manifest.json')]
            self.assertEqual(len(ids), 50)
            self.assertFalse(set(ids) & set(self.cohorts['100']))
            pilot.write(directory/'script_build_errors.json', {})
            return {uid: str(directory/uid) for uid in ids}
        def command(directory, phase, arguments):
            path = Path(arguments[arguments.index('--questions')+1])
            runs = Path(arguments[arguments.index('--out')+1])
            for line in path.read_text().splitlines():
                query = json.loads(line)
                seen.append(query['question_id'])
                self.result(runs, query)
            return 0
        with patch('torch.cuda.is_available', return_value=True), patch('torch.cuda.device_count', return_value=1), \
             patch.object(pilot, 'build_script', side_effect=build), patch.object(pilot, 'command', side_effect=command):
            self.assertEqual(scaling.run_shard(self.output, 0), 0)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 100)
        self.assertTrue(set(seen[:50]) <= set(self.cohorts['300']))
        self.assertFalse(set(seen[50:]) & set(self.cohorts['300']))
        report = pilot.read(self.output/'shards/0/integrity_report.json')
        self.assertEqual(report['queries'], 125)
        self.assertTrue(report['passed'])

    def test_complete_requires_all_audits_and_keeps_baseline_failure_in_denominator(self):
        for path in self.output.glob('shards/*/questions.jsonl'):
            for line in path.read_text().splitlines():
                query = json.loads(line)
                if query['question_id'] not in self.cohorts['100']:
                    self.result(path.parent/'script_runs', query)
        uid = self.cohorts['300'][100]
        directory = self.baseline/'videotree_runs'/uid
        (directory/'summary.json').unlink()
        pilot.write(directory/'failure.json', {'error': 'Recorded baseline failure'})
        self.assertEqual(scaling.score(self.output)['status'], 'running')
        for i in range(4):
            pilot.write(self.output/'shards'/str(i)/'integrity_report.json', {'passed': True, 'queries': 125})
        summary = scaling.score(self.output)
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual(summary['groups']['300']['groups']['VideoTree']['errors'], 1)
        self.assertEqual(summary['groups']['500']['groups']['SCRIPT']['completed_questions'], 500)
        pilot.write(self.output/'shards/0/integrity_report.json', {'passed': False})
        self.assertEqual(scaling.score(self.output)['status'], 'complete_integrity_failed')


if __name__ == '__main__':
    unittest.main()
