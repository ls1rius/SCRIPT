"""Resume the fixed O matrix using independent per-sentence tensor batches."""
from copy import deepcopy
import json
from script import controlled_memory_io as io
from script_core import o_sentence_filter as core
from script_core.budget import BudgetLedger, Meter
from script_core.config import digest
from script_core.o_batch_generation import OPERATION, validate_packet


def packet_for(uid, condition, operation, requests):
    scope = dict(question_id=uid, condition=condition)
    packet = dict(scope=scope, operation=operation, items=[
        dict(source_id=sid, scope=deepcopy(scope), request=req) for sid, req in requests])
    validate_packet(packet)
    return packet


def batch_call(meter, uid, condition, operation, requests):
    packet = packet_for(uid, condition, operation, requests)
    output, call = meter.call(OPERATION, packet)
    rows = output.get('items', [])
    if len(rows) != len(requests): raise ValueError('Batch output row count changed')
    for row, (sid, req) in zip(rows, requests):
        if (row.get('source_id') != sid or row.get('scope') != packet['scope']
                or row.get('request_hash') != digest(req)):
            raise ValueError('Batch output identity/scope/input mismatch')
    return rows


def restore_ledger(path, cap, reserve):
    ledger = BudgetLedger(cap, answer_reserve=reserve, calls_path=path)
    if path.exists():
        ledger.calls = [json.loads(line) for line in path.read_text().splitlines() if line]
        if any(c.get('error') for c in ledger.calls):
            raise ValueError('Cannot resume failed model calls')
        ledger.actual = sum(c['actual_cost'] for c in ledger.calls)
    return ledger


def prior_output(meter, operation, request, serial_index=None):
    """Recover a durable single call interrupted before its derived file commit.

    Only used for the explicit serial-to-batch upgrade, scoped by case/condition
    directory and original source ordinal. Identical sentences at different
    positions remain separate calls; this is not an inference cache.
    """
    wanted = digest(request)
    calls = [c for c in meter.ledger.calls if c['operation'] == operation]
    if operation != core.FILTER_PLAN:
        if serial_index is None: raise ValueError('Recovery requires source ordinal')
        calls = calls[serial_index:serial_index+1]
    for call in reversed(calls):
        if operation != core.FILTER_PLAN and call['input_hash'] != wanted:
            raise ValueError('Inherited call/source ordinal mismatch')
        if call['operation'] == operation and call['input_hash'] == wanted:
            local = meter.raw_directory / f"{call['call_id']}-{call['attempt']}.json"
            record = io.read(local)
            if digest(record['inputs']) != wanted or record.get('error'):
                raise ValueError('Inherited raw call mismatch')
            return record['output']
    return None


def extraction_decision(source, request, output, folder):
    fallback = None
    try:
        if output.get('parse_error') or output.get('data') is None:
            raise ValueError(str(output.get('parse_error')))
        units = core.candidates(source, output['data'])
        if units[0]['kind'] == 'raw_sentence':
            fallback = output['data']['extraction_status'] + ': no extracted information'
    except (ValueError, KeyError, TypeError) as exc:
        fallback = 'extraction_format_failure: ' + str(exc)
        units = [dict(id='U0', kind='raw_sentence', group=source['id'], text=source['text'])]
    io.write(folder/'extraction.json', dict(request=request, output=output, fallback=fallback))
    return dict(source=source, candidates=units, fallback=fallback, source_validation_performed=False)


def decode(operation, request, output):
    if output.get('parse_error') or output.get('data') is None:
        raise ValueError(f"{operation}: {output.get('parse_error')}")
    core.validate_output(output['data'], core.schema(operation, request))
    if operation == core.COMPOSE:
        groups = output['data']['groups']
        return dict(groups=groups, text=' '.join(groups[g]['text'].strip()
            for g in request['groups'] if groups[g]['text'].strip()))
    return output['data']['labels'] if operation == core.FILTER else output['data']


