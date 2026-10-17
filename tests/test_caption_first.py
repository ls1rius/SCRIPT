"""Caption-first decisions: source preservation, targeted checks and honest provenance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from script_core.caption_first import timeline, normalize_review
from script_core.config import DEFAULTS
from script_core.controller import answer_query
from script_core.schemas import output_schema, validate_output, decoder_schema, restore_empty_arrays
from script_core.video import file_hash
from test_pilot_repairs import long_base
from test_script_core import FixtureBackend


class CaptionBackend(FixtureBackend):
    def __init__(self, memory, reads=0, fail=False):
        super().__init__(memory)
        self.reads, self.fail, self.seen = reads, fail, {}

    def call(self, operation, inputs, timeout):
        self.seen.setdefault(operation, []).append(deepcopy(inputs))
        if operation == 'caption_review':
            from script_core.budget import ToolFailure
            self.operations.append(operation)
            if self.fail:
                raise ToolFailure('Caption review unavailable')
            captions = inputs['caption_timeline']
            windows = inputs['candidate_windows']
            checks = []
            if len(inputs['inspected_windows']) < self.reads and windows:
                w = max(windows, key=lambda w: w['interval'][0])
                anchor = next(c for c in captions if c['interval'][0] < w['interval'][1]
                              and w['interval'][0] < c['interval'][1])
                checks = [{'window_id': w['id'], 'question': 'What is the shape of the tool tip?',
                           'caption_ids': [anchor['id']], 'reason': 'Tool shape separates the two actions'}]
            return {'data': {'text': 'The descriptions show drawing before stitching.',
                'selected_option': str(inputs['options'].index('Draw a pattern')),
                'caption_ids': [captions[-1]['id']], 'checks': checks}}
        result = super().call(operation, inputs, timeout)
        if operation == 'answer':
            result['data']['selected_option'] = str(inputs['options'].index('Cut the fabric'))
        return result


class CaptionFirstTests(unittest.TestCase):
    def run_query(self, reads=0, fail=False):
        base, metadata = long_base()
        cfg = deepcopy(DEFAULTS)
        cfg['query']['reading_policy'] = 'script_guided'
        backend = CaptionBackend(base, reads, fail)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'What is the overall process?', 'options': ['Cut the fabric', 'Draw a pattern']},
                metadata, cfg, backend, folder)
            calls = [json.loads(x) for x in (Path(folder)/'calls.jsonl').read_text().splitlines()]
        self.assertIsNone(result['error'], result['error'])
        return result, backend, calls

    def test_descriptions_alone_answer_without_video_or_a_second_guess(self):
        result, backend, calls = self.run_query()
        self.assertEqual(result['answer']['selected_option'], '1')
        self.assertEqual(result['answer']['text'], 'Draw a pattern')
        self.assertNotIn('observe', backend.operations)
        self.assertNotIn('decode', backend.operations)
        self.assertNotIn('answer', backend.operations)
        self.assertNotIn('read_plan', backend.operations)
        self.assertEqual(sum(c['operation'] == 'caption_review' for c in calls), 1)
        self.assertEqual(result['answer']['claim_ids'], [])
        self.assertEqual(result['answer']['evidence_ids'], [])
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')
        reading = result['diagnostics']['reading']
        self.assertEqual(reading['answer_source'], 'caption_review')
        self.assertFalse(reading['caption_answer_comparison']['changed'])
        self.assertFalse(result['cost_summary']['budget_violation'])

    def test_targeted_check_keeps_question_and_records_changed_answer(self):
        result, backend, calls = self.run_query(1)
        self.assertEqual(backend.operations.count('caption_review'), 2)
        self.assertEqual(backend.operations.count('observe'), 1)
        self.assertEqual(backend.operations.count('answer'), 1)
        observed = backend.seen['observe'][0]
        self.assertIn('What is the shape of the tool tip?', observed['goal'])
        self.assertNotIn('Draw a pattern', observed['goal'])
        self.assertTrue(any('tool tip' in s for s in observed['acceptance_checks']))
        final = backend.seen['answer'][0]
        self.assertIn('caption_timeline', final)
        self.assertNotIn('coarse_context', final)
        self.assertNotIn('script_outline', final)
        self.assertNotIn('caption_initial_answer', final)
        self.assertNotIn('caption_reviews', final)
        self.assertTrue(final['claims'])
        first, second = backend.seen['caption_review']
        self.assertEqual(first['verified_claims'], [])
        self.assertTrue(second['verified_claims'])
        inspected = {w['window_id'] for w in second['inspected_windows']}
        self.assertFalse(inspected & {w['id'] for w in second['candidate_windows']})
        self.assertEqual(result['diagnostics']['reading']['caption_answer_comparison'],
                         {'initial_option': '1', 'final_option': '0', 'changed': True})

    def test_repeated_checks_still_obey_three_read_cap(self):
        result, backend, calls = self.run_query(10)
        self.assertEqual(backend.operations.count('observe'), 3)
        self.assertEqual(backend.operations.count('caption_review'), 3)
        self.assertEqual(result['stop_decision']['exploration_limit'], 'script_read_limit')
        self.assertFalse(result['cost_summary']['budget_violation'])

    def test_review_failure_still_answers_from_original_timeline(self):
        result, backend, calls = self.run_query(fail=True)
        self.assertNotIn('observe', backend.operations)
        self.assertIn('caption_timeline', backend.seen['answer'][0])
        self.assertTrue(result['diagnostics']['reading']['planning_error'])
        self.assertEqual(result['answer']['selected_option'], '0')

    def test_raw_seconds_survive_repeated_text_without_window_overlap(self):
        base, _ = long_base()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'captions.json'
            spans = [{'start': s, 'end': s+1, 'text': 'Same motion' if s < 179 else 'A rare pen appears'} for s in range(180)]
            path.write_text(json.dumps({'spans': spans, 'producer': 'fixture'}))
            base.manifest.update(caption_uri=str(path), caption_hash=file_hash(path))
            rows = timeline(base, {'scope': [0., 180.]})
            self.assertEqual(len(rows), 180)
            self.assertEqual([r['text'] for r in rows], [s['text'] for s in spans])
            self.assertEqual(rows[-1]['interval'], [179, 180])
            rows = timeline(base, {'scope': [5.5, 10.5]})
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]['interval'], [6, 7])
            self.assertEqual(rows[-1]['interval'], [9, 10])

    def test_caption_references_cannot_be_visual_citations_or_wrong_time_anchors(self):
        inputs = {'caption_timeline': [{'id': 'c', 'interval': [100., 101.], 'text': 'A pen'}],
                  'candidate_windows': [{'id': 'w', 'interval': [0., 8.]}], 'max_reads': 1,
                  'options': ['a', 'b'], 'option_ids': ['0', '1']}
        report = {'text': 'A pen', 'selected_option': '0', 'caption_ids': ['claim1'], 'checks': []}
        with self.assertRaises(ValueError):
            validate_output(report, output_schema('caption_review', inputs))
        report['caption_ids'] = ['c']
        report['checks'] = [{'window_id': 'w', 'question': 'What is visible?', 'caption_ids': ['c'], 'reason': 'Inspect'}]
        with self.assertRaises(ValueError):
            normalize_review(report, inputs)

    def test_actual_decoder_accepts_caption_answer_without_visual_references(self):
        from lmformatenforcer import JsonSchemaParser
        inputs = {'caption_timeline': [{'id': 'C1', 'interval': [0, 1], 'text': 'A pen'}],
                  'candidate_windows': [], 'options': ['a', 'b'], 'option_ids': ['0', '1'], 'max_reads': 0}
        schema = output_schema('caption_review', inputs)
        report = {'text': 'A pen draws.', 'selected_option': '1', 'caption_ids': ['C1']}
        parser = JsonSchemaParser(decoder_schema(schema))
        for char in json.dumps(report):
            self.assertIn(char, parser.get_allowed_characters())
            parser = parser.add_character(char)
        self.assertTrue(parser.can_end())
        restore_empty_arrays(report, schema)
        validate_output(report, schema)
        self.assertEqual(report['checks'], [])

    def test_scoring_counts_failures_and_separates_caption_baseline_gains_and_losses(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parents[1]/'script'))
        from run_caption_first import score, write
        from script_core.config import digest
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference = [{'question_id': f'q{i}', 'truth': '1', 'correct': True, 'videotree_correct': False} for i in range(4)]
            write(root/'reference_only.json', {'rows': {'script_guided': reference}})
            for i, (initial, final, error) in enumerate([('0', '1', None), ('1', '0', None),
                                                        ('1', '1', {'code': 'tool_failed'}), ('1', '1', None)]):
                directory = root/'groups'/str(i)
                directory.mkdir(parents=True)
                (directory/'questions.jsonl').write_text(json.dumps({'question_id': f'q{i}'})+'\n')
                write(directory/'runs'/digest(f'q{i}')[:20]/'result.json', {
                    'error': error, 'answer': {'selected_option': final},
                    'diagnostics': {'reading': {'caption_initial_answer': {'selected_option': initial}, 'visual_reads': i}},
                    'cost_summary': {'online': 10., 'budget_violation': False}})
            result = score(root)
            self.assertEqual(result['correct'], 2)
            self.assertEqual(result['accuracy'], .5)
            self.assertEqual(result['errors'], 1)
            self.assertEqual(result['caption_paired'], {'n': 4, 'baseline_correct': 3,
                                                      'final_correct': 2, 'fixed': 1, 'lost': 2})


if __name__ == '__main__':
    unittest.main()
