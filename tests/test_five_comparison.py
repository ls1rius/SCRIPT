"""Five-arm scheduling, matched evidence, failure accounting and complete coverage."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from script import run_five_comparison as run
from script import run_optimized_comparison as queue
from script_core.budget import BudgetLedger


def result(uid, error=None, seconds=2):
    ledger = BudgetLedger(100)
    ledger.actual = seconds
    return dict(question_id=uid, answer=None if error else {'selected_option': '0'},
        error=error, incomplete=[], entity_count=1, selected_seconds=[0],
        opened_scenes=['S0'], cost_summary=ledger.summary(), total_measured_seconds=seconds+1)


class FiveComparisonTests(unittest.TestCase):
    def test_rotation_keeps_every_arm_and_scheme3_precedes_scheme4(self):
        starts = set()
        for i in range(12):
            order = run.arm_order(i)
            self.assertEqual(set(order), set(run.ARMS))
            self.assertEqual(len(order), 5)
            self.assertLess(order.index('summary_deep'), order.index('temporal_cross'))
            starts.add(order[0])
        self.assertEqual(len(starts), 4)

    def test_dependency_failure_preserves_source_spending_and_denominator(self):
        prior = result('q', error={'type': 'SourceValidationError'}, seconds=7)
        got = run.dependency_failure({'question': {'question_id': 'q'}}, prior)
        self.assertTrue(got['shared_source_failed'])
        self.assertIsNone(got['answer'])
        self.assertEqual(got['cost_summary']['total'], 7)
        self.assertEqual(got['executed_online_seconds'], 0)
        got['cost_summary']['total'] = 99
        self.assertEqual(prior['cost_summary']['total'], 7)

    def test_worker_runs_all_five_despite_method_and_refill_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            ids = ['bad', 'good']
            for name, data in {'cohort.json': ids, 'config.json': {},
                'settings.json': {'directory_seconds': 600},
                'questions.json': [{'video_id': u} for u in ids],
                'captions.json': {u: [] for u in ids},
                'evaluation_only.json': {u: '0' for u in ids}}.items():
                run.write(out/name, data)
            d = {'cost_summary': {'total': 1}}
            for phase in ['smoke', 'formal']:
                for uid in ids:
                    run.write(out/phase/'directories'/uid/'directory.json', d)
            packets = {u: {'question': {'question_id': u, 'video_id': u}, 'frames': [], 'captions': []} for u in ids}
            executed, replayed = [], []
            def baseline(packet, directory, frames, backend, cfg, target, arm, settings):
                uid = packet['question']['question_id']; executed.append((target.parts[-5], arm, uid))
                r = result(uid, {'type': 'SourceValidationError'} if uid == 'bad' else None)
                if uid == 'good':
                    run.write(target/'evidence_bundle.json', dict(incomplete=[], marker='same-pre-answer-evidence', preparation_cost_summary=r['cost_summary']))
                return r
            def replay(packet, bundle, backend, cfg, target, settings):
                replayed.append(deepcopy(bundle))
                return result(packet['question']['question_id'])
            def implementation(arm):
                def call(packet, directory, frames, backend, cfg, target, settings, memory_path):
                    uid = packet['question']['question_id']; executed.append((target.parts[-5], arm, uid))
                    return result(uid, {'type': 'RelationValidationError'} if arm == 'adaptive_partial' else None)
                return call
            meter = Mock(); meter.call.return_value = ({}, {})
            with patch.object(run.original, 'verify'), patch.object(run, 'load_config', return_value={'models': {'visual_encoder': 'fake'}}), \
                 patch.object(run.subprocess, 'run'), patch('script.run_four_arm_300_local.make_packet'), \
                 patch('script.run_temporal_entity.checked_packet', side_effect=lambda root, uid, **kw: packets[uid]), \
                 patch('script_core.backend.preflight', side_effect=lambda cfg: (cfg, {})), \
                 patch('script_core.budget.ModelWorker'), patch('script_core.budget.Meter', return_value=meter), \
                 patch('script_core.temporal_pipeline.run', side_effect=baseline), \
                 patch('script_core.temporal_pipeline.run_cross_replay', side_effect=replay), \
                 patch('script_core.adaptive_transition.run', side_effect=implementation('adaptive_partial')), \
                 patch('script_core.adaptive_entity.run', side_effect=implementation('adaptive_scheme4')), \
                 patch('script_core.optimized_entity.run', side_effect=implementation('optimized_scheme4')), \
                 patch.object(run.original, 'source_link_probe', return_value={'passed': False, 'error': {'type': 'RefillError'}}), \
                 patch.object(run.optimized_runner, 'refill_probe', return_value={'passed': True}), \
                 patch.object(run.optimized_runner, 'same_evidence_ablation'):
                self.assertEqual(run.worker(out), 0)
            report = run.read(out/'report.json')
            self.assertEqual(report['planned_total_answers'], 10)
            self.assertEqual(report['common_completed'], 2)
            self.assertEqual({r['completed'] for r in report['arms'].values()}, {2})
            self.assertEqual(report['arms']['adaptive_partial']['errors'], 2)
            self.assertEqual(report['arms']['temporal_cross']['dependency_failures'], 1)
            self.assertEqual(len(replayed), 2)  # once in smoke, once in formal
            self.assertTrue(all(b['marker'] == 'same-pre-answer-evidence' for b in replayed))
            self.assertEqual(sum(arm == 'summary_deep' for _, arm, _ in executed), 4)
            gate = run.read(out/'verification/gate.json')
            self.assertTrue(gate['formal_allowed'])
            self.assertFalse(gate['all_method_checks_passed'])

    def test_report_keeps_failed_questions_and_attributes_only_preanswer_source_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            run.write(out/'cohort.json', ['a', 'b', 'pending'])
            run.write(out/'evaluation_only.json', {'a': '0', 'b': '0', 'pending': '0'})
            for arm in run.ARMS:
                for uid in ['a', 'b']:
                    r = result(uid, {'type': 'Failed'} if uid == 'b' else None)
                    if arm == 'temporal_cross':
                        r['shared_preparation_cost_summary'] = {'call_count': 1}
                    target = out/'formal/arms'/arm/'runs'/uid
                    run.write(target/'result.json', r)
                    calls = [dict(operation='caption_temporal_observe', actual_cost=1, input_tokens=10, output_tokens=1),
                             dict(operation='videotree_qa', actual_cost=1, input_tokens=90, output_tokens=9)]
                    (target/'calls.jsonl').write_text('\n'.join(__import__('json').dumps(c) for c in calls)+'\n')
            stats = run.report(out)
            self.assertEqual(stats['temporal_cross']['input_tokens'], 220)  # own 200 + pre-answer source 20
            self.assertEqual(stats['temporal_cross']['errors'], 1)
            self.assertEqual(stats['temporal_cross']['accuracy_among_attempted'], .5)
            self.assertEqual(stats['temporal_cross']['correct_over_fixed_cohort'], 1/3)
            self.assertEqual(stats['temporal_cross']['planned'], 3)

    def test_queue_excludes_gpu_zero_and_launches_five_arm_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder); (out/'source').mkdir()
            run.write(out/'resource_policy.json', dict(preferred_gpu='0', minimum_free_mib=57344, stable_observations=3))
            inventory = {'0': dict(uuid='forbidden', free_mib=97000), '2': dict(uuid='allowed', free_mib=64000)}
            proc = Mock(); proc.poll.return_value = 0; proc.returncode = 0
            with patch.object(queue.original, 'verify'), patch.object(queue.original, 'missing_assets', return_value=[]), \
                 patch.object(queue, 'inventory', return_value=inventory), patch.object(queue.time, 'sleep'), \
                 patch.object(queue.subprocess, 'Popen', return_value=proc) as launch:
                self.assertEqual(queue.queue(out, worker_script='run_five_comparison.py'), 0)
                self.assertEqual(launch.call_args.kwargs['env']['CUDA_VISIBLE_DEVICES'], 'allowed')
                self.assertTrue(launch.call_args.args[0][2].endswith('/run_five_comparison.py'))
                self.assertEqual(launch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
