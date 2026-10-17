"""Bound reading cost without turning caption confidence into a proof."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from script_core.build import summarize_all
from script_core.budget import ToolFailure
from script_core.caption_context import caption_context
from script_core.config import DEFAULTS, load_config, validate_config, base_config, digest
from script_core.controller import answer_query
from script_core.query_policy import global_checkpoints
from script_core.schemas import output_schema, validate_output, decoder_schema
from script_core.selective import eligible, normalize_plan
from script_core.state import obj
from test_pilot_repairs import long_base
from test_script_core import FixtureBackend


def guided_config():
    cfg = deepcopy(DEFAULTS)
    cfg['query']['reading_policy'] = 'script_guided'
    cfg['query']['caption_first'] = False  # Exercise the retained read-plan ablation.
    return cfg


class ReadingBackend(FixtureBackend):
    def __init__(self, memory, count=1):
        super().__init__(memory)
        self.count = count
        self.inputs = {}

    def call(self, operation, inputs, timeout):
        self.inputs.setdefault(operation, []).append(deepcopy(inputs))
        if operation == 'read_plan':
            self.operations.append(operation)
            windows = inputs['coarse_context']['windows']
            count = min(inputs['max_reads'], max(0, self.count-len(inputs.get('inspected_windows', []))))
            chosen = sorted(windows, key=lambda w: ('distinctive tool' not in w['caption'], w['interval'][0]))[:count]
            return {'data': {'reason': 'Check the specific tool rather than repeating the general action',
                'checks': [{'window_id': w['id'], 'goal': 'Record the tool actually used',
                            'reason': 'Tool identity distinguishes the choices'} for w in chosen]}}
        return super().call(operation, inputs, timeout)


class SelectiveReadingTests(unittest.TestCase):
    def run_guided(self, count):
        base, metadata = long_base()
        # The desired interval is away from the old fixed coverage checkpoints.
        base.get('w17')['caption'] = 'A distinctive tool is visible in this step.'
        backend = ReadingBackend(base, count)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'What is the overall process and which distinctive tool is used?',
                'options': ['Use a grater', 'Use a knife']}, metadata, guided_config(), backend, folder)
            steps = [json.loads(line) for line in (Path(folder)/'steps.jsonl').read_text().splitlines()] if (Path(folder)/'steps.jsonl').exists() else []
        return result, backend, steps

    def test_one_targeted_read_replaces_six_checkpoint_reads(self):
        result, backend, steps = self.run_guided(1)
        self.assertIsNone(result['error'])
        self.assertEqual(backend.operations.count('read_plan'), 2)
        self.assertEqual(backend.operations.count('observe'), 1)
        self.assertEqual(backend.operations.count('verify_claims'), 1)
        self.assertEqual(backend.operations.count('assess'), 0)
        self.assertEqual(backend.operations.count('goal'), 0)
        self.assertEqual(backend.operations.count('answer'), 1)
        self.assertEqual(steps[0]['route'], 'script')
        self.assertEqual(steps[0]['interval'], [119., 127.])
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')
        self.assertEqual(result['stop_decision']['exploration_limit'], 'script_read_plan_complete')
        self.assertFalse(result['cost_summary']['budget_violation'])
        final = backend.inputs['answer'][0]
        self.assertTrue(final['claims'])
        self.assertTrue(final['script_outline']['scenes'])
        self.assertEqual(final['claims'][0]['description'], 'Fixture observation')

    def test_script_only_best_choice_does_not_claim_visual_sufficiency(self):
        result, backend, steps = self.run_guided(0)
        self.assertIsNone(result['error'])
        self.assertNotIn('decode', backend.operations)
        self.assertNotIn('observe', backend.operations)
        self.assertEqual(len(steps), 0)
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')
        self.assertEqual(result['answer']['claim_ids'], [])

    def test_planned_reads_have_a_hard_bound(self):
        result, backend, steps = self.run_guided(3)
        self.assertIsNone(result['error'])
        self.assertLessEqual(backend.operations.count('observe'), 3)
        self.assertLessEqual(len(steps), 3)

    def test_failed_planning_keeps_the_budget_and_does_not_start_a_coverage_sweep(self):
        base, metadata = long_base()
        class FailedPlanner(ReadingBackend):
            def call(self, operation, inputs, timeout):
                if operation == 'read_plan':
                    raise ToolFailure('Invalid bounded read plan')
                return super().call(operation, inputs, timeout)
        backend = FailedPlanner(base)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'Summarize the overall activity.',
                'options': ['a', 'b']}, metadata, guided_config(), backend, folder)
        self.assertIsNone(result['error'])
        self.assertNotIn('observe', backend.operations)
        self.assertTrue(result['diagnostics']['reading']['planning_error'])
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')
        self.assertFalse(result['cost_summary']['budget_violation'])

    def test_strict_open_and_local_queries_keep_the_existing_policy(self):
        base, _ = long_base()
        base.put('requirements', obj('r', predicate='other', roles={}, scope=[0., 180.], quantifier='global',
            prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot='activity', support_sets=[], status='unresolved'))
        cfg = guided_config()
        for question, mode in [('Count every action throughout the video.', 'choice'),
                               ('Where is the last object placed?', 'choice'),
                               ('Was this action never performed?', 'choice'), ('Summarize the video.', 'open')]:
            self.assertFalse(eligible(base, {'question': question, 'answer_mode': mode, 'options': ['a', 'b']}, cfg))
        base.get('r')['quantifier'] = 'exists'
        self.assertFalse(eligible(base, {'question': 'What color is the tool?', 'answer_mode': 'choice'}, cfg))
        self.assertEqual(global_checkpoints([0., 180.], cfg), global_checkpoints([0., 180.], DEFAULTS))

    def test_caption_selection_keeps_whole_records_and_reports_omissions(self):
        base, _ = long_base()
        base.put('requirements', obj('r', predicate='other', roles={}, scope=[0., 180.], quantifier='global',
            prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot='activity', support_sets=[], status='unresolved'))
        query = {'question': 'What is the overall activity?', 'options': ['a', 'b'], 'scope': [0., 180.]}
        cfg = guided_config()
        cfg['query']['caption_policy'] = 'ranked'
        context = caption_context(base, query, cfg)
        self.assertEqual(context['selected_windows'], 12)
        self.assertGreater(context['omitted_windows'], 0)
        self.assertEqual(len(base.tables['windows']), context['eligible_windows'])
        for w in context['windows']:
            self.assertEqual(w['caption'], base.get(w['id'])['caption'])
        query['question'] = 'Count all actions throughout the video.'
        self.assertGreater(caption_context(base, query, cfg)['selected_windows'], 12)

    def test_duplicate_and_overlapping_read_requests_are_coalesced(self):
        inputs = {'max_reads': 3, 'coarse_context': {'windows': [
            {'id': 'a', 'interval': [0., 8.]}, {'id': 'b', 'interval': [2., 10.]}, {'id': 'c', 'interval': [20., 28.]}]}}
        report = {'reason': 'Check stages', 'checks': [
            {'window_id': w, 'goal': 'Inspect a visible step', 'reason': 'Distinct stage'} for w in ['a', 'b', 'c']]}
        self.assertEqual([r['window_id'] for r in normalize_plan(report, inputs)['checks']], ['a', 'c'])
        report['checks'][0]['window_id'] = 'outside'
        with self.assertRaises(ValueError):
            normalize_plan(report, inputs)

    def test_candidate_context_prioritizes_a_rare_distinguishing_detail(self):
        base, _ = long_base()
        base.put('requirements', obj('r', predicate='other', roles={}, scope=[0., 180.], quantifier='global',
            prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot='activity', support_sets=[], status='unresolved'))
        for w in base.tables['windows'].values():
            w['caption'] = 'Using needle and thread to stitch fabric in a hoop.'
        base.get('w10')['caption'] = 'A pen draws a pattern.'
        cfg = guided_config()
        cfg['query']['caption_policy'] = 'ranked'
        cfg['query']['script_context_windows'] = 4
        query = {'question': 'What is the overall process using needle and thread?', 'scope': [0., 180.],
            'options': ['She draws a pattern using a pen, then stitches using needle and thread.',
                        'She cuts fabric using scissors, then stitches using needle and thread.']}
        context = caption_context(base, query, cfg)
        self.assertIn('w10', [w['id'] for w in context['windows']])

    def test_profile_changes_online_policy_without_changing_frozen_m0_configuration(self):
        cfg = load_config(Path(__file__).parents[1]/'configs/script_guided.yaml')
        old = load_config()
        old['initialization']['caption_source'] = 'provided'
        old['models']['batching'] = deepcopy(cfg['models']['batching'])
        self.assertEqual(digest(base_config(cfg)), digest(base_config(old)))
        for value in (0, -1, True):
            bad = deepcopy(cfg)
            bad['query']['max_script_reads'] = value
            with self.assertRaises(ValueError):
                validate_config(bad)


class BoundedSummaryTests(unittest.TestCase):
    def test_actual_decoder_forces_summary_text_to_close_at_character_limit(self):
        from lmformatenforcer import JsonSchemaParser
        schema = output_schema('summarize', {'max_chars': 8})
        parser = JsonSchemaParser(decoder_schema(schema)['properties']['text'])
        for character in '"abcdefgh':
            self.assertIn(character, parser.get_allowed_characters())
            parser = parser.add_character(character)
        self.assertNotIn('i', parser.get_allowed_characters())
        self.assertIn('"', parser.get_allowed_characters())
        with self.assertRaises(ValueError):
            validate_output({'text': 'x'*9, 'place': None, 'event_keywords': [], 'uncertainties': []}, schema)

    def test_short_single_window_reuses_text_without_model_call(self):
        meter = Mock()
        result = summarize_all(meter, [{'caption': 'A person draws a pattern.', 'uncertainties': ['Pen partly occluded']}], DEFAULTS)
        meter.json.assert_not_called()
        self.assertEqual(result['text'], 'A person draws a pattern.')
        self.assertEqual(result['uncertainties'], ['Pen partly occluded'])

    def test_reduction_keeps_all_original_uncertainties(self):
        meter = Mock()
        meter.json.return_value = ({'text': 'Compressed activity', 'place': None, 'event_keywords': [], 'uncertainties': ['New gap']}, {})
        result = summarize_all(meter, [{'caption': 'A'*200, 'uncertainties': ['Gap A']},
                                      {'caption': 'B'*200, 'uncertainties': ['Gap B']}], DEFAULTS)
        self.assertEqual(result['uncertainties'], ['Gap A', 'Gap B', 'New gap'])
        self.assertEqual(len(meter.json.call_args.args[1]['windows']), 2)


if __name__ == '__main__':
    unittest.main()
