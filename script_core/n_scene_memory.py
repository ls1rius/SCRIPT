"""N inherits M navigation/presentation, with separately versioned extraction."""
from copy import deepcopy

from . import m_scene_memory as inherited
from .accounting import metered_cpu
from .state import write_json
from .m_candidate_selection import OPERATION as SELECT_OPERATION, KEYS as SELECT_KEYS, normalize_selection, prepare_inputs as prepare_selection_inputs, review_scenes

MODE = 'role_bound_scene_script'
VERSION = 'N-M-neutral-query-joint-candidates-v8'
PARENT_VERSION = inherited.VERSION


def __getattr__(name):
    return getattr(inherited, name)


def preserve_incomplete_sources(pool, proposed):
    """A retained partial H projection must retain its unproven complete source.

    Original captions exist in H's projection precisely when whole-clause
    coverage was not established. This is conservative source preservation,
    not a claim that the added source is independently relevant or correct.
    """
    plan = deepcopy(proposed)
    selected = set(plan['keep'] + plan['uncertain'] + [i for g in plan['merge_groups'] for i in g])
    dependencies = {}
    for record in pool['records']:
        if record['id'] in selected:
            for source in record['sources']:
                if source['region'] == 'H fact':
                    dependencies.setdefault(source['caption_id'], set()).add(record['id'])
    overrides = []
    for record in pool['records']:
        if record['id'] not in plan['skip']:
            continue
        sources = [s for s in record['sources'] if s['region'] == 'description'
                   and s['caption_id'] in dependencies]
        if not sources:
            continue
        plan['skip'].remove(record['id'])
        plan['keep'].append(record['id'])
        overrides.append(dict(record_id=record['id'], model_decision='skip', actual_decision='keep',
            caption_ids=sorted({s['caption_id'] for s in sources}),
            dependent_record_ids=sorted({rid for s in sources for rid in dependencies[s['caption_id']]}),
            reason='Selected H facts lack a whole-source coverage proof; preserve their archived original clause.'))
    return inherited.parent.audit_plan(plan, pool), overrides


@metered_cpu('n_evidence_materialization')
def project_evidence(qa, payload, query, meter, target):
    from script.controlled_memory_io import parts
    target.mkdir(parents=True, exist_ok=True)
    memory = inherited.merged.materialize(payload)
    pool = inherited.build_pool(memory, qa, parts)
    planner = inherited.parent
    inputs = planner.planner_input(query, pool, query.get('id') or query['question_id'])
    write_json(target/'pool.json', pool)
    write_json(target/'planner_input.json', inputs)
    output, _ = meter.call('n_evidence_plan', dict(inputs=inputs, schema=planner.plan_schema(pool)))
    write_json(target/'planner_output.json', output)
    proposed = planner.decode_plan(output['raw'], pool)
    write_json(target/'proposed_plan.json', proposed)
    plan, overrides = preserve_incomplete_sources(pool, proposed)
    write_json(target/'source_preservation.json', dict(overrides=overrides,
        rule='Retained partial H facts require their available original source; all-skipped sources stay skipped.'))
    description, audit = inherited.render(pool, plan)
    audit['source_preservation_overrides'] = overrides
    final = planner.final_input(qa, description, parts)
    write_json(target/'plan.json', plan)
    write_json(target/'audit.json', audit)
    write_json(target/'final_input.json', final)
    return final, dict(version=VERSION, parent='M', parent_version=PARENT_VERSION,
                       pool_records=len(pool['records']), audit=deepcopy(audit))


def answer_without_candidates(packet, bundle, backend, cfg, target, settings):
    from .budget import BudgetLedger, Meter
    from .config import digest
    from script.controlled_memory_io import parts
    target.mkdir(parents=True, exist_ok=True)
    ledger = BudgetLedger(settings['online_seconds'], initialization_cost=bundle['preparation_seconds'],
        answer_reserve=cfg['budget']['final_answer_reserve'], calls_path=target/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, target/'raw_calls')
    result = dict(arm='adaptive_partial_temporal', method=VERSION, memory_variant='N',
        question_id=packet['question']['question_id'], answer=None, error=None,
        opened_scenes=[], selected_seconds=[], paired_evidence_hash=digest(bundle),
        incomplete=[dict(stage='candidate_selection', reason='No relevant candidate scenes; evidence insufficient')],
        source_incomplete=deepcopy(bundle['source_incomplete']),
        supplemental_visual=deepcopy(bundle['supplemental_visual']),
        shared_preparation_seconds=bundle['preparation_seconds'],
        qa_evidence=dict(variant='N', observed_source_rows=0, planner_skipped_no_evidence=True))
    try:
        qa = deepcopy(bundle['qa_input'])
        prefix, _, suffix = parts(qa)
        qa['prompts'][0] = prefix+'No relevant candidate scene was selected. No detailed source observations were read. Evidence is insufficient; do not infer evidence from option premises.'+suffix
        write_json(target/'N/empty_evidence.json', dict(reason='no_relevant_candidates',
            observed_sources=[], planner_called=False, evidence_status='insufficient'))
        write_json(target/'N/final_input.json', qa)
        checked, _ = meter.call('temporal_qa_check', qa, final=True)
        answer, _ = meter.call('n_answer', dict(inputs=qa, options=packet['question']['options']), final=True)
        result.update(answer=answer['data'], qa_input_tokens=checked['input_tokens'])
    except Exception as exc:
        result['error'] = dict(type=type(exc).__name__, message=str(exc))
    finally:
        result['cost_summary'] = ledger.summary()
        write_json(target/'result.json', result)
        meter.close()
    return result
