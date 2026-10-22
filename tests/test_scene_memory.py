"""Scene laziness, caption preservation and source-backed correspondence boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]/'script'))
from script_core.config import DEFAULTS, digest, validate_config
from script_core.controller import answer_query
from script_core.scene_memory import (assemble_directory, add_pointers, validate_directory, frontier,
    build_directory, build_local_graph, validate_matches, caption_sources, relation_context,
    compact_review_inputs, short_context_ids)
from script_core.state import memory_hash
from script_core.video import file_hash
from script_core.budget import Meter, BudgetLedger, ToolFailure
from script_core.schemas import output_schema, validate_output
from test_pilot_repairs import long_base
from test_script_core import FixtureBackend
from run_scene_lazy import profile_config, score, gate
from run_summary_graph import write


class SceneBackend(FixtureBackend):
    def __init__(self, memory, expand=False, backwards=False):
        super().__init__(memory)
        self.seen = []
        self.expand, self.backwards = expand, backwards
        self.directory_bad = False
        self.review_bad = False
        self.scope = 'local'
        self.scope_bad = False
        self.overflow_joint_review = False

    def call(self, operation, inputs, timeout):
        self.seen.append((operation, deepcopy(inputs)))
        if operation == 'scene_directory':
            if self.directory_bad:
                raise ToolFailure('invalid partition')
            phases = [{'end_caption_id': f'TS{i}', 'description': 'C uses a cup.',
                'entity_terms': ['cup', 'invented hammer']} for i in (60, 120, 180)]
            return {'data': {'scenes': phases[:-1], 'final_scene': phases[-1]}}
        if operation == 'scene_scope':
            if self.scope_bad:
                raise ToolFailure('scope unavailable')
            return {'data': {'scope': self.scope, 'reason': 'Temporal requirement of the question'}}
        if operation == 'summary_graph':
            c = inputs['captions'][0]
            return {'data': {'events': [{'caption_ids': [c['id']], 'action': 'uses', 'actor': 'C',
                'object': 'cup', 'instrument': '', 'location': '', 'hand': ''}]}}
        if operation == 'scene_review':
            if self.review_bad:
                raise ToolFailure('review unavailable')
            if self.overflow_joint_review and len(inputs['local_graphs']) > 1:
                raise ToolFailure('Context overflow (17000); text records require explicit batching')
            enough = len(inputs['opened_scene_ids']) >= 2 or not self.expand
            p = next((p for p in inputs['frontier'] if p['direction'] == ('backward' if self.backwards else 'forward')), None)
            return {'data': {'selected_option': '0', 'reason': 'Source distinction',
                'caption_ids': [inputs['captions'][0]['id']], 'sufficient': enough,
                'gap': '' if enough else 'Check the earlier cup', 'next_pointer': None if enough else p['id']}}
        if operation == 'scene_correspondence':
            l, r = inputs['left']['entities'][0], inputs['right']['entities'][0]
            return {'data': {'matches': [{'left': {'entity': l['id'], 'caption_ids': [l['caption_ids'][0]]},
                'right': {'entity': r['id'], 'caption_ids': [r['caption_ids'][0]]},
                'decision': 'same', 'reason': 'Continuous manipulation'}]}}
        if operation == 'videotree_qa':
            return {'data': {'selected_option': '0', 'text': 'prediction: A', 'info': {'prompt_tokens': 99}}}
        if operation == 'retrieve' and self.backwards:
            return {'data': [{'id': d['id'], 'score': 1., 'interval': d['interval']} for d in reversed(inputs['documents'])]}
        return super().call(operation, inputs, timeout)


class SceneMemoryTests(unittest.TestCase):
    def setup_inputs(self, folder):
        memory, metadata = long_base()
        path = Path(folder)/'captions.json'
        write(path, {'spans': [{'start': i, 'end': i+1, 'text': f'C uses a cup at second {i}.'} for i in range(180)]})
        memory.manifest.update(caption_uri=str(path), caption_hash=file_hash(path))
        metadata['base_hash'] = memory_hash(memory)
        cfg = profile_config(deepcopy(DEFAULTS), 'scene_lazy')
        captions = caption_sources(memory.manifest)
        report = {'scenes': [{'end_caption_id': f'TS{i}', 'description': 'C uses a cup.',
            'entity_terms': ['cup', 'invented hammer']} for i in (60, 120, 180)]}
        directory = assemble_directory('v', file_hash(path), captions, report, cfg)
        add_pointers(directory, [[1., 0.]]*3, 2)
        query = {'question_id': 'q', 'video_id': 'v', 'question': 'Which object is used?',
            'options': ['cup', 'knife', 'ruler', 'brush', 'hammer']}
        return memory, metadata, cfg, captions, directory, query

    def run_case(self, expand=False, backwards=False, limit=4, review_bad=False, scope='local'):
        with tempfile.TemporaryDirectory() as folder:
            m, meta, cfg, _, directory, query = self.setup_inputs(folder)
            cfg['scene_memory']['max_scene_visits'] = limit
            before = deepcopy(directory)
            backend = SceneBackend(m, expand, backwards)
            backend.review_bad = review_bad
            backend.scope = scope
            result = answer_query(m, query, meta, cfg, backend, Path(folder)/'run', scene_directory=directory)
            self.assertIsNone(result['error'], result['error'])
            self.assertEqual(before, directory)
            self.assertTrue((Path(folder)/'run/state.sqlite3').exists())
            self.assertTrue((Path(folder)/'run/scene_memory/progress.json').exists())
            return result, backend

    def test_directory_keeps_every_caption_and_has_no_complex_graphs(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, _, captions, d, _ = self.setup_inputs(folder)
            validate_directory(d, m.manifest)
            self.assertEqual(d['captions'], captions)
            self.assertEqual([c for s in d['scenes'] for c in s['caption_ids']], [c['id'] for c in captions])
            self.assertTrue(all(s['status'] == 'directory_only' and 'entities' not in s for s in d['scenes']))
            self.assertTrue(all(not p['entity_correspondences'] for p in d['pointers']))
            self.assertEqual(d['scenes'][0]['entity_terms'], ['cup'])

    def test_nonadjacent_pointer_and_reverse_frontier(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, _, _, d, _ = self.setup_inputs(folder)
            first, _, last = [s['id'] for s in d['scenes']]
            p = next(p for p in d['pointers'] if p['source'] == first and p['target'] == last)
            self.assertEqual(p['kind'], 'semantic_candidate')
            reverse = next(x for x in frontier(d, {last}) if x['id'] == p['id'])
            self.assertEqual(reverse['direction'], 'backward')
            self.assertEqual(reverse['next_scene'], first)

    def test_missing_or_duplicate_sources_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, _, _, d, _ = self.setup_inputs(folder)
            d['scenes'][0]['caption_ids'].pop()
            with self.assertRaises(ValueError): validate_directory(d, m.manifest)

    def test_prebuilt_graph_or_identity_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, _, _, d, _ = self.setup_inputs(folder)
            d['pointers'][0]['entity_correspondences'] = [{'same': True}]
            with self.assertRaises(ValueError): validate_directory(d, m.manifest)

    def test_oversized_phase_split_preserves_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, cfg, captions, _, _ = self.setup_inputs(folder)
            d = assemble_directory('v', 'h', captions, {'scenes': [{'end_caption_id': 'TS180',
                'description': 'Manipulates objects', 'entity_terms': []}]}, cfg)
            self.assertEqual(len(d['scenes']), 3)
            self.assertTrue(all(s['interval'][1]-s['interval'][0] <= 60 for s in d['scenes']))

    def test_incomplete_partition_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, cfg, captions, _, _ = self.setup_inputs(folder)
            with self.assertRaises(ValueError):
                assemble_directory('v', 'h', captions, {'scenes': [{'end_caption_id': 'TS60',
                    'description': '', 'entity_terms': []}]}, cfg)

    def test_offline_directory_has_no_question_or_relation_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, cfg, _, _, _ = self.setup_inputs(folder)
            backend = SceneBackend(m)
            meter = Meter(backend, BudgetLedger(300., answer_reserve=0.), cfg, Path(folder)/'raw')
            try: d = build_directory(m.manifest, cfg, meter)
            finally: meter.close()
            self.assertEqual([op for op, _ in backend.seen], ['scene_directory', 'embed'])
            self.assertNotIn('question', backend.seen[0][1])
            validate_directory(d, m.manifest)

    def test_explicit_directory_fallback_preserves_all_raw_text(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, cfg, captions, _, _ = self.setup_inputs(folder)
            backend = SceneBackend(m); backend.directory_bad = True
            meter = Meter(backend, BudgetLedger(300., answer_reserve=0.), cfg, Path(folder)/'raw')
            try: d = build_directory(m.manifest, cfg, meter)
            finally: meter.close()
            self.assertEqual(d['captions'], captions)
            self.assertEqual(len(d['scenes']), 6)
            self.assertIn('invalid partition', d['build_diagnostics']['fallback_reason'])

    def test_sufficient_scene_never_opens_others_or_matches_entities(self):
        r, b = self.run_case()
        d = r['scene_memory']
        self.assertEqual(len(d['opened_scenes']), 1)
        self.assertEqual(set(d['local_graphs']), set(d['opened_scenes']))
        self.assertEqual(d['correspondences'], [])
        self.assertFalse(any(op == 'scene_correspondence' for op, _ in b.seen))
        sources = [c['id'] for op,i in b.seen if op == 'summary_graph' for c in i['captions']]
        self.assertEqual(sources, [f'TS{i}' for i in range(1, 61)])

    def test_global_question_rejects_local_sufficiency_and_reads_three_stages(self):
        r, b = self.run_case(scope='global')
        d = r['scene_memory']
        self.assertEqual(len(d['opened_scenes']), 3)
        self.assertTrue(d['text_sufficient'])
        self.assertTrue(d['reviews'][0]['model_decision']['sufficient'])
        self.assertFalse(d['reviews'][0]['sufficient'])
        self.assertFalse(d['reviews'][0]['coverage_check']['met'])
        self.assertTrue(d['reviews'][-1]['coverage_check']['met'])
        scope_input = next(i for op, i in b.seen if op == 'scene_scope')
        self.assertEqual(set(scope_input), {'question', 'options'})

    def test_global_visit_limit_does_not_claim_text_sufficiency(self):
        r, _ = self.run_case(scope='global', limit=2)
        self.assertEqual(len(r['scene_memory']['opened_scenes']), 2)
        self.assertFalse(r['scene_memory']['text_sufficient'])

    def test_cross_stage_question_checks_more_than_one_scene(self):
        r, _ = self.run_case(scope='multi_scene')
        self.assertEqual(len(r['scene_memory']['opened_scenes']), 2)

    def test_exhaustive_requirement_cannot_stop_before_all_scenes_read(self):
        r, _ = self.run_case(scope='exhaustive', limit=2)
        self.assertFalse(r['scene_memory']['text_sufficient'])
        self.assertEqual(len(r['scene_memory']['reviews'][-1]['coverage_check']['missing']), 1)

    def test_directory_schema_requires_terminal_boundary_even_when_array_fills(self):
        captions = [{'id': f'TS{i}'} for i in range(1, 181)]
        schema = output_schema('scene_directory', {'captions': captions, 'max_scenes': 6})
        row = lambda i: {'end_caption_id': f'TS{i}', 'description': 'An activity phase', 'entity_terms': []}
        truncated = {'scenes': [row(i) for i in (3, 5, 7, 9, 11)]}
        with self.assertRaises(ValueError):
            validate_output(truncated, schema)
        truncated['final_scene'] = row(12)
        with self.assertRaises(ValueError):
            validate_output(truncated, schema)
        truncated['final_scene'] = row(180)
        validate_output(truncated, schema)
        with tempfile.TemporaryDirectory() as folder:
            _, _, cfg, sources, _, _ = self.setup_inputs(folder)
            d = assemble_directory('v', 'h', sources, truncated, cfg)
            self.assertEqual([c for s in d['scenes'] for c in s['caption_ids']], [c['id'] for c in sources])

    def test_single_caption_directory_can_only_have_final_phase(self):
        schema = output_schema('scene_directory', {'captions': [{'id': 'TS1'}], 'max_scenes': 1})
        validate_output({'scenes': [], 'final_scene': {'end_caption_id': 'TS1',
            'description': 'A single observation', 'entity_terms': []}}, schema)

    def test_scope_failure_cannot_turn_into_sufficient_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            m, meta, cfg, _, directory, query = self.setup_inputs(folder)
            backend = SceneBackend(m)
            backend.scope_bad = True
            r = answer_query(m, query, meta, cfg, backend, Path(folder)/'run', scene_directory=directory)
            self.assertIsNone(r['error'])
            self.assertFalse(r['scene_memory']['text_sufficient'])
            self.assertEqual(r['scene_memory']['scope_requirement']['scope'], 'unresolved')

    def test_repair_smoke_rejects_fallback_and_unchecked_early_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root/'experiment.json', {'arms': {'scene_lazy': 'repair'}, 'require_scene_repairs': True})
            write(root/'smoke/ids.json', ['q'])
            write(root/'evaluation_only.json', {'q': {'videotree': None}})
            write(root/'smoke/arms/scene_lazy/runs'/digest('q')[:20]/'result.json', {
                'error': None, 'scene_memory': {'opened_scenes': ['s1'], 'local_graphs': {'s1': {}},
                    'pixel_reads': 0, 'total_scenes': 3, 'directory_fallback': 'missing last caption',
                    'scope_requirement': {'scope': 'global'},
                    'reviews': [{'sufficient': True, 'coverage_check': {'met': False}}]}})
            self.assertFalse(gate(root))
            status = json.loads((root/'smoke/status.json').read_text())
            reasons = {f['reason'] for f in status['failures']}
            self.assertIn('semantic directory fell back', reasons)
            self.assertIn('invalid temporal sufficiency', reasons)
            self.assertIn('missing cross-scene exploration', reasons)

    def test_gap_traverses_pointer_before_cross_scene_correspondence(self):
        r, b = self.run_case(expand=True)
        d = r['scene_memory']
        self.assertEqual(len(d['opened_scenes']), 2)
        ops = [op for op, _ in b.seen]
        self.assertLess(ops.index('scene_review'), ops.index('scene_correspondence'))
        self.assertEqual(len(d['traversals']), 1)
        match = d['correspondences'][0]['matches'][0]
        self.assertEqual(match['status'], 'text_inferred')
        self.assertFalse(match['physical_identity_confirmed'])

    def test_backward_traversal_opens_previous_scene(self):
        r, _ = self.run_case(expand=True, backwards=True)
        self.assertEqual(r['scene_memory']['traversals'][0]['direction'], 'backward')
        self.assertTrue(r['scene_memory']['entry_scene'].endswith('003'))

    def test_visit_limit_blocks_traversal_and_correspondence(self):
        r, b = self.run_case(expand=True, limit=1)
        self.assertEqual(len(r['scene_memory']['opened_scenes']), 1)
        self.assertFalse(any(op == 'scene_correspondence' for op, _ in b.seen))
        self.assertFalse(r['scene_memory']['text_sufficient'])

    def test_review_failure_still_answers_from_opened_descriptions(self):
        r, b = self.run_case(review_bad=True)
        self.assertTrue(r['scene_memory']['extension_errors'])
        self.assertEqual(r['answer']['selected_option'], '0')
        self.assertEqual([op for op, _ in b.seen].count('videotree_qa'), 1)

    def test_final_qa_retains_raw_times_order_and_no_unopened_captions(self):
        r, b = self.run_case()
        final = next(i for op,i in b.seen if op == 'videotree_qa')
        self.assertIn('Frame 0 (0s): C uses a cup at second 0.', final['prompts'][0])
        self.assertIn('Frame 59 (59s)', final['prompts'][0])
        self.assertNotIn('Frame 60 (60s)', final['prompts'][0])
        self.assertIn('A: cup', final['prompts'][0])
        self.assertFalse(any(op in ('decode', 'observe', 'detect', 'verify_identity') for op,_ in b.seen))
        self.assertEqual(r['stop_decision']['evidence_status'], 'insufficient')

    def test_cross_scene_namespaces_and_both_side_citations(self):
        r, _ = self.run_case(expand=True)
        graphs = list(r['scene_memory']['local_graphs'].values())
        left, right = graphs[:2]
        self.assertTrue(set(e['id'] for e in left['entities']).isdisjoint(e['id'] for e in right['entities']))
        report = {'matches': [{'left_entity': left['entities'][0]['id'], 'right_entity': right['entities'][0]['id'],
            'decision': 'same', 'reason': 'same name', 'caption_ids': [left['captions'][0]['id']]}]}
        with self.assertRaises(ValueError): validate_matches(report, left, right)

    def test_correspondence_schema_couples_each_entity_to_its_own_scene_citations(self):
        r, _ = self.run_case(expand=True)
        left, right = list(r['scene_memory']['local_graphs'].values())[:2]
        l, rr = left['entities'][0], right['entities'][0]
        inputs = {'left': left, 'right': right}
        match = {'left': {'entity': l['id'], 'caption_ids': [l['caption_ids'][0]]},
            'right': {'entity': rr['id'], 'caption_ids': [rr['caption_ids'][0]]},
            'decision': 'same', 'reason': 'Continuity candidate'}
        schema = output_schema('scene_correspondence', inputs)
        validate_output({'matches': [match]}, schema)
        from lmformatenforcer import JsonSchemaParser
        from script_core.schemas import decoder_schema
        parser = JsonSchemaParser(decoder_schema(schema))
        for char in json.dumps({'matches': [match]}):
            parser = parser.add_character(char)
        self.assertTrue(parser.can_end())
        normalized = validate_matches({'matches': [match]}, left, right)[0]
        self.assertEqual(normalized['left_entity'], l['id'])
        self.assertEqual(set(normalized['caption_ids']), {l['caption_ids'][0], rr['caption_ids'][0]})
        for side in ('left', 'right'):
            bad = deepcopy(match)
            bad[side]['caption_ids'] = []
            with self.assertRaises(ValueError): validate_output({'matches': [bad]}, schema)
        bad = deepcopy(match)
        bad['right']['caption_ids'] = match['left']['caption_ids']
        with self.assertRaises(ValueError): validate_output({'matches': [bad]}, schema)
        # A valid source ID from another entity in the SAME scene is also rejected.
        extra = {'id': 'other', 'label': 'spoon', 'caption_ids': ['TS70']}
        inputs['right'] = deepcopy(right)
        inputs['right']['entities'].append(extra)
        schema = output_schema('scene_correspondence', inputs)
        bad = deepcopy(match)
        bad['right']['entity'] = 'other'
        with self.assertRaises(ValueError): validate_output({'matches': [bad]}, schema)

    def test_compact_review_retains_all_source_text_roles_and_navigation_candidates(self):
        r, b = self.run_case(expand=True)
        d = r['scene_memory']
        review = [i for op, i in b.seen if op == 'scene_review'][-1]
        graphs = list(d['local_graphs'].values())
        allowed = {c['id']: c for g in graphs for c in g['captions']}
        self.assertEqual(review['captions'], list(allowed.values()))
        for original, compact in zip(graphs, review['local_graphs']):
            self.assertEqual([e['id'] for e in original['events']], [e['id'] for e in compact['events']])
            for event, projected in zip(original['events'], compact['events']):
                self.assertEqual(event['roles'], projected['roles'])
                self.assertEqual(event['description'], '\n'.join(allowed[c]['text'] for c in projected['caption_ids']))
            for entity in original['entities']:
                refs = {c for e in compact['events'] if entity['id'] in e['roles'].values() for c in e['caption_ids']}
                self.assertEqual(refs, set(entity['caption_ids']))
        final = next(i for op, i in b.seen if op == 'videotree_qa')
        self.assertNotIn(':text_entity:', final['prompts'][-1])
        self.assertNotIn(':text_event:', final['prompts'][-1])
        self.assertIn('text labels', final['prompts'][-1])

    def test_context_overflow_batches_graphs_without_dropping_any_captions(self):
        with tempfile.TemporaryDirectory() as folder:
            m, meta, cfg, _, directory, query = self.setup_inputs(folder)
            backend = SceneBackend(m)
            backend.scope = 'global'
            backend.overflow_joint_review = True
            result = answer_query(m, query, meta, cfg, backend, Path(folder)/'run', scene_directory=directory)
            self.assertIsNone(result['error'])
            self.assertTrue(result['scene_memory']['text_sufficient'])
            self.assertFalse(result['scene_memory']['extension_errors'])
            self.assertIn('graph_batches', result['scene_memory']['reviews'][-1])
            batches = [i for op, i in backend.seen if op == 'scene_review' and 'graph_batch_status' in i]
            self.assertTrue(batches)
            for batch in batches:
                self.assertEqual(len(batch['captions']), 60*len(batch['opened_scene_ids']))
                self.assertIn('entity_index', batch)
            self.assertEqual(len(batches[-1]['captions']), 180)

    def test_review_schema_rejects_unopened_caption_and_invented_pointer(self):
        inputs = {'captions': [{'id': 'C1'}], 'option_ids': list('01234'), 'frontier': [{'id': 'P1'}]}
        value = {'selected_option': '0', 'reason': '', 'caption_ids': ['C2'], 'sufficient': False, 'gap': '', 'next_pointer': 'P1'}
        with self.assertRaises(ValueError): validate_output(value, output_schema('scene_review', inputs))
        value.update(caption_ids=['C1'], next_pointer='P2')
        with self.assertRaises(ValueError): validate_output(value, output_schema('scene_review', inputs))

    def test_modes_mutually_exclusive_and_base_unchanged(self):
        cfg = deepcopy(DEFAULTS)
        cfg['scene_memory']['enabled'] = cfg['compatibility']['enabled'] = True
        with self.assertRaises(ValueError): validate_config(cfg)
        self.assertFalse(profile_config(cfg, 'c0')['scene_memory']['enabled'])

    def test_score_preserves_pending_denominator_and_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root/'cohort.json', ['q1', 'q2'])
            write(root/'evaluation_only.json', {u: {'truth': '0', 'videotree': None} for u in ('q1','q2')})
            write(root/'experiment.json', {'notes': []})
            write(root/'arms/scene_lazy/runs'/digest('q1')[:20]/'result.json', {'error': {'code': 'failed'}})
            result = score(root)
            self.assertFalse(result['complete'])
            self.assertIsNone(result['groups']['scene_lazy']['accuracy'])
            self.assertEqual(result['groups']['scene_lazy']['errors'], 1)


if __name__ == '__main__':
    unittest.main()
