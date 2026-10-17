"""Final-choice consistency, reference safety, and frozen-evidence replay guards."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from script_core.answering import reconcile_answer
from script_core.config import DEFAULTS
from script_core.controller import answer_query
from script_core.schemas import output_schema, validate_output, decoder_schema, restore_empty_arrays
from test_script_core import FixtureBackend, fixture_base


def comparison(inputs, selected='0', matches=('1',)):
    return {'evidence_summary': 'Visible activity, before interpreting options.',
            'option_alignment': {key: {'reason': 'Distinguishing activity seen' if key in matches else 'Only common steps seen',
                'claim_ids': [c['id'] for c in inputs.get('claims', [])[:1]],
                'match': 'matches' if key in matches else 'shared_only'} for key in inputs['option_ids']},
            'selected_option': selected, 'text': 'Initial interpretation',
            'claim_ids': [], 'evidence_ids': []}


class AnswerAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.inputs = {'answer_mode': 'choice', 'options': ['alternative A', 'alternative B'],
                       'option_ids': ['0', '1'], 'claims': [{'id': 'c', 'description': 'Visible activity'}],
                       'evidence': [], 'stop_decision': {'evidence_status': 'insufficient'}}

    def test_summary_and_every_option_precede_selection_in_schema(self):
        schema = output_schema('answer', self.inputs)
        self.assertEqual(list(schema['properties'])[:3], ['evidence_summary', 'option_alignment', 'selected_option'])
        value = comparison(self.inputs)
        validate_output(value, schema)
        del value['option_alignment']['1']
        with self.assertRaises(ValueError):
            validate_output(value, schema)

    def test_unique_distinction_corrects_id_and_discards_old_citations(self):
        original = deepcopy(self.inputs)
        report = comparison(self.inputs)
        report['evidence_ids'] = ['old-choice-evidence']
        result = reconcile_answer(report, self.inputs)
        self.assertEqual(result['selected_option'], '1')
        self.assertEqual(result['claim_ids'], ['c'])
        self.assertEqual(result['evidence_ids'], [])
        self.assertTrue(result['alignment_decision']['corrected'])
        self.assertEqual(report['selected_option'], '0')
        self.assertEqual(self.inputs, original)

    def test_shared_steps_unknown_or_multiple_matches_do_not_force_a_choice(self):
        for matches in ((), ('0', '1')):
            for other in ('shared_only', 'unknown', 'contradicted'):
                report = comparison(self.inputs, selected='1', matches=matches)
                for row in report['option_alignment'].values():
                    if row['match'] != 'matches':
                        row['match'] = other
                result = reconcile_answer(report, self.inputs)
                self.assertEqual(result['selected_option'], '1')
                self.assertFalse(result['alignment_decision']['corrected'])

    def test_permuted_options_have_no_fixed_answer_index(self):
        for key in ('0', '1'):
            result = reconcile_answer(comparison(self.inputs, selected='1' if key == '0' else '0', matches=(key,)), self.inputs)
            self.assertEqual(result['selected_option'], key)

    def test_unseen_comparison_citations_are_rejected(self):
        report = comparison(self.inputs)
        report['option_alignment']['1']['claim_ids'] = ['fabricated']
        with self.assertRaises(ValueError):
            validate_output(report, output_schema('answer', self.inputs))
        with self.assertRaises(ValueError):
            reconcile_answer(report, self.inputs)

    def test_actual_constrained_decoder_roundtrip_without_facts(self):
        from lmformatenforcer import JsonSchemaParser
        inputs = dict(self.inputs, claims=[])
        canonical = comparison(inputs)
        schema = output_schema('answer', inputs)
        decoded = deepcopy(canonical)
        decoded.pop('claim_ids')
        decoded.pop('evidence_ids')
        for row in decoded['option_alignment'].values():
            row.pop('claim_ids')
        parser = JsonSchemaParser(decoder_schema(schema))
        for character in json.dumps(decoded):
            self.assertIn(character, parser.get_allowed_characters())
            parser = parser.add_character(character)
        self.assertTrue(parser.can_end())
        self.assertEqual(restore_empty_arrays(decoded, schema), canonical)
        validate_output(canonical, schema)

    def test_one_answer_call_preserves_insufficient_status_and_original_budget(self):
        base, metadata = fixture_base()
        class CaptionOnlyBackend(FixtureBackend):
            answers = 0
            def call(self, operation, inputs, timeout):
                if operation == 'observe':
                    return {'data': {'claims': [], 'uncertainties': []}}
                if operation == 'answer':
                    self.answers += 1
                    return {'data': comparison(inputs)}
                return super().call(operation, inputs, timeout)
        backend = CaptionOnlyBackend(base)
        cfg = deepcopy(DEFAULTS)
        cfg['query'].update(answer_strategy='contrastive', answer_order='original')
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'What is the main goal?', 'options': ['a', 'b']}, metadata, cfg, backend, folder)
        self.assertIsNone(result['error'])
        self.assertEqual(result['answer']['selected_option'], '1')
        self.assertEqual(result['answer']['text'], 'b')
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')
        self.assertFalse(result['cost_summary']['budget_violation'])
        self.assertEqual(result['answer']['claim_ids'], [])
        self.assertEqual(backend.answers, 1)

    def test_open_answer_is_unchanged(self):
        report = {'selected_option': None, 'text': 'Unknown', 'claim_ids': [], 'evidence_ids': []}
        self.assertEqual(reconcile_answer(report, dict(self.inputs, answer_mode='open')), report)

    def test_recheck_freezes_inputs_and_keeps_labels_out_of_inference(self):
        path = Path(__file__).resolve().parents[1]/'script/recheck_answers.py'
        spec = importlib.util.spec_from_file_location('alignment_recheck', path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder)/'old', Path(folder)/'new'
            directory = source/'shards/0/script_runs/q'
            runner.write_json(directory/'query.json', {'id': 'q'})
            runner.write_json(directory/'result.json', {'error': None, 'answer': {'selected_option': '0'}})
            runner.write_json(directory/'raw.json', {'inputs': self.inputs})
            (directory/'calls.jsonl').write_text(json.dumps({'operation': 'answer', 'output_ref': 'raw.json'})+'\n')
            runner.write_json(source/'comparison_300.json', {'rows': [{'question_id': 'q', 'truth': '1'}]})
            (source/'config.yaml').write_text('schema_version: 2\n')
            frozen = runner.prepare(source, output)
            self.assertEqual(frozen['video_ids'], ['q'])
            inference = json.loads((output/'inputs/q.json').read_text())
            self.assertNotIn('truth', inference)
            self.assertEqual(inference, self.inputs)
            self.assertTrue((output/'source/script_core/answering.py').exists())
            self.assertEqual(json.loads((output/'evaluation_only.json').read_text())['rows'][0]['truth'], '1')
            manifest = json.loads((output/'input_manifest.json').read_text())
            self.assertEqual(manifest[0]['input_sha256'], runner.file_hash(output/'inputs/q.json'))
            with self.assertRaises(ValueError):
                runner.prepare(source, output)


if __name__ == '__main__':
    unittest.main()
