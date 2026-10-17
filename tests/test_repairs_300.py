"""Fixed-cohort, shared-M0 execution and complete-denominator reporting."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from script_core.config import digest, load_config, base_config

spec = importlib.util.spec_from_file_location('repairs_300', Path(__file__).parents[1]/'script/run_repairs_300.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
write = runner.pilot.write


class Repair300Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        data = root/'data'
        (data/'videos').mkdir(parents=True)
        self.ids = [f'v{i}' for i in range(8)]
        annotations = {uid:{'question':'Which activity is shown?', 'truth':i%2,
            **{f'option {j}':f'Choice {j}' for j in range(5)}} for i,uid in enumerate(self.ids)}
        write(data/'subset_anno.json', annotations)
        write(data/'lavila_subset.json', {uid:['caption'] for uid in self.ids})
        for uid in self.ids:
            (data/'videos'/f'{uid}.mp4').write_bytes(b'fixture')
        previous = root/'previous'
        write(previous/'cohorts.json', {'8':self.ids})
        write(previous/'pilot_spec.json', {'seed':0,'data_root':str(data), 'feature_batch_size':64,'embedding_batch_size':16})
        write(previous/'comparison_8.json', {'complete':True,'groups':{'SCRIPT':{'correct':4}},
            'rows':[{'question_id':uid,'truth':str(i%2),'SCRIPT':{'correct':i%2==0},
                     'VideoTree':{'correct':True}} for i,uid in enumerate(self.ids)]})
        self.output = root/'new'
        runner.prepare(self.output,previous,8)

    def result(self, arm, uid, prediction, error=None, violation=False):
        shard = self.ids.index(uid)%4
        runs = self.output/'arms'/arm/'shards'/str(shard)/'runs'
        write(runs/digest(uid)[:20]/'result.json', {'answer':{'selected_option':prediction} if prediction is not None else None,
            'error':error,'cost_summary':{'online':10.,'total':30.,'budget_violation':violation},'wall_seconds':11.,
            'diagnostics':{'reading':{'visual_reads':1 if arm=='script_guided' else 6,'script_guided_active':arm=='script_guided'}}})

    def test_exact_cohort_and_shared_base_configuration_are_frozen_without_predictions_in_inputs(self):
        runner.verify(self.output)
        spec = runner.pilot.read(self.output/'pilot_spec.json')
        self.assertEqual((spec['questions_per_arm'],spec['fresh_m0_videos'],spec['gpu_jobs']), (8,8,2))
        self.assertTrue(spec['shared_m0_across_arms'])
        actual = []
        for p in self.output.glob('shards/*/questions.jsonl'):
            self.assertNotIn('truth',p.read_text())
            actual += [json.loads(line)['question_id'] for line in p.read_text().splitlines()]
        self.assertEqual(set(actual),set(self.ids))
        self.assertEqual(len(actual),len(set(actual)))
        configs = [load_config(self.output/'arms'/arm/'config.yaml') for arm in runner.ARMS]
        self.assertEqual(base_config(configs[0]),base_config(configs[1]))
        self.assertEqual([c['query']['reading_policy'] for c in configs],list(runner.ARMS))
        self.assertEqual(runner.score(self.output)['groups']['script_guided']['completed'],0)

    def test_failed_queries_count_in_the_planned_denominator_and_partial_accuracy_is_not_final(self):
        self.result('coverage_first','v0','0')
        self.result('coverage_first','v1',None, {'code':'parse_failed'})
        report = runner.score(self.output)
        group = report['groups']['coverage_first']
        self.assertEqual((group['planned'],group['completed'],group['correct'],group['errors']), (8,2,1,1))
        self.assertIsNone(group['accuracy'])
        self.assertFalse(report['complete'])

    def test_paired_gains_losses_and_budget_audits_are_reported(self):
        for i,uid in enumerate(self.ids):
            base = self.output/'shards'/str(i%4)/'script_base'/digest(uid)[:20]
            write(base/'base_hash.json', {'base_hash':'fixture'})
            write(base/'build_cost.json', {'total':20.,'by_operation':{'summarize':5.}})
            (base/'summaries.jsonl').write_text(json.dumps({'status':'current'})+'\n')
            for arm in runner.ARMS:
                prediction = str(1-i%2) if (arm=='coverage_first' and i==1) or (arm=='script_guided' and i==0) else str(i%2)
                self.result(arm,uid,prediction,violation=arm=='script_guided' and i==2)
        for arm in runner.ARMS:
            for i in range(4):
                write(self.output/'arms'/arm/'shards'/str(i)/'integrity_report.json', {'passed':True})
        report = runner.score(self.output)
        self.assertTrue(report['complete'])
        self.assertEqual(report['status'],'complete_with_errors')
        self.assertEqual(report['paired_guided_vs_coverage'], {'completed_pairs':8,'gained':1,'lost':1})
        self.assertEqual(report['groups']['coverage_first']['accuracy'],7/8)
        self.assertEqual(report['groups']['script_guided']['budget_violations'],1)
        self.assertEqual(report['groups']['script_guided']['mean_visual_reads'],1.)

    def test_complete_matched_m0_is_reused_and_previous_results_are_frozen(self):
        for i, uid in enumerate(self.ids):
            directory = self.output/'shards'/str(i%4)/'script_base'
            base = directory/digest(uid)[:20]
            write(base/'base_hash.json', {'base_hash': uid})
            write(base/'build_cost.json', {'total': 20., 'by_operation': {}})
            (base/'summaries.jsonl').write_text(json.dumps({'status': 'current'})+'\n')
            index = runner.pilot.read(directory/'videos.json') if (directory/'videos.json').exists() else {}
            index[uid] = str(base.resolve())
            write(directory/'videos.json', index)
            for arm in runner.ARMS:
                self.result(arm, uid, str(i%2))
        for arm in runner.ARMS:
            for i in range(4):
                write(self.output/'arms'/arm/'shards'/str(i)/'integrity_report.json', {'passed': True})
        self.assertTrue(runner.score(self.output)['complete'])
        new = self.output.parent/'reuse'
        spec = runner.prepare(new, self.output.parent/'previous', 8, reuse_m0_from=self.output)
        self.assertEqual((spec['fresh_m0_videos'], spec['reused_m0_videos']), (0, 8))
        self.assertTrue((new/'shards/0/script_base').is_symlink())
        runner.verify(new)
        report = runner.score(new)
        self.assertFalse(report['m0_cost_remeasured'])
        self.assertEqual(report['fresh_m0']['reused'], 8)
        self.assertEqual(report['fresh_m0']['newly_built'], 0)
        self.assertEqual(report['paired_vs_previous_repair']['coverage_first']['previous_correct'], 8)
        self.assertEqual(report['paired_vs_previous_repair']['coverage_first']['completed_pairs'], 0)
        base_hash = next((self.output/'shards/0/script_base').glob('*/base_hash.json'))
        write(base_hash, {'base_hash': 'changed'})
        with self.assertRaisesRegex(ValueError, 'Reused experiment input changed'):
            runner.verify(new)

    def test_one_build_is_shared_by_both_arms_and_one_build_failure_does_not_skip_other_queries(self):
        seen = []
        def build(output,directory,spec):
            write(directory/'script_build_errors.json', {'v4':{'code':'build_failed'}})
            return {'v0':str(directory/'script_base/base')}
        def command(output,directory,phase,arguments):
            self.assertEqual([q['question_id'] for q in [json.loads(line) for line in
                Path(arguments[arguments.index('--questions')+1]).read_text().splitlines()]], ['v0'])
            seen.append((phase,arguments[arguments.index('--base')+1]))
            self.result(phase,'v0','0')
            return 1 if phase=='coverage_first' else 0
        with patch.object(runner.pilot,'build_script',side_effect=build) as builder, \
             patch.object(runner,'command',side_effect=command), \
             patch.object(runner,'audit',return_value={'passed':True,'queries':1}):
            self.assertEqual(runner.run_shard(self.output,0),1)
        self.assertEqual(builder.call_count,1)
        self.assertEqual([arm for arm,base in seen],list(runner.ARMS))
        self.assertEqual(seen[0][1],seen[1][1])
        for arm in runner.ARMS:
            self.assertEqual(runner.score(self.output)['groups'][arm]['completed'],2)

    def test_each_single_gpu_job_launches_two_pipeline_shards(self):
        with patch('torch.cuda.is_available',return_value=True),patch('torch.cuda.device_count',return_value=1), \
             patch.object(runner.subprocess,'Popen',return_value=Mock(wait=Mock(return_value=0))) as processes:
            self.assertEqual(runner.run_job(self.output,0),0)
        self.assertEqual(processes.call_count,2)
        state = runner.pilot.read(self.output/'jobs/job-0-status.json')
        self.assertEqual(state['shards'],[0,2])

    def test_changed_arm_configuration_fails_snapshot_verification(self):
        path = self.output/'arms/script_guided/config.yaml'
        path.write_text(path.read_text()+'\n# changed\n')
        with self.assertRaisesRegex(ValueError,'Frozen experiment input'):
            runner.verify(self.output)


if __name__=='__main__':
    unittest.main()
