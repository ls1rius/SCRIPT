"""Regressions from recorded badcases: pixels, rechecks and option distinctions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_script_core import FixtureBackend, fixture_base
from script_core.config import DEFAULTS
from script_core.controller import answer_query
from script_core.coverage import midpoint_attempted, midpoint_observed
from script_core.materialize import Materializer
from script_core.query_policy import novel_claim_ids, option_distinctions
from script_core.schemas import output_schema, validate_output
from script_core.state import obj
from script_core.video import visual_quality


class EvidenceRepairTests(unittest.TestCase):
    def test_assessment_uses_compact_semantic_input_without_dropping_facts(self):
        from script_core.context import operation_inputs
        claims = [{'id': f'c{i}', 'predicate': 'other', 'fact_type': 'action', 'roles': {},
                   'interval': [i, i+1], 'description': f'Concrete action {i}', 'evidence_ids': ['e'],
                   'status': 'supported', 'support_sets': [['e']]*50,
                   'verification': {'result': 'supported', 'distinguishing_facts': ['visible'], 'process_visible': True},
                   'verification_history': [{'distinguishing_facts': ['history']*100}]*20} for i in range(24)]
        inputs = {'claims': claims, 'occurrences': [], 'bindings': [], 'requirements': [], 'options': ['a', 'b']}
        projected = operation_inputs('assess', inputs)
        self.assertEqual([c['description'] for c in projected['claims']], [c['description'] for c in claims])
        self.assertTrue(all(c['verification']['process_visible'] for c in projected['claims']))
        self.assertTrue(all('verification_history' not in c for c in projected['claims']))
        self.assertIn('verification_history', claims[0])
        self.assertLess(len(json.dumps(projected)), len(json.dumps(inputs))/10)

    def test_final_summary_waits_for_all_audits_and_reports_gains_and_losses(self):
        import importlib.util
        path = Path(__file__).parents[1]/'script/summarize_repair_run.py'
        spec = importlib.util.spec_from_file_location('repair_summary', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'new'
            root.mkdir()
            previous = Path(folder)/'old'
            previous.mkdir()
            module.write(previous/'comparison.json', {'groups': {'SCRIPT': {'final_accuracy': .5}},
                'rows': [{'question_id': 'a', 'SCRIPT': {'correct': False}},
                         {'question_id': 'b', 'SCRIPT': {'correct': True}}]})
            module.write(root/'pilot_spec.json', {'shards': 1, 'reuse_base_from': str(previous), 'cost_limitations': []})
            module.write(root/'comparison.json', {'complete': True, 'groups': {'SCRIPT': {'final_accuracy': .5}},
                'paired_accuracy_bootstrap': {}, 'rows': [{'question_id': 'a', 'SCRIPT': {'correct': True}},
                                                        {'question_id': 'b', 'SCRIPT': {'correct': False}}]})
            document = Path(folder)/'document.json'
            module.write(root/'experiment_plan.json', {'report_document': str(document)})
            self.assertFalse(module.summarize(root)['complete'])
            (root/'shards/0').mkdir(parents=True)
            module.write(root/'shards/0/integrity.json', {'phase': 'integrity', 'returncode': 0})
            self.assertFalse(module.summarize(root)['complete'])
            module.write(root/'shards/0/integrity_report.json', {'passed': True})
            (root/'source/script').mkdir(parents=True)
            (root/'source/script/analyze_query_performance.py').write_text('def analyze(root): return {"analyzed": True}\n')
            summary = module.summarize(root)
            self.assertTrue(summary['complete'])
            self.assertEqual(summary['paired_vs_previous']['fixed_wrong_answers'], 1)
            self.assertEqual(summary['paired_vs_previous']['lost_correct_answers'], 1)
            self.assertEqual(json.loads(document.read_text())['status'], 'complete')
            module.write(root/'shards/0/integrity_report.json', {'passed': False})
            self.assertEqual(module.summarize(root)['status'], 'complete_integrity_failed')

    def materializer_inputs(self, folder):
        base, _ = fixture_base()
        evidence = base.get('ev')
        frames = [f['frame_id'] for f in evidence['frame_inputs'][:4]]
        task = {'id': 'task', 'scene_ids': ['s0'], 'extraction_scope': {'predicates': ['other']}}
        candidate = {'predicate': 'other', 'roles': {}, 'interval': [0., 6.],
                     'source_frame_ids': frames, 'description': 'The object moves across the table.',
                     'fact_type': 'action'}
        report = {'result': 'supported', 'distinguishing_facts': ['Object moves across the table'],
                  'unresolved_conflicts': [], 'source_frame_ids': frames, 'process_visible': True}
        return base, Materializer(base, Mock(), deepcopy(DEFAULTS), 'q', folder), task, candidate, evidence, report

    def test_recheck_can_upgrade_unknown_and_conflict_revokes_downstream_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            base, m, task, candidate, evidence, report = self.materializer_inputs(folder)
            first = m.verify_and_add_claim(task, candidate, evidence, [], dict(report, result='unresolved'))
            upgraded = m.verify_and_add_claim(task, candidate, evidence, [], report)
            self.assertEqual(first['id'], upgraded['id'])
            self.assertEqual(upgraded['status'], 'supported')
            base.put('derived_claims', obj('dependent', node_type='query_fact', content={},
                                         support_sets=[[upgraded['id']]], status='supported'))
            conflict = m.verify_and_add_claim(task, candidate, evidence, [], dict(report, result='refuted'))
            self.assertEqual(conflict['status'], 'unresolved')
            self.assertFalse(base.is_valid('dependent'))
            self.assertEqual(base.get('dependent')['status'], 'invalidated')
            self.assertEqual(len(conflict['verification_history']), 3)
            repeated = m.verify_and_add_claim(task, candidate, evidence, [], report)
            self.assertEqual(repeated['status'], 'unresolved')
            base.validate()

    def test_later_unknown_does_not_silently_preserve_a_supported_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            base, m, task, candidate, evidence, report = self.materializer_inputs(folder)
            first = m.verify_and_add_claim(task, candidate, evidence, [], report)
            changed = m.verify_and_add_claim(task, candidate, evidence, [], dict(report, result='unresolved'))
            self.assertEqual(first['id'], changed['id'])
            self.assertFalse(base.is_valid(changed['id']))
            base.validate()

    def test_other_actions_require_a_process_but_static_attributes_do_not(self):
        for fact_type, expected in [('action', 'unresolved'), ('state', 'supported'), ('inference', 'unresolved'), (None, 'unresolved')]:
            with self.subTest(fact_type=fact_type), tempfile.TemporaryDirectory() as folder:
                base, m, task, candidate, evidence, report = self.materializer_inputs(folder)
                if fact_type is None:
                    candidate.pop('fact_type')
                else:
                    candidate['fact_type'] = fact_type
                if fact_type == 'state':
                    candidate['description'] = 'The object is blue.'
                claim = m.verify_and_add_claim(task, candidate, evidence, [], dict(report, process_visible=False))
                self.assertEqual(claim['status'], expected)
                base.validate()

    def test_action_check_cannot_claim_process_from_a_single_verifier_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            base, m, task, candidate, evidence, report = self.materializer_inputs(folder)
            report['source_frame_ids'] = report['source_frame_ids'][:1]
            claim = m.verify_and_add_claim(task, candidate, evidence, [], report)
            self.assertEqual(claim['status'], 'unresolved')

    def test_action_decoder_requires_process_sources_before_generation(self):
        from lmformatenforcer import JsonSchemaParser
        inputs = {'frames': [{'frame_id': f'f{i}', 'pts': i*.5} for i in range(4)], 'interval': [0., 2.]}
        schema = output_schema('observe', inputs)
        item = {'fact_type': 'action', 'predicate': 'other', 'roles': {}, 'interval': [0., 2.],
                'observed_at': None, 'source_type': 'visual', 'source_frame_ids': ['f0'], 'description': 'Moves an object'}
        with self.assertRaises(ValueError):
            validate_output({'claims': [item], 'uncertainties': []}, schema)
        action = next(v for v in schema['properties']['claims']['items']['anyOf']
                      if v['properties']['fact_type']['enum'] == ['action'])
        self.assertEqual(next(iter(action['properties'])), 'fact_type')
        parser = JsonSchemaParser(action['properties']['source_frame_ids'])
        for character in '["f0"':
            parser = parser.add_character(character)
        self.assertNotIn(']', parser.get_allowed_characters())
        item['source_frame_ids'] = ['f0', 'f1', 'f2']
        validate_output({'claims': [item], 'uncertainties': []}, schema)
        parser = JsonSchemaParser(schema)
        for character in json.dumps({'claims': [item], 'uncertainties': []}):
            self.assertIn(character, parser.get_allowed_characters())
            parser = parser.add_character(character)
        self.assertTrue(parser.can_end())

    def test_black_pixel_detection_does_not_reject_static_visible_images(self):
        from PIL import Image
        self.assertFalse(visual_quality(Image.new('RGB', (32, 32), (0, 0, 0)))['visual_usable'])
        self.assertTrue(visual_quality(Image.new('RGB', (32, 32), (40, 40, 40)))['visual_usable'])

    def test_black_checkpoint_is_attempted_but_not_observed(self):
        evidence = {'interval': [0., 6.], 'frame_inputs': [{'pts': 3., 'visual_usable': False}]}
        self.assertTrue(midpoint_attempted(evidence, 3., DEFAULTS))
        self.assertFalse(midpoint_observed(evidence, 3., DEFAULTS))

    def test_black_only_inputs_do_not_call_observer_or_produce_supported_facts(self):
        base, meta = fixture_base()
        class BlackBackend(FixtureBackend):
            def call(self, operation, inputs, timeout):
                output = super().call(operation, inputs, timeout)
                if operation == 'decode':
                    output['data'] = [dict(f, visual_usable=False) for f in output['data']]
                return output
        backend = BlackBackend(base)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'What is the overall activity?', 'options': ['art', 'study']},
                meta, deepcopy(DEFAULTS), backend, folder)
            records = [json.loads(line) for line in (Path(folder)/'build_records.jsonl').read_text().splitlines()]
        self.assertIsNone(result['error'])
        self.assertNotEqual(result['stop_decision']['evidence_status'], 'sufficient')
        self.assertNotIn('observe', backend.operations)
        self.assertNotIn('verify_claims', backend.operations)
        self.assertTrue(records)
        self.assertTrue(all(r['failure_reason'] == 'no_usable_visual_frames' for r in records))

    def test_black_source_ids_cannot_authorize_an_action(self):
        with tempfile.TemporaryDirectory() as folder:
            base, m, task, candidate, evidence, report = self.materializer_inputs(folder)
            evidence = base.update('evidence', evidence['id'],
                frame_inputs=[dict(f, visual_usable=False) for f in evidence['frame_inputs']])
            claim = m.verify_and_add_claim(task, candidate, evidence, [], report)
            self.assertEqual(claim['status'], 'unresolved')

    def test_option_difference_keeps_extra_event_after_long_shared_prefix(self):
        common = 'He finished fixing the wood to the shelf, put away the tools and picked up his jacket'
        options = [common+'.', common+', and then went to the bathroom.', common+', and then made a sandwich.']
        rows = option_distinctions(options)
        self.assertTrue(rows[0]['shared_steps_only'])
        self.assertIn('bathroom', rows[1]['difference_text'])
        self.assertIn('sandwich', rows[2]['difference_text'])
        self.assertNotIn('shelf', rows[1]['difference_text'])

    def test_gate_decoder_requires_every_option_without_generated_ids(self):
        from lmformatenforcer import JsonSchemaParser
        schema = output_schema('gate', {'options': ['art', 'study'], 'option_ids': ['0', '1'], 'claims': [{'id': 'c'}]})
        check = {'reason': 'Concrete visible distinction', 'claim_ids': ['c'],
                 'basis': 'direct_support', 'result': 'supported'}
        report = {'option_checks': {'0': check, '1': dict(check, basis='direct_contradiction', result='refuted')},
                  'selected_option': '0', 'passed': True, 'unresolved': []}
        validate_output(report, schema)
        parser = JsonSchemaParser(schema)
        for character in json.dumps(report):
            self.assertIn(character, parser.get_allowed_characters())
            parser = parser.add_character(character)
        self.assertTrue(parser.can_end())
        del report['option_checks']['1']
        with self.assertRaisesRegex(ValueError, 'missing fields'):
            validate_output(report, schema)

    def test_answer_text_is_canonical_and_explanation_is_preserved(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('run_integrity', Path(__file__).parents[1]/'script/check_run_integrity.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        base, meta = fixture_base()
        backend = FixtureBackend(base)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'first', 'options': ['Canonical art option', 'Canonical study option']},
                meta, deepcopy(DEFAULTS), backend, folder)
            self.assertFalse(module.audit(Path(folder)/'does-not-exist')['passed'])
            # The auditor consumes a collection of query directories.
            runs = Path(folder)/'audit_collection'
            runs.mkdir()
            (runs/'query').symlink_to(Path(folder), target_is_directory=True)
            self.assertTrue(module.audit(runs)['passed'])
            saved = json.loads((Path(folder)/'result.json').read_text())
            saved['answer']['text'] = 'Contradictory option text'
            (Path(folder)/'result.json').write_text(json.dumps(saved))
            self.assertFalse(module.audit(runs)['passed'])
        self.assertIsNone(result['error'])
        self.assertEqual(result['answer']['text'], 'Canonical art option')
        self.assertEqual(result['answer']['explanation'], 'Fixture answer')

    def test_same_description_at_another_time_is_not_new_discriminating_information(self):
        with tempfile.TemporaryDirectory() as folder:
            base, m, task, candidate, evidence, report = self.materializer_inputs(folder)
            original = m.verify_and_add_claim(task, candidate, evidence, [], report)
            copy = obj('copy', **{k: deepcopy(v) for k, v in original.items() if k not in ('id', 'schema_version', 'revision')})
            base.put('claims', copy)
            self.assertEqual(novel_claim_ids(base, [copy['id']]), [])


if __name__ == '__main__':
    unittest.main()
