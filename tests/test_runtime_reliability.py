"""Regressions for the 21 observed v2 runtime failures, independent of labels."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_script_core import FixtureBackend, fixture_base, fixture_memory
from script_core.budget import BudgetLedger, Meter, ToolFailure, ResourceExhausted
from script_core.accounting import cpu_phase
from script_core.config import DEFAULTS
from script_core.controller import answer_query, load_plan
from script_core.query_policy import normalize_plan
from script_core.schemas import output_schema, validate_output, decoder_schema, restore_empty_arrays


class RuntimeReliabilityTests(unittest.TestCase):
    def test_recorded_undefined_variables_are_declared_without_binding_or_scope_change(self):
        cases = json.loads((Path(__file__).parent/'fixtures/pilot_runtime_failures.json').read_text())
        self.assertEqual(len(cases), 21)
        plans = [case for case in cases if 'plan' in case]
        self.assertEqual(len(plans), 2)
        for case in plans:
            # Exercise symbol repair independently of the newer overview routing.
            plan = normalize_plan(case['plan'], dict(case, question='Which object does the person touch?'))
            base = fixture_memory(180.)
            variables = load_plan(base, {'id': 'q', 'scope': case['scope']}, plan)
            self.assertEqual(len({v['name'] for v in variables}), len(variables))
            self.assertFalse(base.tables['bindings'])
            for original, normalized in zip(case['plan']['requirements'], plan['requirements']):
                self.assertEqual(original, normalized)
            self.assertFalse(any(base.is_valid(r) for r in base.tables['requirements']))

    def test_empty_evidence_arrays_cannot_generate_a_sentinel_reference(self):
        from lmformatenforcer import JsonSchemaParser
        schema = output_schema('answer', {'answer_mode': 'choice', 'option_ids': ['0', '1']})
        good = {'selected_option': '1', 'text': 'Evidence insufficient.', 'claim_ids': [], 'evidence_ids': []}
        validate_output(good, schema)
        for field in ('claim_ids', 'evidence_ids'):
            bad = dict(good, **{field: ['__no_allowed_reference__']})
            with self.assertRaises(ValueError):
                validate_output(bad, schema)
            parser = JsonSchemaParser(decoder_schema(schema))
            # Check the actual constrained decoder, not just the post-validator.
            for char in json.dumps(bad):
                if char not in parser.get_allowed_characters():
                    break
                parser = parser.add_character(char)
            else:
                self.fail('Decoder allowed a fabricated empty-allowlist reference')
        decoded = {'selected_option': '1', 'text': 'Evidence insufficient.'}
        parser = JsonSchemaParser(decoder_schema(schema))
        for char in json.dumps(decoded):
            self.assertIn(char, parser.get_allowed_characters())
            parser = parser.add_character(char)
        self.assertTrue(parser.can_end())
        self.assertEqual(restore_empty_arrays(decoded, schema), good)

    def test_forced_choice_without_verified_facts_still_finishes_as_insufficient(self):
        base, meta = fixture_base()
        class NoFacts(FixtureBackend):
            def call(self, operation, inputs, timeout):
                if operation == 'observe':
                    return {'data': {'claims': [], 'uncertainties': []}}
                return super().call(operation, inputs, timeout)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'What is happening overall?', 'options': ['a', 'b']},
                meta, deepcopy(DEFAULTS), NoFacts(base), folder)
        self.assertIsNone(result['error'])
        self.assertEqual(result['answer']['selected_option'], '0')
        self.assertEqual(result['answer']['claim_ids'], [])
        self.assertEqual(result['answer']['evidence_ids'], [])
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')

    def test_exhausted_exploration_can_pay_for_cold_final_worker_and_answer(self):
        clock = [100.]
        class ColdBackend:
            requires_vlm_warmup = True
            def call(self, operation, inputs, timeout):
                if operation == 'prepare_vlm':
                    clock[0] += 15.
                    self.requires_vlm_warmup = False
                    return {'ready': True}
                clock[0] += 5.
                return {'data': {'selected_option': '1', 'text': 'answer', 'claim_ids': [], 'evidence_ids': []}}
        with tempfile.TemporaryDirectory() as folder, patch('script_core.budget.time.monotonic', side_effect=lambda: clock[0]):
            ledger = BudgetLedger(600.)
            ledger.warmup_reserve, ledger.cleanup_reserve = 60., 10.
            ledger.actual = 500.
            self.assertEqual(ledger.remaining, 0.)
            meter = Meter(ColdBackend(), ledger, DEFAULTS, folder)
            try:
                report, _ = meter.json('answer', {'answer_mode': 'choice', 'option_ids': ['0', '1']},
                    ('text', 'selected_option', 'claim_ids', 'evidence_ids'), final=True)
            finally:
                meter.close()
        self.assertEqual(report['selected_option'], '1')
        self.assertEqual([c['operation'] for c in ledger.calls], ['prepare_vlm', 'answer'])
        self.assertEqual(ledger.actual, 520.)
        self.assertFalse(ledger.budget_violation)

    def test_cold_start_longer_than_tool_cap_uses_separate_paid_preparation(self):
        clock = [100.]
        class SlowColdBackend:
            requires_vlm_warmup = True
            def call(self, operation, inputs, timeout):
                if operation == 'prepare_vlm':
                    if timeout < 80.:
                        clock[0] += timeout
                        raise TimeoutError('cold initialization was cancelled')
                    clock[0] += 80.
                    self.requires_vlm_warmup = False
                    return {'ready': True}
                clock[0] += 1.
                return {'data': {}}
        with tempfile.TemporaryDirectory() as folder, patch('script_core.budget.time.monotonic', side_effect=lambda: clock[0]):
            ledger = BudgetLedger(600.)
            ledger.warmup_reserve, ledger.cleanup_reserve = 60., 10.
            meter = Meter(SlowColdBackend(), ledger, DEFAULTS, folder)
            try:
                meter.call('parse', {})
            finally:
                meter.close()
        self.assertEqual([c['operation'] for c in ledger.calls], ['prepare_vlm', 'parse'])
        self.assertEqual(ledger.actual, 81.)
        self.assertGreater(ledger.warmup_reserve, 80.)
        self.assertEqual(ledger.total_cap, 600.)
        self.assertEqual(ledger.calls[1]['reservation'], DEFAULTS['budget']['tool_call_cap'])
        self.assertFalse(ledger.budget_violation)

    def test_tiny_local_balance_does_not_start_cold_worker(self):
        class ColdBackend:
            requires_vlm_warmup = True
            def call(self, *args):
                raise AssertionError('A doomed cold reload must not be started')
        with tempfile.TemporaryDirectory() as folder:
            ledger = BudgetLedger(600.)
            ledger.task_cap = 7.5
            meter = Meter(ColdBackend(), ledger, DEFAULTS, folder)
            try:
                with self.assertRaises(ResourceExhausted):
                    meter.call('assess', {})
            finally:
                meter.close()
        self.assertEqual(ledger.calls, [])

    def test_preparation_inside_task_keeps_local_cap(self):
        class ColdBackend:
            requires_vlm_warmup = True
            seen_timeout = None
            def call(self, operation, inputs, timeout):
                if operation == 'prepare_vlm':
                    self.seen_timeout = timeout
                    self.requires_vlm_warmup = False
                    return {'ready': True}
                return {'data': {}}
        backend = ColdBackend()
        with tempfile.TemporaryDirectory() as folder:
            ledger = BudgetLedger(600.)
            ledger.task_cap = 40.
            meter = Meter(backend, ledger, DEFAULTS, folder)
            try:
                meter.call('assess', {})
            finally:
                meter.close()
        self.assertLess(backend.seen_timeout, 40.)
        self.assertEqual(ledger.calls[0]['reservation'], 40.)

    def test_answer_retry_shares_original_thirty_second_allowance(self):
        clock = [100.]
        class TimeoutBackend:
            def call(self, operation, inputs, timeout):
                clock[0] += timeout
                raise TimeoutError('cancelled')
        with tempfile.TemporaryDirectory() as folder, patch('script_core.budget.time.monotonic', side_effect=lambda: clock[0]):
            ledger = BudgetLedger(600.)
            meter = Meter(TimeoutBackend(), ledger, DEFAULTS, folder)
            try:
                with self.assertRaises(ToolFailure):
                    meter.call('answer', {}, final=True)
            finally:
                meter.close()
        self.assertLessEqual(ledger.actual, DEFAULTS['budget']['final_answer_reserve'])
        self.assertFalse(ledger.budget_violation)

    def test_completion_reserve_tracks_expensive_checkpoint_without_increasing_cap(self):
        ledger = BudgetLedger(600.)
        ledger.warmup_reserve, ledger.cleanup_reserve = 60., 10.
        ledger.actual = 450.
        ledger.calls = [{'operation': 'checkpoint_write', 'actual_cost': 20.}]
        self.assertEqual(ledger.remaining, 15.)
        self.assertEqual(ledger.total_cap, 600.)

    def test_periodic_checkpoint_can_use_reserved_housekeeping_time(self):
        clock = [100.]
        with tempfile.TemporaryDirectory() as folder, patch('time.monotonic', side_effect=lambda: clock[0]):
            ledger = BudgetLedger(600.)
            ledger.warmup_reserve, ledger.cleanup_reserve, ledger.actual = 60., 10., 500.
            meter = Meter(object(), ledger, DEFAULTS, folder)
            try:
                self.assertEqual(ledger.remaining, 0.)
                with cpu_phase('checkpoint_write'):
                    with cpu_phase('object_validation'):
                        clock[0] += .2
                    clock[0] += 5.
            finally:
                meter.close()
        self.assertAlmostEqual(ledger.actual, 505.2)
        self.assertFalse(ledger.budget_violation)

    def test_parser_repeated_declarations_and_long_answer_are_bounded(self):
        parse = output_schema('parse', {})
        self.assertEqual(parse['properties']['variables']['maxItems'], 8)
        variable = {'name': 'target_object', 'description': 'object', 'constraints': []}
        with self.assertRaises(ValueError):
            validate_output([variable]*9, parse['properties']['variables'])
        answer = output_schema('answer', {'answer_mode': 'choice', 'option_ids': ['0']})
        self.assertEqual(next(iter(answer['properties'])), 'selected_option')
        with self.assertRaises(ValueError):
            validate_output({'selected_option': '0', 'text': 'x'*161, 'claim_ids': [], 'evidence_ids': []}, answer)
