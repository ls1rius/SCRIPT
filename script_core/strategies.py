"""Eager details are a separately costed overlay, built with the same materializer."""
from copy import deepcopy
from pathlib import Path
import time

from .budget import BudgetLedger, Meter, ResourceExhausted
from .config import digest
from .controller import extraction_scope
from .materialize import Materializer
from .state import Store, obj, write_json


def build_eager(base, cfg, backend, output, initialization_cap=None):
    from .accounting import ACTIVE
    previous = ACTIVE.get()
    try:
        return _build_eager(base, cfg, backend, output, initialization_cap)
    except Exception as exc:
        meter = ACTIVE.get()
        if meter is not previous and meter is not None:
            write_json(Path(output) / 'build_failure.json', {
                'error': {'code': 'build_failed', 'message': str(exc)},
                'construction_complete': False, 'cost_summary': meter.ledger.summary()})
        raise
    finally:
        meter = ACTIVE.get()
        if meter is not previous and meter is not None:
            meter.close()


def _build_eager(base, cfg, backend, output, initialization_cap=None):
    output = Path(output)
    if (output / 'build_cost.json').exists():
        store = Store(output)
        try:
            memory, meta = store.load()
        finally:
            store.close()
        if meta['config_hash'] != digest(cfg):
            raise ValueError('Existing eager overlay has a different configuration')
        return memory, meta['build_cost']
    memory = deepcopy(base)
    namespace = base.manifest['video_id']+':eager'
    ledger = BudgetLedger(initialization_cap if initialization_cap is not None else 1e12,
                          answer_reserve=0, calls_path=output / 'calls.jsonl')
    meter = Meter(backend, ledger, cfg, output / 'raw_calls')
    materializer = Materializer(memory, meter, cfg, namespace, output)
    start = time.monotonic()
    complete = True
    for scene in memory.tables['scenes'].values():
        windows = [memory.get(w) for w in scene['window_ids']]
        for window in windows:
            if ledger.remaining < .001:
                complete = False
                break
            candidate_id = namespace+':candidate:'+window['id']
            branch_id = namespace+':branch:'+scene['id']
            if branch_id not in memory.tables['branches']:
                memory.put('branches', obj(branch_id, parent_branch_id=None, hypothesis_ids=[], requirement_ids=[],
                                           candidate_ids=[], status='active', no_progress_count=0))
            # No Query or question input is supplied during eager construction.
            dummy_requirement = obj(namespace+':all', predicate='other', roles={}, scope=scene['interval'], quantifier='all',
                                    prerequisite_ids=[], acceptable_modalities=['visual', 'subtitle'], answer_slot=None,
                                    support_sets=[], status='unresolved')
            if dummy_requirement['id'] not in memory.tables['requirements']:
                memory.put('requirements', dummy_requirement)
            extraction = extraction_scope(dummy_requirement, 'eager')
            memory.put('candidate_queue', obj(candidate_id, scene_ids=[scene['id']], requirement_id=dummy_requirement['id'],
                                               branch_id=branch_id, goal_type='relation', route='time', basis_refs=[window['id']],
                                               binding_ids=[], priority_class=1, basis_rank=2, expected_cost=cfg['budget']['local_task_cap'],
                                               interval=window['interval'], status='pending'))
            task = obj(memory.new_id(namespace, 'task'), candidate_id=candidate_id, scene_ids=[scene['id']],
                       allowed_interval=window['interval'], requirement_ids=[], target_binding_ids=[],
                       goal='记录所有已定位实体及预定义关系；无问题输入。', acceptance_checks=[], route='time',
                       basis_refs=[window['id']], relevant_versions={}, max_new_observations=cfg['query']['new_observations_per_task'],
                       cost_cap=min(ledger.remaining, cfg['budget']['local_task_cap']), extraction_scope=extraction,
                       target_signature=digest({'scope': 'all', 'predicates': extraction['predicates']}), variables=[])
            memory.put('tasks', task)
            result = materializer.execute(task, [{'interval': window['interval']}])
            complete &= all(memory.get(b)['status'] == 'complete' for b in result['build_record_ids']) and bool(result['build_record_ids'])
    cost = ledger.summary() | {'construction_complete': complete, 'wall_seconds': time.monotonic()-start}
    store = Store(output)
    try:
        store.checkpoint(memory, {'config_hash': digest(cfg), 'build_cost': cost})
        cost.update(ledger.summary())
        store.finalize_accounting(memory, {'build_cost': cost})
    finally:
        store.close()
        meter.close()
    write_json(output / 'build_cost.json', cost)
    return memory, cost
