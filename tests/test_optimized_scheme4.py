"""Regression tests for provenance, event instances, gaps and bounded planning."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from script_core import optimized_memory as memory
from script_core import optimized_entity as pipeline
from script_core.config import load_config
from script_core.optimized_sampling import HybridSampler, required_scope
from script_core.schemas import decoder_schema, output_schema, restore_empty_arrays, validate_output
from script_core.state import write_json


def caps(texts):
    return [dict(id=f'C{i}', interval=[i, i+1], text=text) for i, text in enumerate(texts)]


def directory(sources, split=None):
    split = split or len(sources)
    scenes = [dict(id=f'S{n}', center=[1., 0.], interval=[i, min(i+split, len(sources))],
        chronological_order=list(range(i, min(i+split, len(sources)))),
        caption_ids=[c['id'] for c in sources[i:i+split]], description='navigation hint')
        for n, i in enumerate(range(0, len(sources), split))]
    return dict(captions=deepcopy(sources), scenes=scenes, pointers=[])


def span(sources, cid, quote):
    c = next(c for c in sources if c['id'] == cid)
    start = c['text'].index(quote)
    ts = memory.tokens(c['text'])
    a = next(i for i, t in enumerate(ts) if t.start() == start)
    b = next(i+1 for i, t in enumerate(ts) if t.end() == start+len(quote))
    return dict(span_id=f'{cid}@{a}:{b}')


def entity(sources, label, cids=('C0',), ref='new', continuity='initial', kind='state'):
    return dict(entity_ref=ref, mentions=[span(sources, cid, label) for cid in cids], attributes=[],
        observations=[dict(caption_id=cid, kind=kind, continuity=continuity) for cid in cids])


def delta(*entities, relations=(), events=()):
    return dict(entities=list(entities), relations=list(relations), events=list(events), unresolved='')


def fresh(sources, split=None):
    return memory.new_memory('v', sources, 'hash', directory(sources, split))


def commit(m, value, *, cids=None, sid='S0', fields=('state',), depth='event', key='request'):
    sources = [c for c in m['source_captions'].values() if (cids is None or c['id'] in cids)
               and m['ownership'][c['id']] == sid]
    return memory.commit_delta(m, value, sources, sid, dict(request_key=key), 'call', list(fields), depth)


class SourcedMemoryTests(unittest.TestCase):
    def test_event_pass_excludes_page_aliases_and_binds_the_action_to_its_own_caption(self):
        sources = caps(['Alice opens the box.', 'Bob holds a knife.'])
        inp = dict(captions=sources, entities=[dict(id='E7', label='Alice')], events=[])
        action = next(ref for ref, s in memory.event_spans(sources).items() if s['caption_id'] == 'C0')
        event = dict(event_ref='new', kind='action', action=dict(span_id=action),
                     roles=dict(actor='E7'), source_caption_ids=['C0'], basis='caption_explicit')
        schema = output_schema('optimized_events', inp)
        validate_output(delta(events=[event]), schema)
        with self.assertRaises(ValueError):
            validate_output(delta(events=[dict(event, roles=dict(actor='B0'))]), schema)
        with self.assertRaises(ValueError):
            validate_output(delta(events=[dict(event, roles=dict(actor='E7', item='E7'))]), schema)
        with self.assertRaises(ValueError):
            validate_output(delta(events=[dict(event, source_caption_ids=['C1'])]), schema)
        from lmformatenforcer import JsonSchemaParser
        parser = JsonSchemaParser(decoder_schema(schema))
        parser.config.force_json_field_order = True
        for char in json.dumps(dict(events=[event], unresolved='')):
            self.assertIn(char, parser.get_allowed_characters())
            parser = parser.add_character(char)
        self.assertTrue(parser.can_end())

    def test_entity_stage_cannot_emit_events_referencing_uncommitted_entities(self):
        sources = caps(['Alice opens the box.'])
        inp = dict(captions=sources, entities=[], fields=['action'], depth='event', entity_stage=True)
        r = delta(events=[dict(event_ref='new', kind='action', action=span(sources, 'C0', 'opens'),
            roles=dict(actor='B0'), source_caption_ids=['C0'], basis='caption_explicit')])
        with self.assertRaises(ValueError):
            validate_output(r, output_schema('optimized_extract', inp))

    def test_complete_action_anchor_excludes_the_caption_tag_and_survives_commit(self):
        sources = caps(['#C C puts the onion in the bowl.'])
        actions = memory.event_spans(sources)
        self.assertEqual([s['text'] for s in actions.values()], ['C puts the onion in the bowl'])
        action = next(iter(actions))
        m = fresh(sources)
        commit(m, delta(events=[dict(event_ref='new', kind='action', action=dict(span_id=action),
               roles={}, source_caption_ids=['C0'], basis='caption_explicit')]))
        self.assertEqual(m['events'][0]['action'], 'C puts the onion in the bowl')
        self.assertEqual(m['events'][0]['roles'], {})
        self.assertEqual(m['entities'], {})

    def test_conflicting_optional_roles_are_audited_without_losing_the_source_action(self):
        sources = caps(['Alice opens the box.'])
        m = fresh(sources)
        event = dict(event_ref='new', kind='action', action=span(sources, 'C0', 'opens'),
            roles=dict(actor='B0', item='B0', tool='B0'), source_caption_ids=['C0'], basis='caption_explicit')
        commit(m, delta(entity(sources, 'Alice'), events=[event]))
        self.assertEqual(len(m['events']), 1)
        self.assertEqual(m['events'][0]['roles'], {})
        self.assertEqual(m['events'][0]['unresolved_roles'], ['actor', 'item', 'tool'])
        self.assertEqual([r['kind'] for r in m['rejected_fields']], ['event_role']*3)

    def test_spans_recover_original_spacing_and_reject_foreign_and_reversed_offsets(self):
        sources = caps(['Alice holds a red  metal box.'])
        self.assertEqual(memory.recover_span(span(sources, 'C0', 'red  metal'), {'C0': sources[0]}), 'red  metal')
        m = fresh(sources)
        for broken in (dict(caption_id='C99', start_token=0, end_token=1),
                       dict(caption_id='C0', start_token=3, end_token=2)):
            r = delta(entity(sources, 'box'))
            r['entities'][0]['mentions'] = [broken]
            before = deepcopy(m)
            with self.assertRaises(ValueError):
                commit(m, r)
            self.assertEqual(m, before)

    def test_decoder_handles_reference_spans_and_restores_forced_empty_fields(self):
        from lmformatenforcer import JsonSchemaParser
        sources = caps(['Alice holds a red box.'])
        inp = dict(captions=memory.source_view(sources), entities=[], fields=['state'], depth='entity', page_size=3)
        r = delta(entity(sources, 'box'))
        del r['entities'][0]['attributes']
        del r['events']
        schema = output_schema('optimized_extract', inp)
        parser = JsonSchemaParser(decoder_schema(schema))
        parser.config.force_json_field_order = True
        for char in json.dumps(r):
            self.assertIn(char, parser.get_allowed_characters())
            parser = parser.add_character(char)
        self.assertTrue(parser.can_end())
        restore_empty_arrays(r, schema)
        validate_output(r, schema)

    def test_bad_optional_cause_is_quarantined_without_losing_entity_or_event(self):
        sources = caps(['Alice opens the box.', 'Bob holds a knife.'])
        m = fresh(sources)
        e = entity(sources, 'box', kind='action')
        e['observations'][0]['cause'] = dict(tool=span(sources, 'C1', 'knife'))
        commit(m, delta(e), depth='entity')
        self.assertEqual(len(m['entities']), 1)
        self.assertEqual(len(m['observations']), 1)
        self.assertEqual(m['rejected_fields'][0]['kind'], 'cause')
        self.assertNotIn('cause', m['observations']['O0'])

    def test_mismatching_secondary_mention_does_not_merge_other_objects_or_abort_valid_anchor(self):
        sources = caps(['Alice holds a box.', 'Bob holds a knife.'])
        m = fresh(sources)
        e = entity(sources, 'box')
        e['mentions'].append(span(sources, 'C1', 'knife'))
        e['observations'].append(dict(caption_id='C1', kind='state', continuity='same'))
        commit(m, delta(e), depth='entity')
        self.assertEqual(m['entities']['E0']['label'], 'box')
        self.assertEqual(m['entities']['E0']['mention_caption_ids'], ['C0'])
        self.assertEqual(len(m['observations']), 1)
        self.assertEqual({r['kind'] for r in m['rejected_fields']}, {'mention', 'observation'})

    def test_decoder_binds_existing_entity_to_its_own_literal_mention_choices(self):
        sources = caps(['Alice holds a box beside a knife.'])
        inp = dict(captions=sources, entities=[dict(id='E0', label='box')], fields=['state'], depth='entity')
        schema = output_schema('optimized_extract', inp)
        good = delta(entity(sources, 'box', ref='E0'))
        validate_output(good, schema)
        bad = delta(entity(sources, 'knife', ref='E0'))
        with self.assertRaises(ValueError):
            validate_output(bad, schema)

    def test_wrong_optional_attribute_span_is_logged_and_other_facts_survive(self):
        sources = caps(['Alice holds a red box.', 'Bob wears blue clothing.'])
        m = fresh(sources)
        e = entity(sources, 'box')
        e['attributes'] = [dict(field='color', value=span(sources, 'C1', 'blue'))]
        commit(m, delta(e), fields=['color'])
        self.assertEqual(m['entities']['E0']['attributes'], [])
        self.assertEqual(m['rejected_fields'][0]['kind'], 'attribute')

    def test_foreign_optional_span_is_still_a_hard_atomic_failure(self):
        sources = caps(['Alice holds a box.'])
        m = fresh(sources)
        e = entity(sources, 'box')
        e['observations'][0]['cause'] = dict(tool=dict(caption_id='C99', start_token=0, end_token=1))
        before = deepcopy(m)
        with self.assertRaises(ValueError):
            commit(m, delta(e))
        self.assertEqual(m, before)

    def test_repeated_same_action_is_two_events_and_repeat_extraction_is_idempotent(self):
        sources = caps(['Alice gives the box to Bob.', 'Bob holds the box.', 'Alice gives the box to Bob.'])
        m = fresh(sources)
        def ev(cid):
            return dict(event_ref='new', kind='transfer', action=span(sources, cid, 'gives'),
                roles=dict(giver='B0', receiver='B1', item='B2'), source_caption_ids=[cid], basis='caption_explicit')
        r = delta(entity(sources, 'Alice', ('C0', 'C2')), entity(sources, 'Bob', ('C0', 'C2')),
                  entity(sources, 'box', ('C0', 'C2')), events=[ev('C0'), ev('C2')])
        commit(m, r)
        self.assertEqual(len(m['events']), 2)
        self.assertEqual([e['action_interval'] for e in m['events']], [[0, 1], [2, 3]])
        # Refill uses existing local IDs; exact source occurrences do not multiply.
        for i, e in enumerate(r['entities']):
            e['entity_ref'] = f'E{i}'
        commit(m, r, key='repeat')
        self.assertEqual(len(m['events']), 2)

    def test_possession_transfer_hypothesis_does_not_become_observed_handover(self):
        sources = caps(['Alice holds the box beside Bob.'])
        m = fresh(sources)
        ev = dict(event_ref='new', kind='transfer', action=span(sources, 'C0', 'holds'),
            roles=dict(giver='B0', receiver='B1', item='B2'), source_caption_ids=['C0'], basis='inferred_from_states')
        commit(m, delta(entity(sources, 'Alice'), entity(sources, 'Bob'), entity(sources, 'box'), events=[ev]))
        self.assertEqual(m['events'][0]['status'], 'unresolved_hypothesis')
        self.assertIsNone(m['events'][0]['action_interval'])

    def test_disjoint_relation_occurrences_do_not_erase_time_and_adjacent_support_can_compress(self):
        sources = caps(['Alice holds the box.', 'Alice holds the box.', 'Alice walks away.', 'Alice holds the box.'])
        m = fresh(sources)
        r = delta(entity(sources, 'Alice', ('C0', 'C1', 'C3')), entity(sources, 'box', ('C0', 'C1', 'C3')),
            relations=[dict(subject='B0', object='B1', predicate=span(sources, cid, 'holds'), update='present') for cid in ('C0', 'C1', 'C3')])
        commit(m, r)
        self.assertEqual(len(m['relation_records']), 3)
        self.assertEqual([r['supported_intervals'] for r in m['temporal_network']['relations']], [[[0, 2]], [[3, 4]]])

    def test_same_state_does_not_fill_a_missing_interval_and_returning_state_is_new(self):
        sources = caps(['box is closed.', 'box is open.', 'box is closed.'])
        m = fresh(sources)
        commit(m, delta(entity(sources, 'box')), cids=['C0'])
        commit(m, delta(entity(sources, 'box', ('C2',), 'E0', 'same')), cids=['C2'])
        self.assertEqual(len(m['temporal_network']['states']), 1)
        self.assertEqual(m['temporal_network']['states']['T0']['supported_intervals'], [[0, 1]])
        # An earlier backfill is rebuilt in video time, not extraction order.
        commit(m, delta(entity(sources, 'box', ('C1',), 'E0', 'changed')), cids=['C1'])
        commit(m, delta(entity(sources, 'box', ('C2',), 'E0', 'changed')), cids=['C2'])
        self.assertEqual(len(m['temporal_network']['states']), 3)
        self.assertEqual(len(m['temporal_network']['transitions']), 2)

    def test_identity_is_sparse_revisable_and_cannot_silently_use_unread_identity_evidence(self):
        sources = caps(['Alice holds the box.', 'Alice opens the box.'])
        m = fresh(sources, split=1)
        commit(m, delta(entity(sources, 'box')), cids=['C0'])
        commit(m, delta(entity(sources, 'box', ('C1',))), cids=['C1'], sid='S1')
        m['cross_matches'] = [dict(id='I0', left_entity='E0', right_entity='E1', decision='same',
            relation='same_instance', caption_ids=['C0', 'C1'], active=True, status='text_inferred')]
        memory.rebuild(m)
        self.assertEqual(len(m['indexes']['entity_occurrences']), 1)
        self.assertEqual(sum(map(len, m['indexes']['entity_occurrences'].values())), 2)
        view = memory.context(m, dict(question='box'), {'C0'}, dict(scope='local', met=True))
        self.assertEqual(view['states'][0]['entity_id'], 'E0')
        memory.revoke_identity(m, 'I0', 'conflicting later evidence')
        self.assertEqual(len(m['temporal_network']['entities']), 2)
        self.assertEqual(len(m['indexes']['entity_occurrences']), 2)
        self.assertEqual(m['events'], [])

    def test_disk_reload_refill_keeps_sources_and_old_observations_and_detects_corruption(self):
        sources = caps(['Alice holds a red box.'])
        m = fresh(sources)
        commit(m, delta(entity(sources, 'box')), depth='entity')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'memory.json'
            write_json(path, m)
            restored = memory.load_memory(path, 'v', sources, 'hash', directory(sources))
            e = entity(sources, 'box', ref='E0')
            e['attributes'] = [dict(field='color', value=span(sources, 'C0', 'red'))]
            commit(restored, delta(e), fields=['color'], depth='entity', key='refill')
            self.assertEqual(len(restored['observations']), 1)
            self.assertEqual(restored['entities']['E0']['attributes'][0]['value'], 'red')
            restored['indexes']['entity_occurrences'] = {}
            write_json(path, restored)
            with self.assertRaises(ValueError):
                memory.load_memory(path, 'v', sources, 'hash', directory(sources))

    def test_unknown_identity_verification_is_cached_across_wording_but_revocation_invalidates_it(self):
        sources = caps(['Alice holds the box.', 'Alice opens the box.', 'Alice closes the box.'])
        m = fresh(sources, split=1)
        commit(m, delta(entity(sources, 'box')), cids=['C0'])
        commit(m, delta(entity(sources, 'box', ('C1',))), cids=['C1'], sid='S1')
        meter = Mock()
        meter.json.return_value = (dict(matches=[dict(left=dict(entity='E0', caption_ids=['C0']),
            right=dict(entity='E1', caption_ids=['C1']), relation='unknown', reason='ambiguous')]), {})
        memory.align_needed(m, 'S0', dict(question='Where is the box?'), 'identity', meter)
        memory.align_needed(m, 'S0', dict(question='What color is the box?'), 'different wording', meter)
        self.assertEqual(meter.json.call_count, 1)
        self.assertEqual(len(m['temporal_network']['entities']), 2)
        self.assertEqual(m['cross_matches'][0]['status'], 'unresolved')
        memory.revoke_identity(m, 'I0', 'new conflict')
        memory.align_needed(m, 'S0', dict(question='Where?'), '', meter)
        self.assertEqual(meter.json.call_count, 2)


class RetrievalTests(unittest.TestCase):
    def test_lexical_retrieval_recovers_rare_object_without_claiming_identity(self):
        sources = caps(['A wall.', 'A rare wrench.'])
        d = directory(sources, split=1)
        sampler = HybridSampler(d, [[1., 0.]]*2, [1., 0.], 'Where is the wrench?')
        seen, opened = set(), []
        self.assertEqual(sampler.rank_scenes()[0]['scene_id'], 'S1')
        self.assertEqual(sampler.read_batch(seen, opened, count=1), ['C1'])
        self.assertEqual(sampler.postings['wrench'], ['C1'])

    def test_temporal_window_crosses_scene_boundary_without_exceeding_read_budget(self):
        sources = caps(['wall', 'wall', 'Alice opens a box', 'box is open', 'wall', 'wall'])
        sampler = HybridSampler(directory(sources, split=3), [[1., 0.]]*6, [1., 0.], 'Who opens the box?')
        seen, opened = set(), []
        selected = sampler.read_batch(seen, opened, count=3, temporal=True, max_scenes=2)
        self.assertEqual(set(selected), {'C1', 'C2', 'C3'})
        self.assertEqual(len(opened), 2)
        self.assertFalse(sampler.coverage(dict(scope='exhaustive'), seen)['met'])

    def test_ordinal_last_and_count_force_exhaustive_coverage(self):
        for text in ('Who receives the box the second time?', 'When is the last opening?', 'How many times?', '第二次交给谁？'):
            self.assertEqual(required_scope(text), 'exhaustive')


class PipelineTests(unittest.TestCase):
    def test_event_extraction_sees_committed_entity_ids_instead_of_future_page_aliases(self):
        from script_core.budget import BudgetLedger, Meter
        sources = caps(['Alice opens the box.'])
        m = fresh(sources)
        calls = []
        class Backend:
            def call(self, op, inp, timeout):
                calls.append((op, deepcopy(inp)))
                if op == 'optimized_extract':
                    return dict(data=delta(entity(sources, 'Alice'), entity(sources, 'box')))
                if op == 'optimized_events':
                    ids = {e['label']: e['id'] for e in inp['entities']}
                    action = next(iter(memory.event_spans(sources)))
                    return dict(data=delta(events=[dict(event_ref='new', kind='action',
                        action=dict(span_id=action), roles=dict(actor=ids['Alice'], item=ids['box']),
                        source_caption_ids=['C0'], basis='caption_explicit')]))
                raise AssertionError(op)
        with tempfile.TemporaryDirectory() as tmp:
            meter = Meter(Backend(), BudgetLedger(600), load_config(), tmp)
            try:
                changed = pipeline.extract_batch(m, sources, 'S0', dict(question='What opens?', options=[]),
                    dict(gap='opening event', focus=['Alice', 'box'], fields=['action'], depth='event'), meter, set())
            finally:
                meter.close()
        self.assertTrue(changed)
        self.assertEqual([op for op, inp in calls], ['optimized_extract', 'optimized_events'])
        self.assertEqual(m['events'][0]['roles'], dict(actor='E0', item='E1'))
        self.assertEqual(m['rejected_fields'], [])

    def run_case(self, question, plan_factory, texts=None, max_plans=4):
        sources = caps(texts or ['Alice holds a red box.']*20)
        packet = dict(captions=sources, frame_features=[[1., 0.]]*len(sources),
            question=dict(question_id='q', video_id='v', question=question, options=['a', 'b', 'c', 'd', 'e']))
        cfg = load_config()
        cfg['query']['temporal_sparse_visual'] = False
        calls = []
        class Backend:
            def call(self, op, inp, timeout):
                calls.append((op, deepcopy(inp)))
                if op == 'clip_text':
                    return dict(vectors=[[1., 0.]])
                if op == 'optimized_plan':
                    if inp.get('source_type') != 'caption':
                        raise AssertionError('Text planning must not receive the backend visual-source instruction')
                    return dict(data=plan_factory(inp))
                if op == 'optimized_extract':
                    cid = inp['captions'][0]['id']
                    local = inp['entities']
                    return dict(data=delta(entity(sources, 'box', (cid,), local[0]['id'] if local else 'new')))
                if op == 'optimized_events':
                    return dict(data=delta())
                if op == 'temporal_qa_check':
                    return dict(input_tokens=100)
                if op == 'videotree_qa':
                    return dict(data=dict(selected_option='0'))
                raise AssertionError(op)
        with tempfile.TemporaryDirectory() as tmp:
            r = pipeline.run(packet, directory(sources), [], Backend(), cfg, Path(tmp),
                dict(online_seconds=600, max_scene_visits=4, optimized_max_plans=max_plans,
                     optimized_initial_captions=2, optimized_batch_captions=3))
            stored = json.loads((Path(tmp)/'network.json').read_text())
        return r, calls, stored

    @staticmethod
    def plan(inp, **changes):
        p = dict(scope='local', depth='direct', sufficient=True, reason='explicit source', gap='',
            support_caption_ids=[inp['captions'][0]['id']], scene_ids=[], caption_ids=[],
            entity_ids=[], focus=[], fields=[], need_identity=False)
        p.update(changes)
        return p

    def test_simple_question_bypasses_graph_and_never_passes_unread_text_to_qa(self):
        texts = ['Alice holds a red box.', 'A blue cabinet.', 'UNREAD SECRET TEXT']
        r, calls, m = self.run_case('What color is the box?', self.plan, texts=texts)
        self.assertIsNone(r['error'], r['error'])
        self.assertEqual(r['planning_calls'], 1)
        self.assertEqual(r['entity_count'], 0)
        self.assertFalse(any(op in ('optimized_extract', 'scene_correspondence_lineage') for op, _ in calls))
        qa = next(inp for op, inp in calls if op == 'videotree_qa')
        self.assertNotIn('UNREAD SECRET TEXT', qa['prompts'][-1])
        self.assertEqual(m['source_captions']['C2']['text'], 'UNREAD SECRET TEXT')

    def test_non_direct_depth_extracts_without_automatic_identity_call(self):
        def planner(inp):
            return self.plan(inp, depth='entity', caption_ids=[inp['captions'][0]['id']], fields=['state'], focus=['box'])
        r, calls, m = self.run_case('What happens to the box?', planner)
        self.assertIsNone(r['error'], r['error'])
        self.assertEqual(r['entity_count'], 1)
        self.assertEqual(r['planning_calls'], 2)
        self.assertFalse(any(op == 'scene_correspondence_lineage' for op, _ in calls))

    def test_planning_is_bounded_and_unmet_exhaustive_coverage_is_reported(self):
        def planner(inp):
            return self.plan(inp, sufficient=False, gap='unseen events', depth='direct')
        r, calls, _ = self.run_case('How many times does Alice open the box?', planner, max_plans=2)
        self.assertIsNone(r['error'], r['error'])
        self.assertEqual(r['planning_calls'], 2)
        self.assertEqual(r['extraction_depth'], 'event')
        self.assertFalse(r['coverage_check']['met'])
        self.assertTrue(any(i['stage'] == 'coverage' for i in r['incomplete']))
        self.assertLessEqual(r['read_caption_count'], 8)

    def test_planner_confidence_does_not_stop_sampling_before_requested_events_exist(self):
        def planner(inp):
            return self.plan(inp, sufficient=True, depth='event', fields=['action'], focus=['box'])
        r, calls, _ = self.run_case('Describe the action with the box.', planner, max_plans=2)
        self.assertIsNone(r['error'], r['error'])
        self.assertGreater(r['read_caption_count'], 2)
        self.assertEqual(r['planning_calls'], 2)
        self.assertTrue(any(i['stage'] == 'requested_structure' for i in r['incomplete']))


class ComparisonRunnerTests(unittest.TestCase):
    def test_same_evidence_direct_qa_keeps_question_options_frames_and_supplement_but_omits_memory(self):
        from script import run_optimized_comparison as runner
        sources = caps(['Alice holds a box.', 'UNREAD SOURCE'])
        packet = dict(captions=sources, question=dict(question_id='q', video_id='v',
            question='Who holds the box?', options=['Alice', 'Bob', 'C', 'D', 'E']))
        calls = []
        class Backend:
            def call(self, operation, inputs, timeout):
                calls.append((operation, deepcopy(inputs)))
                if operation == 'temporal_qa_check':
                    return dict(input_tokens=100)
                if operation == 'videotree_qa':
                    return dict(data=dict(selected_option='0'))
                raise AssertionError(operation)
        result = dict(selected_seconds=[0], answer=dict(selected_option='0'),
            supplemental_visual=dict(observations=[dict(description='LOCAL VISIBLE DETAIL')]))
        with tempfile.TemporaryDirectory() as tmp:
            runner.same_evidence_ablation(Path(tmp), 'v', 'smoke', packet, result, Backend(), load_config())
            stored = json.loads((Path(tmp)/'smoke/ablation/v/result.json').read_text())
        self.assertIsNone(stored['error'], stored['error'])
        qa = next(inp for op, inp in calls if op == 'videotree_qa')
        self.assertIn('Alice holds a box', qa['prompts'][-1])
        self.assertIn('LOCAL VISIBLE DETAIL', qa['prompts'][-1])
        self.assertNotIn('UNREAD SOURCE', qa['prompts'][-1])
        self.assertNotIn('event/state memory', qa['prompts'][-1])
        self.assertEqual(qa['query_id'], 'q')

    def test_report_counts_failed_control_in_denominator_and_keeps_evaluation_cost_separate(self):
        from script import run_optimized_comparison as runner
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_json(out/'cohort.json', ['v'])
            write_json(out/'evaluation_only.json', {'v': 0})
            for arm in runner.ARMS:
                write_json(out/'formal/arms'/arm/'runs/v/result.json', dict(question_id='v',
                    answer=None if arm == 'adaptive_scheme4' else dict(selected_option='0'),
                    error=dict(message='failed') if arm == 'adaptive_scheme4' else None,
                    total_measured_seconds=10, read_caption_count=2, reviews=[], incomplete=[]))
            runner.report(out)
            report_ = json.loads((out/'report.json').read_text())
        self.assertEqual(report_['arms']['adaptive_scheme4']['completed'], 1)
        self.assertEqual(report_['arms']['adaptive_scheme4']['errors'], 1)
        self.assertEqual(report_['arms']['adaptive_scheme4']['correct'], 0)
        self.assertEqual(report_['arms']['optimized_scheme4']['correct'], 1)
        self.assertTrue(report_['ablation_cost_separate'])


if __name__ == '__main__':
    unittest.main()
