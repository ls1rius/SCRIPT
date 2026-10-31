from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from script import generalized_lazy_execution as execution
from script.run_generalized_lazy_experiment import extract_source
from script_core import generalized_lazy_memory as core
from script_core.budget import BudgetLedger, ResourceExhausted
from script_core.qa_evidence_view import compact_description


def unit(uid, text, start):
    return dict(id=uid, text=text, source_caption_ids=[uid + 'C'], source_intervals=[[start, start + 1]])


def row(u):
    return dict(caption_id=u['source_caption_ids'][0], source_id=u['id'],
        sample_interval=u['source_intervals'][0], frame_index=u['source_intervals'][0][0], original_text=u['text'])


def output(inputs):
    data = {category: [] for category in core.joint.COLLECTIONS.values()}
    data['uncertain'] = False
    if 'holds' in inputs['unit']['text']:
        data['actions'] = [dict(actor='C', verb='holds', roles=[dict(role='patient', value='book')])]
    return dict(data=data, usage=dict(input_tokens=10, output_tokens=20))


class Meter:
    def __init__(self):
        self.ledger = SimpleNamespace(calls=[])
    def call(self, operation, inputs):
        self.ledger.calls.append(dict(operation=operation, parameters=deepcopy(inputs), actual_cost=2))
        return dict(items=[output(request) for request in inputs['requests']]), None
    def json(self, operation, inputs, required):
        self.ledger.calls.append(dict(operation=operation, parameters=deepcopy(inputs), actual_cost=1))
        return output(inputs)['data'], None


class GeneralizedLazyExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = core.ScriptDB(Path(self.temp.name) / 'memory.sqlite3')
        self.addCleanup(self.store.close)
        self.protocol = SimpleNamespace(is_infrastructure=lambda exc: 'CUDA' in str(exc))
        self.meter = Meter()
        self.extractor = execution.SourceExtractor(self.meter, core, self.protocol, extract_source)

    def test_batched_fields_match_serial_memory_and_keep_source_order(self):
        units = [unit('U0', '#C C holds the book.', 0), unit('U1', '#C The book is wet.', 1),
            unit('U2', '#C C never holds the book.', 2)]
        rows = list(map(row, units))
        actual, stats = execution.ensure_batched(self.store, 'v', units, rows, ['actions.base'], self.extractor)
        with core.ScriptDB(Path(self.temp.name) / 'serial.sqlite3') as serial:
            expected, old = serial.ensure('v', units, rows, ['actions.base'], self.extractor)
        self.assertEqual(actual, expected)
        self.assertEqual(stats, old)
        self.assertEqual(list(actual), ['U0', 'U1', 'U2'])
        self.assertEqual(self.meter.ledger.calls[0]['operation'], execution.BATCH_OPERATION)
        for inputs in self.meter.ledger.calls[0]['parameters']['requests']:
            self.assertEqual(set(inputs), {'unit', 'requested_fields', 'source_type'})
        text, _, audit = self.store.view('v', {}, rows, actual, compact_description)
        self.assertIn('never holds', text)
        self.assertEqual(audit['visible_source_rows'], 3)
        class NoCall:
            batch_size = 2
            def many(self, jobs):
                raise AssertionError('Re-extracted cached fields')
        warm, hits = execution.ensure_batched(self.store, 'v', units, rows, ['actions.base'], NoCall())
        self.assertEqual(warm, actual)
        self.assertEqual(hits['missing_fields'], 0)

    def test_repeated_text_is_generated_once_but_all_observation_times_remain(self):
        units = [unit('U0', '#C C holds the book.', 0), unit('U1', '#C C holds the book.', 5)]
        results, stats = execution.ensure_batched(self.store, 'v', units, list(map(row, units)),
            ['actions.base'], self.extractor)
        self.assertEqual(len(self.meter.ledger.calls), 1)
        self.assertEqual(stats['source_extractions'], 1)
        self.assertEqual(results['U1']['accepted']['facts'][0]['source_id'], 'U1')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 2)

    def test_one_invalid_output_does_not_discard_other_source(self):
        original = self.meter.call
        def invalid(operation, inputs):
            response, call = original(operation, inputs)
            response['items'][1]['data']['actions'] = [dict(actor='C', verb='holds', roles=[dict(role='patient', value='hammer')])]
            return response, call
        self.meter.call = invalid
        units = [unit('U0', '#C C holds the book.', 0), unit('U1', '#C C holds a spoon.', 1)]
        results, stats = execution.ensure_batched(self.store, 'v', units, list(map(row, units)),
            ['actions.base'], self.extractor)
        self.assertFalse(results['U0']['accepted']['uncertain'])
        self.assertTrue(results['U1']['accepted']['uncertain'])
        self.assertEqual(stats['status_failed'], 1)
        text, _, _ = self.store.view('v', {}, list(map(row, units)), results, compact_description)
        self.assertIn(units[1]['text'], text)

    def test_batch_wall_cost_charged_once_and_sources_counted_separately(self):
        units = [unit('U0', '#C C holds the book.', 0), unit('U1', '#C The book is wet.', 1)]
        execution.ensure_batched(self.store, 'v', units, list(map(row, units)), ['actions.base'], self.extractor)
        self.assertEqual(sum(c['actual_cost'] for c in self.meter.ledger.calls), 2)
        self.assertEqual(execution.extraction_counts(self.meter.ledger.calls, core.EXTRACT),
            dict(field_extraction_model_calls=1, field_extraction_source_requests=2, extraction_batches=1))

    def test_infrastructure_failure_stops_without_retry_or_database_commit(self):
        count = []
        def fail(*args):
            count.append(1)
            raise RuntimeError('CUDA out of memory')
        self.meter.call = fail
        units = [unit('U0', '#C C holds the book.', 0), unit('U1', '#C The book is wet.', 1)]
        with self.assertRaisesRegex(RuntimeError, 'CUDA'):
            execution.ensure_batched(self.store, 'v', units, list(map(row, units)), ['actions.base'], self.extractor)
        self.assertEqual(len(count), 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM field_cache').fetchone()[0], 0)

    def test_exhausted_budget_keeps_both_original_sources_without_retry(self):
        def fail(*args):
            raise ResourceExhausted('No budget')
        self.meter.call = fail
        units = [unit('U0', '#C C holds the book.', 0), unit('U1', '#C The book is wet.', 1)]
        results, stats = execution.ensure_batched(self.store, 'v', units, list(map(row, units)), ['actions.base'], self.extractor)
        self.assertEqual(stats['status_failed'], 2)
        text, _, _ = self.store.view('v', {}, list(map(row, units)), results, compact_description)
        self.assertTrue(all(u['text'] in text for u in units))

    def test_restored_ledger_keeps_spent_budget(self):
        path = Path(self.temp.name) / 'calls.jsonl'
        path.write_text(json.dumps(dict(operation='h_extract_fields', actual_cost=200, reservation=250)) + '\n')
        ledger = execution.restore_ledger(BudgetLedger)(300, answer_reserve=45, calls_path=path)
        self.assertEqual(ledger.actual, 200)
        self.assertEqual(ledger.allowance(150), 55)
        self.assertEqual(len(ledger.calls), 1)

    def test_resume_reuses_logged_full_output_for_partially_committed_fields(self):
        u = unit('U0', '#C C holds the book.', 0)
        inputs = self.extractor.inputs(u, ['actions.base', 'actions.instrument'])
        raw = Path(self.temp.name) / 'raw.json'
        raw.write_text(json.dumps(dict(inputs=inputs, output=output(inputs), error=None)))
        self.meter.ledger.calls = [dict(operation=core.EXTRACT, output_ref=str(raw), error=None)]
        recovered = execution.SourceExtractor(self.meter, core, self.protocol, extract_source)
        def no_call(*args):
            raise AssertionError('Completed source output was generated again')
        self.meter.json = no_call
        result = recovered(u, ['actions.base'])
        self.assertEqual(result['accepted']['facts'][0]['patient'], 'book')


if __name__ == '__main__':
    unittest.main()
