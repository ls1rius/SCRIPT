"""Recorded six-query parse regressions and strict-scope negative controls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_script_core import FixtureBackend, fixture_base, fixture_memory
from test_pilot_repairs import long_base
from script_core.budget import BudgetLedger, Meter
from script_core.config import DEFAULTS
from script_core.controller import answer_query, load_plan
from script_core.query_policy import normalize_plan, retain_plan_context, strict_question


CASES = json.loads((Path(__file__).parent/'fixtures/parse_role_failures_300.json').read_text())


class ParseRoleRepairTests(unittest.TestCase):
    def test_recorded_parse_and_failed_repair_outputs_load_without_asserting_facts(self):
        self.assertEqual(len(CASES), 6)
        for case in CASES:
            for field in ('plan', 'repaired_plan'):
                with self.subTest(id=case['id'], field=field):
                    original = deepcopy(case[field])
                    plan = normalize_plan(case[field], case)
                    base = fixture_memory(180.)
                    variables = load_plan(base, {'id': case['id'], 'scope': case['scope']}, plan)
                    self.assertEqual(case[field], original)
                    self.assertEqual(len(plan['requirements']), 1)
                    self.assertEqual(plan['requirements'][0]['quantifier'], 'global')
                    self.assertEqual(plan['requirements'][0]['scope'], case['scope'])
                    self.assertEqual(variables, [])
                    self.assertFalse(base.tables['bindings'])
                    self.assertFalse(base.tables['claims'])
                    self.assertTrue(all(r['status'] == 'unresolved' for r in base.tables['requirements'].values()))

    def test_six_recorded_outputs_reach_answer_in_controller_without_repair_loop(self):
        for case in CASES:
            with self.subTest(id=case['id']):
                base, metadata = long_base()
                class RecordedParser(FixtureBackend):
                    def call(self, operation, inputs, timeout):
                        if operation == 'parse':
                            self.operations.append(operation)
                            if set(inputs) != {'question', 'scope'}:
                                raise AssertionError('Parser must not receive answers or options')
                            return {'data': deepcopy(case['plan'])}
                        return super().call(operation, inputs, timeout)
                backend = RecordedParser(base)
                with tempfile.TemporaryDirectory() as folder:
                    result = answer_query(base, {'question_id': case['id'], 'video_id': 'v',
                        'question': case['question'], 'options': ['a', 'b']}, metadata,
                        deepcopy(DEFAULTS), backend, folder)
                    self.assertIsNone(result['error'])
                    self.assertIsNotNone(result['answer'])
                    self.assertEqual(backend.operations.count('parse'), 1)
                    self.assertNotIn('repair', backend.operations)
                    self.assertLessEqual(len((Path(folder)/'steps.jsonl').read_text().splitlines()), 8)

    def test_real_local_role_collision_only_adds_an_unbound_query_variable(self):
        plan = deepcopy(CASES[0]['plan'])
        plan['task_type'] = 'local'
        plan['variables'][0]['constraints'] = ['red']
        original = deepcopy(plan)
        query = {'id': 'q', 'scope': [0., 180.], 'question': 'Where is the bearing located?'}
        result = normalize_plan(plan, query)
        r = result['requirements'][0]
        self.assertEqual(r['predicate'], 'at')
        self.assertEqual(r['quantifier'], original['requirements'][0]['quantifier'])
        self.assertNotEqual(r['roles']['object'], r['roles']['location'])
        variables = {v['name']: v for v in result['variables']}
        self.assertEqual(variables[r['roles']['location']]['constraints'], [])
        self.assertEqual(variables[r['roles']['object']]['constraints'], ['red'])
        base = fixture_memory(180.)
        load_plan(base, query, result)
        self.assertFalse(base.tables['bindings'])
        self.assertFalse(base.tables['claims'])
        self.assertEqual(plan, original)

    def test_strict_count_last_absence_and_exhaustive_requirements_stay_strict(self):
        for question, quantifier in (
            ('How many times was each mango slice turned?', 'count'),
            ('Where is the last object placed?', 'last'),
            ('List each tool used throughout the video.', 'all'),
            ('What is the purpose of each tool and list all objects used?', 'all'),
            ('Describe recurring steps, and list every action while processing each mango slice.', 'all'),
            ('Did the person never open the drawer?', 'negative'),
            ('Count every opening of the drawer.', 'count'),
        ):
            with self.subTest(question=question):
                self.assertTrue(strict_question(question))
                plan = deepcopy(CASES[0]['plan'])
                plan['requirements'][0]['quantifier'] = quantifier
                result = normalize_plan(plan, {'question': question, 'scope': [0., 180.]})
                self.assertEqual(result['requirements'][0]['quantifier'], quantifier)
                self.assertEqual(result['requirements'][0]['predicate'], 'at')
                self.assertNotEqual(*result['requirements'][0]['roles'].values())

    def test_same_local_role_repair_is_reused_across_requirements(self):
        plan = deepcopy(CASES[0]['plan'])
        plan['task_type'] = 'local'
        second = deepcopy(plan['requirements'][0])
        second.update(key='second', prerequisites=[plan['requirements'][0]['key']])
        plan['requirements'].append(second)
        result = normalize_plan(plan, {'question': 'Where is the bearing located?', 'scope': [0., 180.]})
        self.assertEqual(result['requirements'][0]['roles'], result['requirements'][1]['roles'])
        self.assertEqual(result['requirements'][1]['prerequisites'], second['prerequisites'])
        self.assertEqual(len(result['variables']), 2)

    def test_local_essential_tool_question_is_not_forced_into_overview(self):
        plan = deepcopy(CASES[0]['plan'])
        plan['task_type'] = 'local'
        plan['requirements'][0]['quantifier'] = 'exists'
        result = normalize_plan(plan, {'question': 'Which essential tool does C hold in her left hand?',
                                      'scope': [0., 180.]})
        self.assertEqual(result['requirements'][0]['quantifier'], 'exists')
        self.assertEqual(result['requirements'][0]['predicate'], 'at')

    def test_structural_repair_preserves_missing_context_and_allows_explicit_reclassification(self):
        original = CASES[0]['plan']
        repaired = CASES[0]['repaired_plan']
        merged = retain_plan_context(repaired, original)
        self.assertEqual(merged['task_type'], 'temporal_overview')
        self.assertNotIn('task_type', repaired)
        self.assertEqual(retain_plan_context(dict(repaired, task_type='local'), original)['task_type'], 'local')

    def test_meter_json_repair_preserves_task_type_after_schema_failure(self):
        base, _ = fixture_base()
        class MissingField(FixtureBackend):
            def call(self, operation, inputs, timeout):
                self.operations.append(operation)
                if operation == 'parse':
                    plan = deepcopy(CASES[0]['plan'])
                    plan.pop('variables')
                    return {'data': plan}
                if operation == 'repair':
                    return {'data': deepcopy(CASES[0]['repaired_plan'])}
                raise AssertionError(operation)
        backend = MissingField(base)
        with tempfile.TemporaryDirectory() as folder:
            meter = Meter(backend, BudgetLedger(600.), deepcopy(DEFAULTS), Path(folder)/'raw')
            try:
                plan, _ = meter.json('parse', {'question': CASES[0]['question'], 'scope': [0., 180.]},
                                     ('requirements', 'variables', 'ambiguities'))
                self.assertEqual(plan['task_type'], 'temporal_overview')
                self.assertEqual(backend.operations, ['parse', 'repair'])
            finally:
                meter.close()


class ParseRecheckPreparationTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('parse_recheck_runner',
            Path(__file__).parents[1]/'script/verify_runtime_repairs.py')
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.previous, self.output = Path(folder.name)/'previous', Path(folder.name)/'recheck'
        self.previous.mkdir()
        (self.previous/'config.yaml').write_text('{}\n')
        self.runner.write(self.previous/'source_hashes.json', {'script_qa.py': 'fixture'})
        self.runner.write(self.previous/'cohorts.json', {'300': [f'q{i}' for i in range(7)]})
        self.runner.write(self.previous/'evaluation_only.json', {'questions': [
            {'question_id': f'q{i}', 'selected_option': '0'} for i in range(8)]})
        from script_core.config import digest
        for shard in range(2):
            directory = self.previous/'shards'/str(shard)
            directory.mkdir(parents=True)
            questions, bases = [], {}
            for i in range(shard, 8, 2):
                query = {'question_id': f'q{i}', 'video_id': f'v{i}', 'question': 'Question', 'options': ['a', 'b']}
                questions.append(query)
                base = directory/'M0'/f'v{i}'
                base.mkdir(parents=True)
                self.runner.write(base/'base_hash.json', {'base_hash': f'h{i}'})
                (base/'state.sqlite3').write_bytes(b'Frozen fixture M0')
                bases[f'v{i}'] = str(base)
                self.runner.write(directory/'script_runs'/digest(f'q{i}')[:20]/'result.json',
                    {'error': {'code': 'tool_failed' if i == 6 else 'parse_failed'}})
            (directory/'questions.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in questions))
            self.runner.write(directory/'stages/300/script_base/videos.json', bases)
        self.runner.prepare(self.output, self.previous, shards=2, streams_per_gpu=2,
                            cohort_size=300, failure_code='parse_failed')

    def test_recheck_selects_six_nested_base_failures_with_one_gpu_and_isolated_labels(self):
        self.runner.verify_snapshot(self.output)
        spec = self.runner.read(self.output/'verification_spec.json')
        self.assertEqual(set(spec['question_ids']), {f'q{i}' for i in range(6)})
        self.assertEqual((spec['gpu_jobs'], spec['shards'], spec['streams_per_gpu']), (1, 2, 2))
        self.assertEqual(len(self.runner.read(self.output/'evaluation_only.json')['questions']), 6)
        for path in self.output.glob('shards/*/questions.jsonl'):
            self.assertEqual(len(path.read_text().splitlines()), 3)
            self.assertNotIn('selected_option', path.read_text())
        original = Path(self.runner.read(self.output/'original_failures.json')[0]['source'])
        original.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Input changed'):
            self.runner.verify_snapshot(self.output)

    def test_single_gpu_job_runs_both_prepared_shards(self):
        from script_core.config import digest
        def launch(arguments, **kwargs):
            questions = Path(arguments[arguments.index('--questions')+1])
            out = Path(arguments[arguments.index('--out')+1])
            for line in questions.read_text().splitlines():
                q = json.loads(line)
                self.runner.write(out/digest(q['question_id'])[:20]/'result.json',
                    {'answer': {'selected_option': '0', 'text': 'a'}, 'error': None,
                     'cost_summary': {'online': 1., 'budget_violation': False}})
            return Mock(wait=Mock(return_value=0))
        with patch.object(self.runner.subprocess, 'Popen', side_effect=launch) as processes:
            self.assertEqual(self.runner.run_job(self.output, 0), 0)
            self.assertEqual(processes.call_count, 2)
        state = self.runner.read(self.output/'jobs/job-0-status.json')
        self.assertEqual(state['shards'], [0, 1])
        self.assertEqual(state['results'], 6)
        self.assertTrue(state['passed'])
        with self.assertRaisesRegex(ValueError, 'Job index'):
            self.runner.run_job(self.output, 1)

    def test_explicit_case_selection_accepts_correct_answers_and_keeps_labels_separate(self):
        from script_core.config import digest
        original = self.previous/'shards/0/script_runs'/digest('q0')[:20]/'result.json'
        self.runner.write(original, {'error': None, 'answer': {'selected_option': '0'},
                                     'cost_summary': {'online': 12.}})
        output = self.output.parent/'explicit'
        self.runner.prepare(output, self.previous, shards=2, streams_per_gpu=2,
                            cohort_size=300, question_ids=['q0', 'q1'])
        self.runner.verify_snapshot(output)
        spec = self.runner.read(output/'verification_spec.json')
        self.assertEqual(set(spec['question_ids']), {'q0', 'q1'})
        self.assertEqual(spec['selection_mode'], 'explicit_cases')
        outcomes = self.runner.read(output/'original_outcomes.json')
        self.assertEqual(next(r for r in outcomes if r['question_id'] == 'q0')['prediction'], '0')
        self.assertEqual(len(self.runner.read(output/'evaluation_only.json')['questions']), 2)
        for path in output.glob('shards/*/questions.jsonl'):
            self.assertNotIn('selected_option', path.read_text())

    def test_missing_explicit_case_is_rejected_before_freezing(self):
        output = self.output.parent/'missing'
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.runner.prepare(output, self.previous, shards=2, streams_per_gpu=2,
                                question_ids=['q0', 'absent'])
        self.assertFalse(output.exists())

    def test_script_guided_profile_is_frozen_with_three_reads_and_unchanged_model(self):
        output = self.output.parent/'guided'
        self.runner.prepare(output, self.previous, shards=2, streams_per_gpu=2,
                            question_ids=['q0', 'q1'], reading_policy='script_guided')
        from script_core.config import load_config
        cfg = load_config(output/'config.yaml')
        self.assertEqual(cfg['query']['reading_policy'], 'script_guided')
        self.assertEqual(cfg['query']['max_script_reads'], 3)
        self.assertEqual(cfg['models']['generation']['max_new_tokens'], 1024)
        self.runner.verify_snapshot(output)

    def test_fresh_build_probe_is_separate_and_prefers_unhealthy_summaries(self):
        output = self.output.parent/'probe'
        self.runner.prepare(output, self.previous, shards=2, streams_per_gpu=2,
                            question_ids=['q0', 'q1'], reading_policy='script_guided')
        bases = {}
        for p in output.glob('shards/*/base/videos.json'):
            bases.update(self.runner.read(p))
        for uid, base in bases.items():
            base = Path(base)
            self.runner.write(base/'manifest.json', {'video_id': uid, 'uri': '/fixture/video.mp4', 'cutoff': 180.,
                                                     'caption_uri': '/fixture/captions.json'})
            (base/'summaries.jsonl').write_text(json.dumps({'status': 'unreadable' if uid == 'v1' else 'current'})+'\n')
            self.runner.write(base/'build_cost.json', {'by_operation': {'summarize': 10., 'repair': 5.}})
        self.runner.prepare_build_probe(output, 1)
        self.runner.verify_snapshot(output)
        chosen = self.runner.read(output/'build_probe/spec.json')['selected']
        self.assertEqual([r['video_id'] for r in chosen], ['v1'])
        self.assertFalse((output/'build_probe/base').exists())
        self.assertEqual(len(self.runner.read(output/'build_probe/manifest.json')), 1)
        with self.assertRaises(ValueError):
            self.runner.prepare_build_probe(output, -1)

    def test_reused_original_query_finds_its_original_shard_base_index(self):
        from script_core.config import digest
        query_run = self.previous/'shards/0/script_runs'/digest('q0')[:20]
        origin = self.previous.parent/'historical/shards/7'
        target = origin/'script_runs'/query_run.name
        target.parent.mkdir(parents=True)
        query_run.rename(target)
        query_run.symlink_to(target, target_is_directory=True)
        stage = self.previous/'shards/0/stages/300/script_base/videos.json'
        index = self.runner.read(stage)
        base = index.pop('v0')
        self.runner.write(stage, index)
        self.runner.write(origin/'script_base/videos.json', {'v0': base})
        output = self.output.parent/'reused'
        self.runner.prepare(output, self.previous, shards=2, streams_per_gpu=2,
                            question_ids=['q0', 'q1'])
        self.runner.verify_snapshot(output)
        combined = {}
        for path in output.glob('shards/*/base/videos.json'):
            combined.update(self.runner.read(path))
        self.assertEqual(combined['v0'], base)

    def test_targeted_summary_counts_both_fixes_and_losses(self):
        from script_core.config import digest
        import sys
        from types import SimpleNamespace
        for i, prediction in enumerate(('1', '0')):
            path = self.previous/f'shards/{i}/script_runs'/digest(f'q{i}')[:20]/'result.json'
            self.runner.write(path, {'error': None, 'answer': {'selected_option': prediction},
                                     'cost_summary': {'online': 12.}})
        output = self.output.parent/'paired'
        self.runner.prepare(output, self.previous, shards=2, streams_per_gpu=2,
                            question_ids=['q0', 'q1'])
        for i, prediction in enumerate(('0', '1')):
            self.runner.write(output/f'shards/{i}/runs'/digest(f'q{i}')[:20]/'result.json',
                {'error': None, 'answer': {'selected_option': prediction, 'text': 'answer'},
                 'cost_summary': {'online': 15., 'budget_violation': False}})
        audit = Mock(return_value={'queries': 1, 'passed': True})
        with patch.dict(sys.modules, {'check_run_integrity': SimpleNamespace(audit=audit)}), \
             patch('script_core.evaluation.evaluate', return_value={'groups': {}}):
            report = self.runner.summarize(output)
        self.assertTrue(report['passed'])
        self.assertEqual(report['paired_cases']['fixed_wrong_answers'], 1)
        self.assertEqual(report['paired_cases']['lost_correct_answers'], 1)
        self.assertEqual(report['paired_cases']['new_correct'], 1)
        self.assertEqual(len(self.runner.read(output/'paired_cases.json')), 2)


if __name__ == '__main__':
    unittest.main()
