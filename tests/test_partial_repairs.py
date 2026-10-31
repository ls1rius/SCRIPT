"""Regressions for endpoint, source and planner failures from the 300-question run."""
from copy import deepcopy
import tempfile
import unittest

from script_core import adaptive_transition as partial
from script_core.budget import BudgetLedger, Meter
from script_core.config import load_config
from script_core.schemas import validate_output


class PartialRepairTests(unittest.TestCase):
    def setUp(self):
        self.captions = [dict(id='C0', interval=[0, 1], text='C picks the knife.'),
                         dict(id='C1', interval=[1, 2], text='C cuts the onion.'),
                         dict(id='C2', interval=[2, 3], text='The onion is red.')]
        self.memory = partial.new_memory('v', self.captions, 'h')
        partial.rebuild_indexes(self.memory)

    def entity(self, label, refs, observations=(), ref='new'):
        return dict(entity_ref=ref, label=label, source_caption_ids=refs,
                    attributes=[], observations=[dict(caption_id=c, kind='action') for c in observations])

    def commit(self, rows, relations=(), **request):
        return partial.commit_delta(self.memory, dict(entities=rows, relations=list(relations), unresolved=''),
            self.captions, 'S0', dict(request_key='r', fields=['state', 'relation'], **request), 'call')

    def test_self_relation_is_rejected_without_losing_the_valid_entity(self):
        result = self.commit([self.entity('C', ['C0'], ['C0'])],
            [dict(subject='B0', object='B0', predicate='picks', source_caption_ids=['C0'])])
        self.assertEqual(result['rejected_fields'], 1)
        self.assertEqual(len(self.memory['entities']), 1)
        self.assertEqual(len(self.memory['observations']), 1)
        self.assertEqual(self.memory['relations'], [])
        self.assertEqual(self.memory['rejected_fields'][0]['call_id'], 'call')

    def test_missing_observation_mention_is_added_only_when_source_mentions_the_entity(self):
        self.commit([self.entity('C', ['C0'], ['C1', 'C2'])])
        self.assertEqual(self.memory['entities']['E0']['mention_caption_ids'], ['C0', 'C1'])
        self.assertEqual([o['caption_id'] for o in self.memory['observations'].values()], ['C1'])
        self.assertEqual(self.memory['rejected_fields'][0]['kind'], 'observation')

    def test_bad_first_entity_does_not_reassign_its_page_alias_to_valid_second_entity(self):
        self.commit([self.entity('onion', ['C0']), self.entity('C', ['C0'])],
            [dict(subject='B0', object='B1', predicate='picks', source_caption_ids=['C0'])])
        self.assertEqual([e['label'] for e in self.memory['entities'].values()], ['C'])
        self.assertEqual(self.memory['relations'], [])

    def test_literal_predicate_and_both_endpoint_support_remain_required(self):
        self.commit([self.entity('C', ['C0']), self.entity('knife', ['C0'])],
            [dict(subject='B0', object='B1', predicate='picks', source_caption_ids=['C0']),
             dict(subject='B0', object='B1', predicate='uses', source_caption_ids=['C0']),
             dict(subject='B0', object='B1', predicate='cuts', source_caption_ids=['C1'])])
        self.assertEqual([r['predicate'] for r in self.memory['relations']], ['picks'])
        self.assertEqual(len(self.memory['rejected_fields']), 2)

    def test_foreign_source_and_changed_source_text_are_atomic_hard_failures(self):
        before = deepcopy(self.memory)
        with self.assertRaises(ValueError):
            self.commit([self.entity('C', ['FOREIGN'])])
        self.assertEqual(self.memory, before)
        changed = deepcopy(self.captions)
        changed[0]['text'] = 'C picks the onion.'
        with self.assertRaises(ValueError):
            partial.commit_delta(self.memory, dict(entities=[self.entity('C', ['C0'])], relations=[], unresolved=''),
                                 changed, 'S0', {'request_key': 'r'}, 'call')
        self.assertEqual(self.memory, before)

    def test_decoder_disallows_self_endpoints_and_unrequested_relations(self):
        inp = dict(captions=self.captions, entities=[], page_size=3, fields=['relation'])
        report = dict(entities=[], relations=[dict(subject='B0', object='B0',
                        predicate='picks', source_caption_ids=['C0'])], unresolved='')
        with self.assertRaises(ValueError):
            validate_output(report, partial.output_schema('transition_extract', inp))
        report['relations'][0]['object'] = 'B1'
        validate_output(report, partial.output_schema('transition_extract', inp))
        with self.assertRaises(ValueError):
            validate_output(report, partial.output_schema('transition_extract', dict(inp, fields=['state'])))

    def test_malformed_planner_regenerates_from_original_inputs_without_copying_broken_output(self):
        calls = []
        plan = dict(sufficient=True, reason='enough', gap='', action='stop', scene_id='stop',
                    caption_ids=[], entity_ids=[], focus=[], fields=[])
        class Backend:
            def call(self, op, inp, timeout):
                calls.append((op, deepcopy(inp)))
                return {'data': '{"reason":"unfinished' if len(calls) == 1 else plan}
        with tempfile.TemporaryDirectory() as folder:
            meter = Meter(Backend(), BudgetLedger(100), load_config(), folder)
            try:
                result, _ = meter.json('transition_next', dict(captions=[], entities=[], allowed_scene_ids=[]), tuple(plan))
            finally:
                meter.close()
        self.assertEqual(result, plan)
        self.assertEqual([op for op, _ in calls], ['transition_next', 'transition_next'])
        self.assertIn('validation_feedback', calls[1][1])
        self.assertNotIn('original_output', calls[1][1])


if __name__ == '__main__':
    unittest.main()
