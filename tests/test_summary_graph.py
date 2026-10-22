"""Caption graphs preserve source boundaries and keep ordinary coverage intact."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]/'script'))
from script_core.config import DEFAULTS, digest
from script_core.controller import answer_query
from script_core.schemas import output_schema, validate_output
from script_core.state import memory_hash
from script_core.summary_graph import (source_records, extraction_inputs, assemble_graph,
                                      validate_graph, retrieve_graph, promote_windows)
from script_core.video import file_hash
from run_summary_graph import build_graph, new_visual_claims, protect, score, write
from test_pilot_repairs import long_base
from test_script_core import FixtureBackend


def graph_fixture(start=35, video='v', caption_hash=None):
    captions = source_records([{'start': start, 'end': start+1, 'text': 'C uses a grater on the cheese with the right hand.'}], 180)
    report = {'events': [{'caption_ids': ['TS1'], 'actor': 'C', 'action': 'uses', 'object': 'cheese',
                         'instrument': 'grater', 'location': '', 'hand': 'right hand'}]}
    return assemble_graph(video, caption_hash, captions, [report])


class GraphBackend(FixtureBackend):
    def __init__(self, memory):
        super().__init__(memory)
        self.answers = []

    def call(self, operation, inputs, timeout):
        result = super().call(operation, inputs, timeout)
        if operation == 'gate':
            result['data']['passed'] = False
            result['data']['unresolved'] = ['Tool identity remains uncertain']
        if operation == 'answer':
            self.answers.append(deepcopy(inputs))
            if inputs.get('summary_graph'):
                result['data']['caption_event_ids'] = [inputs['summary_graph']['events'][0]['id']]
        return result


class SummaryGraphTests(unittest.TestCase):
    def test_extraction_has_no_question_options_labels_or_images(self):
        inputs = extraction_inputs(graph_fixture()['captions'])
        self.assertEqual(set(inputs), {'captions', 'max_events'})
        self.assertNotIn('selected_option', json.dumps(inputs))

    def test_repeated_descriptions_at_different_times_stay_distinct(self):
        spans = [{'start': t, 'end': t+1, 'text': 'C cuts bread.'} for t in (0, 40)]
        captions = source_records(spans, 100)
        reports = [{'events': [{'caption_ids': [c['id']], 'action': 'cuts', 'actor': 'C',
                    'object': 'bread', 'instrument': '', 'hand': '', 'location': ''}]} for c in captions]
        graph = assemble_graph('v', None, captions, reports)
        self.assertEqual(len(graph['events']), 2)
        self.assertNotEqual(graph['events'][0]['id'], graph['events'][1]['id'])
        self.assertEqual(graph['temporal_edges'][0]['basis'], 'source_timestamps_only')
        self.assertFalse(graph['temporal_edges'][0]['causal'])

    def test_unknown_sources_and_distant_merges_are_rejected(self):
        captions = source_records([{'start': t, 'end': t+1, 'text': 'C cuts bread.'} for t in (0, 40)], 100)
        report = {'events': [{'caption_ids': ['fake']}, {'caption_ids': ['TS1', 'TS2']}]}
        graph = assemble_graph('v', None, captions, [report])
        self.assertEqual(graph['events'], [])
        self.assertEqual(graph['build_diagnostics']['rejected_events'], {'unknown_or_empty_caption_reference': 1, 'nonlocal_event_merge': 1})

    def test_hallucinated_tool_is_not_added_and_description_is_exact_quote(self):
        graph = graph_fixture()
        captions = graph['captions']
        graph = assemble_graph('v', None, captions, [{'events': [{'caption_ids': ['TS1'],
            'action': 'repairs', 'instrument': 'hammer', 'object': 'cheese'}]}])
        self.assertNotIn('instrument', graph['events'][0]['roles'])
        self.assertEqual(graph['events'][0]['description'], captions[0]['text'])
        self.assertEqual(graph['events'][0]['action'], 'source_description')
        self.assertTrue(graph['build_diagnostics']['dropped_roles'])

    def test_source_text_times_and_entity_status_cannot_be_promoted_or_rewritten(self):
        manifest = {'video_id': 'v', 'caption_hash': None}
        graph = graph_fixture()
        validate_graph(graph, manifest)
        for modify in ('text', 'time', 'identity', 'source'):
            changed = deepcopy(graph)
            if modify == 'text': changed['events'][0]['description'] = 'A hammer is used.'
            if modify == 'time': changed['events'][0]['interval'] = [0, 1]
            if modify == 'identity': changed['entities'][0]['identity_status'] = 'confirmed'
            if modify == 'source': changed['captions'][0]['text'] = 'Changed'
            with self.assertRaises(ValueError):
                validate_graph(changed, manifest)

    def test_actual_caption_file_is_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'captions.json'
            write(path, {'spans': [{'start': 35, 'end': 36, 'text': 'Different original description.'}]})
            graph = graph_fixture(caption_hash=file_hash(path))
            with self.assertRaisesRegex(ValueError, 'original provided'):
                validate_graph(graph, {'video_id': 'v', 'caption_hash': file_hash(path),
                    'caption_uri': str(path), 'cutoff': 180, 'duration': 180})

    def test_scope_crossing_descriptions_do_not_leak(self):
        self.assertEqual(source_records([{'start': 9, 'end': 11, 'text': 'Future action'}], 10), [])
        query = {'question': 'Which grater?', 'options': ['grater', 'knife'], 'scope': [0, 35.5]}
        self.assertIsNone(retrieve_graph(graph_fixture(), query))

    def test_irrelevant_graph_returns_no_context(self):
        query = {'question': 'Which airplane?', 'options': ['jet', 'helicopter'], 'scope': [0, 180]}
        self.assertIsNone(retrieve_graph(graph_fixture(), query))

    def test_only_existing_matching_windows_are_promoted(self):
        base, _ = long_base()
        ranked = [{'id': key} for key in base.tables['windows']]
        context = retrieve_graph(graph_fixture(), {'question': 'Which grater?', 'options': [], 'scope': [0, 180]})
        after = promote_windows(ranked, context, base)
        self.assertEqual({x['id'] for x in ranked}, {x['id'] for x in after})
        self.assertEqual(base.get(after[0]['id'])['interval'][0], 28.)
        self.assertEqual(promote_windows(ranked, None, base), ranked)

    def test_graph_citations_have_separate_allowlist(self):
        inputs = {'answer_strategy': 'direct', 'answer_mode': 'choice', 'option_ids': ['0', '1'],
                  'claims': [], 'evidence': [], 'summary_graph': {'events': [{'id': 'event'}]}}
        schema = output_schema('answer', inputs)
        answer = {'text': 'Description', 'selected_option': '0', 'claim_ids': [], 'evidence_ids': [], 'caption_event_ids': ['event']}
        validate_output(answer, schema)
        with self.assertRaises(ValueError):
            validate_output(dict(answer, claim_ids=['event']), schema)
        with self.assertRaises(ValueError):
            validate_output(dict(answer, caption_event_ids=['fake']), schema)
        inputs.pop('summary_graph')
        self.assertNotIn('caption_event_ids', output_schema('answer', inputs)['properties'])

    def test_coverage_checkpoints_and_shared_memory_remain_intact(self):
        base, metadata = long_base()
        original_hash = memory_hash(base)
        outputs = []
        for use_graph in (False, True):
            backend = GraphBackend(base)
            with tempfile.TemporaryDirectory() as folder:
                result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                    'question': 'What is the overall process?', 'options': ['Use a grater', 'Use a knife']},
                    metadata, deepcopy(DEFAULTS), backend, folder,
                    summary_graph=graph_fixture() if use_graph else None, graph_routing=use_graph)
                steps = [json.loads(s) for s in (Path(folder)/'steps.jsonl').read_text().splitlines()]
                self.assertIsNone(result['error'], result['error'])
                self.assertEqual((Path(folder)/'entities.jsonl').read_text(), '')
                self.assertFalse(result['cost_summary']['budget_violation'])
                self.assertEqual(bool(backend.answers[-1].get('summary_graph')), use_graph)
                outputs.append((result, steps))
        self.assertEqual([s['interval'] for s in outputs[0][1][:6]], [s['interval'] for s in outputs[1][1][:6]])
        self.assertNotEqual(outputs[0][1][6]['interval'], outputs[1][1][6]['interval'])
        self.assertTrue(outputs[1][0]['answer']['caption_event_ids'])
        self.assertEqual(memory_hash(base), original_hash)

    def test_graph_build_calls_only_text_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'captions.json'
            spans = [{'start': 0, 'end': 1, 'text': 'C cuts bread.'}]
            write(path, {'spans': spans})
            packet = {'manifest': {'video_id': 'v', 'caption_hash': file_hash(path), 'caption_uri': str(path),
                                  'cutoff': 180, 'duration': 180}, 'captions': source_records(spans, 180)}
            class TextBackend:
                def call(self, operation, inputs, timeout):
                    if operation != 'summary_graph' or set(inputs) != {'captions', 'max_events'}:
                        raise AssertionError(operation)
                    return {'data': {'events': [{'caption_ids': ['TS1'], 'action': 'cuts',
                        'actor': 'C', 'object': 'bread', 'instrument': '', 'location': '', 'hand': ''}]}}
            graph = build_graph(Path(folder), packet, TextBackend(), deepcopy(DEFAULTS))
            self.assertTrue(graph['events'])
            self.assertFalse(graph['cost_summary']['budget_violation'])

    def test_guard_rejects_unchanged_facts_and_state_labelled_actions(self):
        claim = {'id': 'c', 'description': 'C cuts bread', 'interval': [0, 2], 'source_type': 'visual',
            'status': 'supported', 'fact_type': 'action', 'source_frame_ids': ['f1', 'f2', 'f3'],
            'verification': {'whole_claim_supported': True, 'process_visible': True, 'unresolved_conflicts': []}}
        self.assertEqual(new_visual_claims({'claims': [claim]}, {'claims': [claim]}), [])
        self.assertEqual(new_visual_claims(None, {'claims': [dict(claim, fact_type='state')]}), [])
        self.assertEqual(new_visual_claims(None, {'claims': [claim]}), [claim])

    def test_protection_without_new_visual_information_preserves_baseline_without_a_call(self):
        with tempfile.TemporaryDirectory() as folder:
            class NoCalls:
                def call(self, *args, **kwargs): raise AssertionError('No audit should run')
            baseline, candidate = {'answer': {'selected_option': '0'}, 'error': None}, {'answer': {'selected_option': '1'}, 'error': None}
            result = protect(Path(folder), {'question': {'question_id': 'q'}}, baseline, candidate, NoCalls(), deepcopy(DEFAULTS))
            self.assertEqual(result['answer'], baseline['answer'])
            self.assertFalse(result['accepted_update'])

    def test_scoring_keeps_errors_in_denominator_and_pending_accuracy_null(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root/'cohort.json', ['q', 'r'])
            write(root/'evaluation_only.json', {u: {'truth': '0', 'historical_correct': True} for u in ('q', 'r')})
            write(root/'experiment.json', {'scope': []})
            write(root/'b0/runs'/digest('q')[:20]/'result.json', {'answer': {'selected_option': '0'}, 'error': None, 'cost_summary': {'online': 1.}})
            self.assertIsNone(score(root)['groups']['b0']['accuracy'])
            write(root/'b0/runs'/digest('r')[:20]/'result.json', {'answer': None, 'error': 'Failed', 'cost_summary': {'online': 1.}})
            group = score(root)['groups']['b0']
            self.assertEqual(group['accuracy'], .5)
            self.assertEqual(group['errors'], 1)


if __name__ == '__main__':
    unittest.main()