def run(out, backend, cfg, *, case_ids=None):
    from script.o_composition_context_eval import conditions_for, report
    from script.run_o_sentence_experiment import extraction_filter_costs
    policy = io.read(out/'experiment.json'); gold = io.read(out/'evaluation_only.json')
    conditions = conditions_for(policy)
    batch_size = policy['independent_sentence_batch_size']
    if batch_size not in (2, 4): raise ValueError('Unsupported fixed batch size')
    for index, uid in enumerate(policy['case_ids'], 1):
        if case_ids is not None and uid not in case_ids: continue
        inputs = out/'cases'/uid; folder = out/'runs'/uid
        question = io.read(inputs/'question.json'); sources = io.read(inputs/'sentences.json')
        original = io.read(inputs/'original_qa.json')
        result_path = folder/'result.json'
        result = io.read(result_path) if result_path.exists() else dict(
            question_id=uid, complete=False, error=None, arms={}, conditions={})
        if result.get('error'): raise ValueError('Cannot silently resume an errored case')
        if result['complete']: continue
        def progress(stage, condition, number):
            io.write(out/'progress.json', dict(stage=stage, case=index, question_id=uid,
                condition=condition, sentence=number, total_sentences=len(sources),
                batch_size=batch_size, updated_at=io.now()))
        replay = (inputs/'selection_replay/selection.json').exists()
        frozen = io.read(inputs/'selection_replay/selection.json') if replay else {}
        extraction_cost = extraction_filter_costs(io.read(inputs/'selection_replay/result.json'))[core.EXTRACT] if replay else 0.
        result.update(new_extraction=not replay, new_filtering=True,
                      execution='independent-tensor-batches' if policy.get('full_cohort') else 'serial-prefix-plus-independent-tensor-batches')
        try:
            if not replay:
                target = folder/'shared_extraction'
                ledger = restore_ledger(target/'calls.jsonl', policy['case_budget_seconds'], 2*len(conditions)*cfg['budget']['final_answer_reserve'])
                meter = Meter(backend, ledger, cfg, target/'raw_calls')
                try:
                    pending = []
                    for serial_index, source in enumerate(sources):
                        sf = target/'sentences'/source['id']; request = core.extraction_input(source)
                        if (sf/'extraction.json').exists():
                            stored = io.read(sf/'extraction.json')
                            if stored['request'] != request: raise ValueError('Inherited extraction source changed')
                            output = stored['output']
                        else: output = prior_output(meter, core.EXTRACT, request, serial_index)
                        if output is None: pending.append(source)
                        else: frozen[source['id']] = extraction_decision(source, request, output, sf)
                    for start in range(0, len(pending), batch_size):
                        chunk = pending[start:start+batch_size]
                        progress('shared_extraction', 'source_only', len(frozen)+1)
                        requests = [(s['id'], core.extraction_input(s)) for s in chunk]
                        outputs = batch_call(meter, uid, 'source_only', core.EXTRACT, requests)
                        for source, (_, request), output in zip(chunk, requests, outputs):
                            frozen[source['id']] = extraction_decision(source, request, output, target/'sentences'/source['id'])
                        io.write(target/'candidates.json', frozen)
                finally:
                    result['shared_extraction_cost_summary'] = ledger.summary()
                    extraction_cost = ledger.summary()['total']
                    io.write(target/'cost_summary.json', ledger.summary()); meter.close()
            result['shared_extraction_seconds'] = extraction_cost
            prior_conditions_cost = 0.
            for condition, include_options in conditions.items():
                target = folder/condition
                ledger = restore_ledger(target/'calls.jsonl', policy['case_budget_seconds'], 2*cfg['budget']['final_answer_reserve'])
                ledger.dependency_cost = extraction_cost + prior_conditions_cost
                if result['conditions'].get(condition, {}).get('complete'):
                    prior_conditions_cost += ledger.actual
                    continue
                meter = Meter(backend, ledger, cfg, target/'raw_calls'); decisions = {}; compositions = {}
                result['conditions'][condition] = dict(complete=False, error=None)
                try:
                    q = dict(question=question['question'])
                    if include_options: q['options'] = deepcopy(question['options'])
                    if sources:
                        plan = prior_output(meter, core.FILTER_PLAN, q)
                        if plan is None: plan, _ = meter.call(core.FILTER_PLAN, q)
                        q['evidence_task'] = decode(core.FILTER_PLAN, q, plan)
                    io.write(target/'filter_question.json', q)
                    pending = []
                    for source in sources:
                        sf = target/'sentences'/source['id']
                        if (sf/'selection.json').exists():
                            d = io.read(sf/'selection.json')
                            if d['source'] != source or d['candidates'] != frozen[source['id']]['candidates']:
                                raise ValueError('Inherited selection source/candidates changed')
                            decisions[source['id']] = d
                        else: pending.append(source)
                    for start in range(0, len(pending), batch_size):
                        chunk = pending[start:start+batch_size]
                        progress('matrix_filter', condition, len(decisions)+1)
                        requests = [(s['id'], core.filter_input(q, s, frozen[s['id']]['candidates'])) for s in chunk]
                        # A stopped serial parent may have committed the raw call only.
                        source_order = {s['id']: i for i, s in enumerate(sources)}
                        recovered = [prior_output(meter, core.FILTER, req, source_order[sid]) for sid, req in requests]
                        missing = [req for req, output in zip(requests, recovered) if output is None]
                        fresh = iter(batch_call(meter, uid, condition, core.FILTER, missing) if missing else [])
                        outputs = [o if o is not None else next(fresh) for o in recovered]
                        for source, (_, request), output in zip(chunk, requests, outputs):
                            sf = target/'sentences'/source['id']; d = deepcopy(frozen[source['id']])
                            raw = decode(core.FILTER, request, output)
                            labels, overrides = core.preserve_action_core(d['candidates'], raw)
                            d.update(model_labels=raw, labels=labels, program_overrides=overrides, selected=core.select(d['candidates'], labels))
                            io.write(sf/'filter_decision_input.json', request); io.write(sf/'filter_decision.json', output['data'])
                            io.write(sf/'selection.json', d); decisions[source['id']] = d
                    io.write(target/'selection.json', decisions)
                    pending = []
                    for source in sources:
                        sf = target/'sentences'/source['id']; chosen = decisions[source['id']]['selected']
                        if (sf/'composition.json').exists():
                            compositions[source['id']] = io.read(sf/'composition.json')
                        elif not chosen:
                            compositions[source['id']] = dict(groups={}, text='')
                            io.write(sf/'composition.json', compositions[source['id']])
                        else: pending.append(source)
                    for start in range(0, len(pending), batch_size):
                        chunk = pending[start:start+batch_size]
                        progress('matrix_compose', condition, len(compositions)+1)
                        requests = [(s['id'], core.compose_input(question, decisions[s['id']]['selected'], include_options=include_options)) for s in chunk]
                        compose_order = {s['id']: i for i, s in enumerate(s for s in sources if decisions[s['id']]['selected'])}
                        recovered = [prior_output(meter, core.COMPOSE, req, compose_order[sid]) for sid, req in requests]
                        missing = [req for req, output in zip(requests, recovered) if output is None]
                        fresh = iter(batch_call(meter, uid, condition, core.COMPOSE, missing) if missing else [])
                        outputs = [o if o is not None else next(fresh) for o in recovered]
                        for source, (_, request), output in zip(chunk, requests, outputs):
                            sf = target/'sentences'/source['id']; composed = decode(core.COMPOSE, request, output)
                            io.write(sf/'composition_input.json', request); io.write(sf/'composition.json', composed)
                            compositions[source['id']] = composed
                    for kind, operation in [('O', 'o_answer'), ('Oplus', 'oplus_answer')]:
                        arm = f'{kind}_{condition}'; af = folder/arm
                        if arm in result['arms']: continue
                        rows = core.present(sources, decisions, composed=compositions if kind == 'Oplus' else None)
                        final = core.final_input(original, rows)
                        io.write(af/'retained.json', rows); io.write(af/'final_input.json', final)
                        answer_request = dict(inputs=final, options=question['options'])
                        answer = prior_output(meter, operation, answer_request, 0)
                        if answer is None:
                            answer, call = meter.call(operation, answer_request, final=True)
                        else:
                            call = next(c for c in ledger.calls if c['operation'] == operation)
                        io.write(af/'answer.json', answer)
                        result['arms'][arm] = dict(answer=answer['data'], correct=str(answer['data']['selected_option']) == str(gold[uid]),
                            input_tokens=answer['usage']['input_tokens'], retained_sentences=len(rows), answer_seconds=call['actual_cost'],
                            shared_selection_hash=io.sha(target/'selection.json'))
                        io.write(result_path, result)
                    result['conditions'][condition].update(complete=True,
                        retained_units=sum(len(d['selected']) for d in decisions.values()),
                        all_zero_sentences=sum(not d['selected'] for d in decisions.values()),
                        composition_empty_sentences=sum(bool(decisions[s]['selected']) and not c['text'].strip() for s, c in compositions.items()))
                except Exception as exc:
                    result['conditions'][condition]['error'] = dict(type=type(exc).__name__, message=str(exc)); raise
                finally:
                    result['conditions'][condition]['cost_summary'] = ledger.summary()
                    io.write(target/'cost_summary.json', ledger.summary()); io.write(result_path, result); meter.close()
                prior_conditions_cost += ledger.actual
            result['complete'] = True
        except Exception as exc:
            result['error'] = dict(type=type(exc).__name__, message=str(exc)); raise
        finally:
            result['updated_at'] = io.now(); io.write(result_path, result); report(out)
        print('BATCHED CASE COMPLETE', index, uid, flush=True)
