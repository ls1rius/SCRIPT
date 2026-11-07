"""Measured serial/batch probes, kept separate from the ten diagnostic cases."""
from script import controlled_memory_io as io
from script.o_batched_matrix import packet_for, batch_call, decode
from script_core import o_sentence_filter as core
from script_core.budget import BudgetLedger, Meter
from script_core.o_batch_generation import messages_for


def run(out, backend, cfg):
    target = out/'batch_functional'
    ledger = BudgetLedger(900, answer_reserve=0, calls_path=target/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, target/'raw_calls')
    result = dict(passed=False, checks=[], error=None, speed={})
    sources = [dict(id=f'S{i}', text=text) for i, text in enumerate([
        'D opens a drawer.', 'E closes a window.', 'F lifts a cup.', 'G pushes a cart.'])]
    try:
        requests = [(s['id'], core.extraction_input(s)) for s in sources]
        outputs = batch_call(meter, 'batch_probe', 'source_only', core.EXTRACT, requests)
        extracted = []
        for source, (_, req), output in zip(sources, requests, outputs):
            if output.get('parse_error') or not output['ended_normally']: raise ValueError('Batched extraction decoding failed')
            extracted.append(core.candidates(source, decode(core.EXTRACT, req, output)))
            if output['prompt'] != messages_for(core.EXTRACT, req)[0]: raise ValueError('Extraction prompt was contaminated')
        result['checks'].append(dict(name='four_independent_source_prompts', passed=True))
        q = dict(question='Which actions are explicitly observed?', evidence_task=dict(option_tests=dict(
            Q=dict(single_observation='An explicitly observed action.', aggregate_requirement='', interpretation='Keep direct action observations.'))))
        # Distinct row schemas exercise routing even when sentence lengths differ.
        requests = [(s['id'], core.filter_input(q, s, units)) for s, units in zip(sources, extracted)]
        serial = []
        start = ledger.actual
        for _, req in requests:
            output, _ = meter.call(core.FILTER, req)
            serial.append(decode(core.FILTER, req, output))
        serial_seconds = ledger.actual-start
        start = ledger.actual
        outputs = batch_call(meter, 'batch_probe', 'question_only', core.FILTER, requests)
        batch_seconds = ledger.actual-start
        batched = [decode(core.FILTER, req, output) for (_, req), output in zip(requests, outputs)]
        for (_, req), output in zip(requests, outputs):
            if output['prompt'] != messages_for(core.FILTER, req)[0]: raise ValueError('Filter prompt was contaminated')
        result['checks'].append(dict(name='independent_filter_schemas_and_prompts', passed=True,
                                    serial_labels=serial, batch_labels=batched, labels_equal=serial == batched))
        result['speed'] = dict(serial_seconds=serial_seconds, batch_seconds=batch_seconds,
            ratio=serial_seconds/batch_seconds, rows=4,
            scope='Same four requests on the shared GPU, serial then batched; changing contention is not controlled.')
        requests = []
        for i, actor in enumerate(['D', 'E', '繫', 'D"E']):
            units = [dict(id='U0', kind='action_field', group='actions_0', field='actor', value=actor)]
            requests.append((f'S{i}', core.compose_input(dict(question='Who is explicitly identified?'), units)))
        outputs = batch_call(meter, 'batch_probe', 'question_only', core.COMPOSE, requests)
        for (_, req), output in zip(requests, outputs):
            decode(core.COMPOSE, req, output)
            if output['prompt'] != messages_for(core.COMPOSE, req)[0]: raise ValueError('Composition prompt was contaminated')
        result['checks'].append(dict(name='identity_aliases_and_eos_padding', passed=True))
        if batch_seconds >= serial_seconds: raise ValueError('Batch probe did not improve measured throughput')
        result['passed'] = True
    except Exception as exc:
        result['error'] = dict(type=type(exc).__name__, message=str(exc)); raise
    finally:
        result['cost_summary'] = ledger.summary(); result['updated_at'] = io.now()
        io.write(out/'validation/batch_functional.json', result); meter.close()
