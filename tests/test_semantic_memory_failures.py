"""Failure and budget accounting for independent semantic category extraction."""
from collections import defaultdict
from copy import deepcopy
import unittest

from script_core.budget import ResourceExhausted, ToolDeadlineExceeded, ToolFailure
from script_core.semantic_memory import COLLECTIONS, ROLES, normalize, view
from tests.test_normalized_memory import unit


def bundle(count=9):
    units = []
    for index in range(count):
        source = unit('C wipes the book with a cloth.', f'U{index}')
        source['source_caption_ids'] = [f'C{index}']
        units.append(source)
    return dict(units=units, read_caption_ids=[f'C{i}' for i in range(count)])


class Meter:
    def __init__(self, error=None, category='state'):
        self.error = error
        self.category = category
        self.calls = []

    def json(self, operation, inputs, required):
        self.calls.append(deepcopy(inputs))
        if inputs['category'] == self.category and self.error:
            raise self.error
        return dict(facts=[], uncertain_source_ids=[]), {}


class SemanticMemoryFailureTests(unittest.TestCase):
    def role_review(self, corrected=True, error=None):
        class ReviewMeter(Meter):
            def json(self, operation, inputs, required):
                self.calls.append(deepcopy(inputs))
                if inputs['category'] != 'action':
                    return dict(facts=[], uncertain_source_ids=[]), {}
                reviewing='role_review' in inputs
                if reviewing and error:raise error
                fact=dict(source_id='U0',actor='C',verb='draws',**dict.fromkeys(ROLES))
                fact['instrument']='pencil'
                fact['surface' if reviewing and corrected else 'material']='drawing book'
                return dict(facts=[fact],uncertain_source_ids=[]), {}
        source=bundle(1)
        source['units'][0]['text']='C draws on the drawing book with a pencil.'
        meter=ReviewMeter()
        memory,audit=normalize(source,meter)
        return source,meter,memory,audit

    def test_invalid_role_gets_one_source_bound_review_and_keeps_original(self):
        source,meter,memory,audit=self.role_review()
        self.assertEqual(audit['role_review_calls'],1)
        self.assertEqual(len(meter.calls),8)
        self.assertEqual(meter.calls[0]['units'],meter.calls[1]['units'])
        self.assertIn('role_review',meter.calls[1])
        self.assertEqual(memory['actions'][0]['surface'],'drawing book')
        self.assertNotIn('material',memory['actions'][0])
        self.assertEqual(memory['residuals'][0]['text'],source['units'][0]['text'])
        self.assertEqual(audit['rejected_records'][0]['stage'],'before_role_review')
        self.assertTrue(all(c['checked_units']==1 for c in audit['category_coverage'].values()))

    def test_unsuccessful_role_review_does_not_loop_or_relabel_the_fact(self):
        source,meter,memory,audit=self.role_review(corrected=False)
        self.assertEqual(audit['role_review_calls'],1)
        self.assertEqual(len(meter.calls),8)
        self.assertEqual(memory['actions'],[])
        self.assertEqual(memory['residuals'][0]['text'],source['units'][0]['text'])

    def test_exhausted_role_review_stops_remaining_calls_and_preserves_source(self):
        source,meter,memory,audit=self.role_review(error=ResourceExhausted('review budget ended'))
        self.assertEqual(len(meter.calls),2)
        self.assertEqual(memory['actions'],[])
        self.assertEqual(audit['coverage']['covered_captions'],1)
        self.assertTrue(audit['batch_failures'])

    def test_every_category_checks_every_unit_once_across_batch_boundaries(self):
        source = bundle()
        before = deepcopy(source)
        meter = Meter()
        memory, audit = normalize(source, meter)
        checked = defaultdict(list)
        for call in meter.calls:
            self.assertEqual(set(call), {'category', 'units', 'source_type'})
            checked[call['category']].extend(u['id'] for u in call['units'])
            self.assertEqual(len(call['units']), 1)
        expected = [u['id'] for u in source['units']]
        self.assertEqual(set(checked), set(COLLECTIONS))
        for category in COLLECTIONS:
            self.assertEqual(checked[category], expected)
            self.assertEqual(audit['category_coverage'][category],
                             dict(checked_units=9, failed_units=0, unattempted_units=0))
        self.assertEqual(source, before)
        self.assertEqual(len(view(memory)['observations']), 9)

    def test_invalid_category_output_does_not_erase_sources_or_skip_other_categories(self):
        meter = Meter(error=ValueError('invalid state JSON'))
        memory, audit = normalize(bundle(2), meter)
        self.assertEqual(audit['coverage']['covered_captions'], 2)
        self.assertEqual(audit['category_coverage']['state'],
                         dict(checked_units=0, failed_units=2, unattempted_units=0))
        self.assertEqual(audit['category_coverage']['event_link']['checked_units'], 2)
        self.assertEqual(len(audit['batch_failures']), 2)
        self.assertEqual(audit['batch_failures'][0]['category'], 'state')
        self.assertEqual(len(view(memory)['observations']), 2)

    def test_budget_exhaustion_retains_all_unattempted_sources_and_stops_calls(self):
        for error in (ResourceExhausted('no exploration allowance'),
                      ToolDeadlineExceeded('budget ended', operation='semantic_memory_extract',
                                           budget_limited=True, worker_reusable=True)):
            with self.subTest(error=type(error).__name__):
                meter = Meter(error=error, category='action')
                memory, audit = normalize(bundle(), meter)
                self.assertEqual(len(meter.calls), 1)
                self.assertEqual(audit['category_coverage']['action'],
                                 dict(checked_units=0, failed_units=1, unattempted_units=8))
                for category in set(COLLECTIONS) - {'action'}:
                    self.assertEqual(audit['category_coverage'][category],
                                     dict(checked_units=0, failed_units=0, unattempted_units=9))
                self.assertEqual(audit['coverage']['covered_captions'], 9)
                self.assertEqual(len(view(memory)['observations']), 9)
                self.assertTrue(all(r['information_types'] == ['unclassified']
                                    for r in memory['residuals']))

    def test_infrastructure_failures_propagate_instead_of_becoming_empty_categories(self):
        for error in (ToolFailure('CUDA out of memory'),
                      ToolFailure('worker terminated'),
                      ToolDeadlineExceeded('full tool timeout', operation='semantic_memory_extract',
                                           budget_limited=False, worker_reusable=True)):
            with self.subTest(error=str(error)):
                with self.assertRaises(ToolFailure):
                    normalize(bundle(2), Meter(error=error))


if __name__ == '__main__':
    unittest.main()
