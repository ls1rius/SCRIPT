"""Explicit manual M/N continuation after a navigation/candidate decoder failure."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import controlled_memory_io as io
from script import run_m_scene_experiment as common

DECODER = 'MN-navigation-candidate-complete-unicode-v2'
PATCH_FILES = ('script_core/backend.py', 'script/run_m_scene_experiment.py',
               'script/run_mn_decoder_resume.py')
REPAIR_OPERATIONS = ('m_transition_next', 'm_sufficiency_check', 'm_resolve_gap',
                     'm_candidate_query')


def patch_frozen_backend(source):
    """Apply only the decoder fix; never import unrelated workspace changes."""
    old = "('m_transition_next', 'm_sufficiency_check', 'm_resolve_gap')"
    new = "('m_transition_next', 'm_sufficiency_check', 'm_resolve_gap', 'm_candidate_query')"
    if source.count(old) != 2 or source.count("'MN-navigation-complete-unicode-v1'") != 1:
        raise ValueError('Unsupported frozen decoder baseline; inspect before patching')
    return source.replace(old, new).replace("'MN-navigation-complete-unicode-v1'", repr(DECODER))


def tree_hashes(folder):
    return {str(p.relative_to(folder)): io.sha(p) for p in sorted(folder.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}


def prepare(prior, out, *, upgrade_record=None, decoder_probe=None):
    started = time.monotonic()
    common.verify(prior)
    if out.exists() or io.read(prior/'queue_status.json')['state'] != 'failed':
        raise ValueError('Manual continuation needs a failed run and a fresh directory')
    for name, key in [('launch.json','worker_pid'), ('controller_launch.json','pid')]:
        proc = Path('/proc')/str(io.read(prior/name)[key])
        if proc.exists() and (proc/'stat').read_text().split()[2] != 'Z':
            raise ValueError('Prior process is still alive')
    upgrade = io.read(upgrade_record) if upgrade_record else None
    if bool(upgrade_record) != bool(decoder_probe):
        raise ValueError('Controlled upgrade requires both interruption audit and decoder probe')
    if upgrade and (upgrade.get('prior_run') != str(prior)
                    or not upgrade.get('between_calls_no_pending')
                    or upgrade.get('reason') != 'user_requested_decoder_upgrade'):
        raise ValueError('Invalid controlled decoder upgrade audit')
    if upgrade and list((prior/'runs').rglob('*.pending.json')):
        raise ValueError('Interrupted model calls require explicit accounting before continuation')
    policy = io.read(prior/'experiment.json')
    variant = policy.get('experiment_label','M')
    mode = {'M':'scene_planned_script','N':'role_bound_scene_script'}[variant]
    if io.read(prior/'qa_config.json')['query']['temporal_qa_evidence'] != mode:
        raise ValueError('Variant/config mismatch')
    ids = io.read(prior/'cohort.json')
    successful = []
    failed = None
    for uid in ids:
        folder = prior/'runs'/uid
        if not folder.exists():
            continue
        result = io.read(folder/'result.json') if (folder/'result.json').exists() else {}
        if result.get('answer') and not result.get('error'):
            if failed or uid != ids[len(successful)]:
                raise ValueError('Only a contiguous successful prefix may be inherited')
            if io.read(folder/'partial_temporal/result.json').get('memory_variant') != variant:
                raise ValueError('Inherited answer has wrong variant')
            search = io.read(folder/'partial_search/result.json')
            order = search['retrieval']['candidate_order']; opened = search['opened_scenes']
            if opened != order[:len(opened)] or not (folder/'partial_search/navigation_trace.json').exists():
                raise ValueError('Inherited navigation audit missing or inconsistent')
            successful.append(uid)
        else:
            if failed or uid != ids[len(successful)]:
                raise ValueError('Expected exactly one failed query immediately after successful prefix')
            failed = uid
    if not failed and upgrade and len(successful) < len(ids):
        failed = ids[len(successful)]
    if not failed:
        raise ValueError('No failed query to resume')
    if upgrade:
        operation = 'm_candidate_query'
        original = io.read(decoder_probe)
        probe_record = dict(operation=operation,original_ref=str(decoder_probe),
            scope='Shared decoder regression input from M failure; no N answer or evidence reused.')
    else:
        calls = [c for c in common.leaf_calls(prior/'runs'/failed) if c['operation'] in REPAIR_OPERATIONS]
        calls.sort(key=lambda c: c.get('ended_at', ''))
        if not calls:
            raise ValueError('No saved failing navigation/candidate call')
        operation = calls[-1]['operation']
        original = io.read(calls[-1]['output_ref'])
        probe_record = dict(operation=operation,call=calls[-1],original_ref=calls[-1]['output_ref'])
    try:
        json.loads(original['output']['raw_text'])
    except json.JSONDecodeError:
        pass
    else:
        raise ValueError('This continuation only repairs invalid navigation/candidate JSON')
    before = tree_hashes(prior)
    out.mkdir(parents=True)
    # Inherit original runtime, then replace only the decoder and explicit runner.
    for name in ('runtime','inputs','selection_validation'):
        shutil.copytree(prior/name,out/name,ignore=shutil.ignore_patterns('__pycache__'))
    for name in PATCH_FILES:
        if name == 'script_core/backend.py':
            (out/'runtime'/name).write_text(patch_frozen_backend((prior/'runtime'/name).read_text()))
        else:
            shutil.copy2(ROOT/name,out/'runtime'/name)
    old_runtime = tree_hashes(prior/'runtime')
    new_runtime = tree_hashes(out/'runtime')
    changed_runtime = sorted(name for name in old_runtime.keys() | new_runtime.keys()
                             if old_runtime.get(name) != new_runtime.get(name))
    if set(changed_runtime) - set(PATCH_FILES):
        raise ValueError('Unexpected runtime change during decoder-only continuation')
    for name in ('cohort.json','evaluation_only.json','qa_expected_artifacts.json','qa_config.json'):
        shutil.copy2(prior/name,out/name)
    if (prior/'historical_cost_snapshot.json').exists():
        shutil.copy2(prior/'historical_cost_snapshot.json',out/'historical_cost_snapshot.json')
    for uid in successful:
        shutil.copytree(prior/'runs'/uid,out/'runs'/uid)
    archive = out/'archived_attempt'
    if (prior/'runs'/failed).exists():
        shutil.copytree(prior/'runs'/failed,archive/('interrupted_query' if upgrade else 'failed_query'))
    if upgrade:
        io.write(archive/'interruption.json',upgrade)
    for name in ('experiment.json','report.json','setup.json','queue_status.json','frozen_hashes.json'):
        shutil.copy2(prior/name,archive/name)
    shutil.copytree(prior/'functional',archive/'functional')
    shutil.copytree(prior/'validation',archive/'validation')
    io.write(out/'validation/failed_navigation_input.json',original['inputs'])
    io.write(out/'validation/failed_navigation_original.json',original)
    io.write(out/'validation/failed_decoder_call.json',probe_record)
    failed_seconds = sum(c['actual_cost'] for c in common.leaf_calls(prior/'runs'/failed))
    # Inherited successful calls are counted once in runs/. Archive overhead and
    # the failed query are charged separately, not mixed into fresh measurements.
    overhead = io.read(prior/'setup.json')['seconds'] + io.read(prior/'functional/cost_summary.json')['seconds']
    prior_preparation_seconds = policy.get('prior_preparation_seconds',0.) + policy['cpu_preparation_seconds']
    policy.update(created_at=io.now(),prior_attempt=str(prior),
        runner_entry='script/run_mn_decoder_resume.py',navigation_decoder=DECODER,
        failed_decoder_operation=operation,
        resume_successful_ids=successful,resume_question_id=failed,
        reused_benchmark_answers=len(successful),answers_reused=True,
        cpu_preparation_seconds=time.monotonic()-started,
        prior_preparation_seconds=prior_preparation_seconds,
        prior_runtime_seconds=policy.get('prior_runtime_seconds',0.)+overhead+failed_seconds,
        prior_failed_query_seconds=policy.get('prior_failed_query_seconds',0.)+(0. if upgrade else failed_seconds),
        prior_interrupted_query_seconds=policy.get('prior_interrupted_query_seconds',0.)+(failed_seconds if upgrade else 0.),
        continuation_reason='user_requested_decoder_upgrade' if upgrade else 'invalid_json_repair',
        decoder_repair_scope='Extend existing UTF-8 token safety to free-text candidate query; schemas, prompts, source selection, extraction, plans and QA unchanged.',
        functional_gate_policy='Fresh saved-failure call with its original operation, then full M/N functional gate before resuming.',
        historical_cost_policy='Inherited successful calls counted once in runs; old setup/gates/failed query charged separately; all old records retained.')
    io.write(out/'experiment.json',policy)
    after = tree_hashes(prior)
    if after != before:
        raise ValueError('Prior run changed during preparation')
    io.write(out/'validation/continuation.json',dict(passed=True,prior_run=str(prior),
        prior_hashes=before,successful_ids=successful,restart_question=failed,
        original_runtime_changed_only=changed_runtime,prior_unchanged=True,
        inherited_run_hashes={uid:tree_hashes(out/'runs'/uid) for uid in successful},
        answers_inherited_byte_for_byte=True,failed_attempt_archived=not bool(upgrade),
        interrupted_attempt_archived=bool(upgrade and (prior/'runs'/failed).exists()),
        continuation_reason=policy['continuation_reason'],
        saved_failure_input_hash=io.sha(out/'validation/failed_navigation_input.json')))
    io.write(out/'frozen_hashes.json',tree_hashes(out))
    common.verify(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    common.report(out)


def functional(out, backend, cfg):
    from script_core.budget import BudgetLedger, Meter
    from script_core.m_scene_memory import validate_step
    from script_core.schemas import output_schema, validate_output
    request = io.read(out/'validation/failed_navigation_input.json')
    operation = io.read(out/'validation/failed_decoder_call.json')['operation']
    ledger = BudgetLedger(150,answer_reserve=0,calls_path=out/'functional/decoder/calls.jsonl')
    meter = Meter(backend,ledger,cfg,out/'functional/decoder/raw_calls')
    passed = False
    try:
        schema = output_schema(operation, request)
        result,_ = meter.json(operation,request,tuple(schema.get('required', ())))
        validate_output(result,schema)
        if operation in ('m_transition_next','m_resolve_gap'):
            validate_step(deepcopy(result),request)
        passed = True
        io.write(out/'functional/decoder/result.json',dict(passed=True,operation=operation,output=result,
            scope='Same saved failing input/operation; JSON/schema and applicable source/action validation, not answer accuracy.'))
    finally:
        io.write(out/'functional/decoder/cost_summary.json',ledger.summary())
        meter.close()
        if not passed:
            io.write(out/'validation/functional.json',dict(passed=False,failures=['saved_navigation_decoder'],
                updated_at=io.now(),benchmark_labels_used=False))
    variant = io.read(out/'experiment.json').get('experiment_label','M')
    if variant == 'N':
        from script.run_n_scene_experiment import functional as full_gate
    else:
        full_gate = common.functional
    gate = full_gate(out,backend,cfg)
    gate['saved_navigation_decoder_passed'] = passed
    gate['saved_decoder_operation'] = operation
    gate['navigation_decoder'] = DECODER
    io.write(out/'validation/functional.json',gate)
    return gate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--prior',type=Path)
    parser.add_argument('--upgrade-record',type=Path)
    parser.add_argument('--decoder-probe',type=Path)
    args=parser.parse_args();out=args.out.resolve()
    if args.mode=='prepare':
        if args.prior is None:parser.error('prepare requires --prior')
        prepare(args.prior.resolve(),out,
            upgrade_record=args.upgrade_record.resolve() if args.upgrade_record else None,
            decoder_probe=args.decoder_probe.resolve() if args.decoder_probe else None)
    elif args.mode=='queue':common.queue(out)
    elif args.mode=='worker':common.worker(out,functional_gate=functional)
    else:print(json.dumps(common.report(out),ensure_ascii=False))


if __name__=='__main__':main()
