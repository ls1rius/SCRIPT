"""Freeze and queue a new two-arm comparison without changing the four-arm run.

Control: the preserved adaptive_scheme4. Treatment: optimized_scheme4.
Both use the same cohort, source directory, budget and final QA template.
"""
import argparse
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import shlex
import statistics
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import run_adaptive_comparison as original
from script_core.config import load_config
from script_core.state import write_json as write

ARMS = ('adaptive_scheme4', 'optimized_scheme4')
read, now = original.read, original.now


def freeze(out, count=30):
    original.freeze(out, count)
    settings = read(out/'settings.json')
    settings.update(optimized_max_plans=4, optimized_initial_captions=6, optimized_batch_captions=8)
    write(out/'settings.json', settings)
    # Freeze first, then finalize the new experiment's controls and hashes before
    # any execution. No earlier experiment directory is touched or resumed.
    experiment = read(out/'experiment.json')
    for key in ('transition', 'transition_provenance', 'adaptive', 'initial_run', 'formal_order', 'main_control', 'baseline'):
        experiment.pop(key, None)
    experiment.update(arms=ARMS, parent='adaptive-comparison-30-20261023-r4',
        baseline='unchanged adaptive_entity.run, question/frame-directed scheme 4',
        treatment='optimized_entity.run, source spans, batched planning, events and hybrid retrieval',
        formal_order='alternate control/treatment order per question on one physical GPU',
        smoke_gate='optimized execution, grounding, coverage, budgets and disk-reload refill; failed control reported separately',
        control_failures='Retained and counted; cannot silently remove failed control questions from accuracy denominator',
        ablation='Additional direct QA on optimized arm same selected evidence and same visual supplement; separately budgeted/evaluation-only',
        scope='Development cohort, not a held-out performance claim')
    write(out/'experiment.json', experiment)
    write(out/'resource_policy.json', dict(preferred_gpu='3', minimum_free_mib=56*1024,
        stable_observations=3, poll_seconds=30, shared_load=True,
        note='Admission check cannot reserve memory against unrelated processes; no automatic failed-run retries'))
    controls = list(read(out/'control_hashes.json')) + ['resource_policy.json']
    write(out/'control_hashes.json', {n: original.sha(out/n) for n in controls})
    original.verify(out)


def refill_probe(out, packet, directory, backend, cfg):
    from script_core import optimized_memory as memory
    from script_core.optimized_entity import extraction_input
    from script_core.budget import BudgetLedger, Meter
    uid = packet['question']['video_id']
    target = out/'verification/refill'/uid
    if (target/'result.json').exists():
        return read(target/'result.json')
    if (target/'calls.jsonl').exists():
        raise ValueError('Interrupted refill retains its spent budget; choose a fresh experiment')
    ledger = BudgetLedger(600, answer_reserve=0, calls_path=target/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, target/'raw_calls')
    result = dict(passed=False, scope='real-model source-location and incremental-reload probe, no accuracy label')
    try:
        primary = read(out/'smoke/arms/optimized_scheme4/runs'/uid/'result.json')
        sid = primary['opened_scenes'][0]
        selected = set(primary['read_caption_ids'])
        scene = next(s for s in directory['scenes'] if s['id'] == sid)
        sources = [c for c in packet['captions'] if c['id'] in selected and c['id'] in scene['caption_ids']][:4]
        m = memory.new_memory(uid, packet['captions'], memory.VERSION, directory)
        plan = dict(gap='Find one explicit object mention', focus=['one object'], fields=['state'], depth='entity')
        query = dict(question='Which object is explicitly mentioned in these sources?', options=[])
        inp = extraction_input(m, sources, sid, query, plan)
        for attempt in range(2):
            report, call = meter.json('optimized_extract', inp, ('entities', 'relations', 'events', 'unresolved'))
            try:
                memory.commit_delta(m, report, sources, sid, dict(request_key='probe-anchor'), call['call_id'], plan['fields'], plan['depth'])
                if not m['entities']:
                    raise ValueError('No explicit object anchor was extracted')
                break
            except ValueError as exc:
                if attempt:
                    raise
                inp['validation_feedback'] = str(exc)
        write(target/'memory.json', m)
        before = deepcopy(m)
        m = memory.load_memory(target/'memory.json', uid, packet['captions'], memory.VERSION, directory)
        eid = next(iter(m['entities']))
        refs = m['entities'][eid]['mention_caption_ids']
        sources = [m['source_captions'][cid] for cid in refs]
        plan.update(gap='Supplement only explicit color or material; absent is allowed',
                    focus=[m['entities'][eid]['label']], fields=['color', 'material'])
        query['question'] = 'What color or material is explicitly stated for ' + m['entities'][eid]['label'] + '?'
        inp = extraction_input(m, sources, sid, query, plan)
        inp['entities'] = [e for e in inp['entities'] if e['id'] == eid]
        report, call = meter.json('optimized_extract', inp, ('entities', 'relations', 'events', 'unresolved'))
        if any(e['entity_ref'] != eid for e in report['entities']):
            raise ValueError('Refill created a new instance instead of using the requested entity ID')
        memory.commit_delta(m, report, sources, sid, dict(request_key='probe-refill'), call['call_id'], plan['fields'], plan['depth'])
        if not set(before['observations']) <= m['observations'].keys():
            raise ValueError('Refill removed previous observations')
        write(target/'memory.json', m)
        memory.load_memory(target/'memory.json', uid, packet['captions'], memory.VERSION, directory)
        result.update(passed=True, reloaded_from_disk=True, previous_observations_preserved=True,
                      entity_id=eid, source_caption_ids=refs, rejected_fields=m['rejected_fields'])
    except Exception as exc:
        result['error'] = dict(type=type(exc).__name__, message=str(exc))
    finally:
        result['passed'] &= not ledger.budget_violation
        result['cost_summary'] = ledger.summary()
        write(target/'result.json', result)
        meter.close()
    return result


def same_evidence_ablation(out, uid, phase, packet, result, backend, cfg):
    from script_core.budget import BudgetLedger, Meter
    from script_core.compatibility import qa_inputs
    target = out/phase/'ablation'/uid
    if (target/'result.json').exists():
        return
    if (target/'calls.jsonl').exists():
        raise ValueError('Interrupted ablation retains its spent budget')
    ledger = BudgetLedger(120, answer_reserve=30, calls_path=target/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, target/'raw_calls')
    ablation = dict(answer=None, error=None, selected_seconds=result['selected_seconds'],
        memory_answer=(result.get('answer') or {}).get('selected_option'), evaluation_only=True)
    try:
        inp = qa_inputs(dict(packet['question'], id=packet['question']['question_id']), packet['captions'], result['selected_seconds'])
        observations = result['supplemental_visual']['observations']
        if observations:
            inp['prompts'][-1] += '\nSupplemental sampled visual observations:\n' + json.dumps(observations, ensure_ascii=False)
        checked, _ = meter.call('temporal_qa_check', inp, final=True)
        answer, _ = meter.call('videotree_qa', inp, final=True)
        ablation.update(answer=answer['data'], input_tokens=checked['input_tokens'])
    except Exception as exc:
        ablation['error'] = dict(type=type(exc).__name__, message=str(exc))
    finally:
        ablation['cost_summary'] = ledger.summary()
        write(target/'result.json', ablation)
        meter.close()


def report(out):
    truths = read(out/'evaluation_only.json')
    cohort = read(out/'cohort.json')
    arms = {}
    for arm in ARMS:
        rows = [read(out/'formal/arms'/arm/'runs'/u/'result.json') for u in cohort
                if (out/'formal/arms'/arm/'runs'/u/'result.json').exists()]
        costs = [r['total_measured_seconds'] for r in rows]
        calls = [json.loads(line) for u in cohort
                 if (f := out/'formal/arms'/arm/'runs'/u/'calls.jsonl').exists()
                 for line in f.read_text().splitlines() if line.strip()]
        arms[arm] = dict(completed=len(rows), cohort_size=len(cohort),
            errors=sum(bool(r.get('error')) for r in rows),
            correct=sum(not r.get('error') and str((r.get('answer') or {}).get('selected_option')) == str(truths[r['question_id']]) for r in rows),
            denominator_policy='all attempted cohort questions, including failures',
            mean_seconds=statistics.mean(costs) if costs else None,
            median_seconds=statistics.median(costs) if costs else None,
            p90_seconds=sorted(costs)[max(0, int(.9*len(costs)+.999)-1)] if costs else None,
            mean_read_captions=statistics.mean(r['read_caption_count'] for r in rows) if rows else None,
            mean_planning_calls=statistics.mean(r.get('planning_calls', len(r.get('reviews', []))) for r in rows) if rows else None,
            input_tokens=sum(c.get('input_tokens', 0) or 0 for c in calls),
            output_tokens=sum(c.get('output_tokens', 0) or 0 for c in calls),
            extraction_calls=sum(c['operation'] in ('adaptive_extract', 'optimized_extract', 'optimized_events') for c in calls),
            planning_seconds=sum(c['actual_cost'] for c in calls if c['operation'] in ('adaptive_next', 'optimized_plan')),
            extraction_seconds=sum(c['actual_cost'] for c in calls if c['operation'] in ('adaptive_extract', 'optimized_extract', 'optimized_events')),
            rejected_fields=sum(r.get('rejected_field_count', 0) for r in rows),
            incomplete=sum(bool(r.get('incomplete')) for r in rows))
    ablations = [read(out/'formal/ablation'/u/'result.json') for u in cohort if (out/'formal/ablation'/u/'result.json').exists()]
    write(out/'report.json', dict(updated_at=now(), arms=arms, same_evidence_ablation=dict(
        completed=len(ablations), direct_correct=sum(not a['error'] and str((a['answer'] or {}).get('selected_option')) == str(truths[u])
            for u in cohort if (out/'formal/ablation'/u/'result.json').exists()
            for a in [read(out/'formal/ablation'/u/'result.json')]),
        mean_extra_seconds=statistics.mean(a['cost_summary']['total'] for a in ablations) if ablations else None),
        includes_shared_directory_cost=True, excludes_shared_asset_preparation_and_cold_setup=True,
        ablation_cost_separate=True, shared_gpu_timing=True, development_cohort=True))


def worker(out):
    original.verify(out)
    from script_core.backend import preflight
    from script_core.budget import BudgetLedger, Meter, ModelWorker
    from script_core.temporal_pipeline import directory
    from script_core.adaptive_entity import run as control
    from script_core.optimized_entity import run as optimized
    from script.run_four_arm_300_local import make_packet
    from script.run_temporal_entity import checked_packet
    import torch
    ids = read(out/'cohort.json')
    cfg = load_config(out/'config.json')
    settings = read(out/'settings.json')
    questions = {q['video_id']: q for q in read(out/'questions.json')}
    captions = read(out/'captions.json')
    write(out/'progress.json', dict(stage='asset_preparation', updated_at=now()))
    started = time.monotonic()
    subprocess.run([sys.executable, str(ROOT/'data_extraction/extract_images.py'), '--video-ids', *ids], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT/'data_extraction/extract_features.py'),
        '--model', cfg['models']['visual_encoder'], '--video-ids', *ids, '--batch-size', '4'], cwd=ROOT, check=True)
    write(out/'input_hashes.json', {})
    for uid in ids:
        make_packet(out, uid, questions[uid], captions[uid], cfg)
    write(out/'asset_preparation.json', dict(wall_seconds=time.monotonic()-started, shared_by_all_arms=True))
    cfg, artifacts = preflight(cfg)
    write(out/'artifacts.json', artifacts)
    backend = ModelWorker(cfg)
    try:
        ledger = BudgetLedger(600, answer_reserve=0, calls_path=out/'setup/calls.jsonl')
        meter = Meter(backend, ledger, cfg, out/'setup/raw_calls')
        try:
            meter.call('prepare_vlm', {}, build=True)
            meter.call('clip_text', dict(texts=['video']), build=True)
        finally:
            write(out/'setup/cost_summary.json', ledger.summary())
            meter.close()

        def execute(uid, arm, phase):
            target = out/phase/'arms'/arm/'runs'/uid
            if (target/'result.json').exists():
                return read(target/'result.json')
            if (target/'calls.jsonl').exists():
                raise ValueError('Interrupted query retains its spent budget; use a fresh run')
            packet = checked_packet(out, uid, check_images=True)
            directory_target = out/phase/'directories'/uid
            if (directory_target/'directory.json').exists():
                d = read(directory_target/'directory.json')
            else:
                if (directory_target/'calls.jsonl').exists():
                    raise ValueError('Interrupted directory retains its spent budget')
                ledger = BudgetLedger(settings['directory_seconds'], answer_reserve=0, calls_path=directory_target/'calls.jsonl')
                meter = Meter(backend, ledger, cfg, directory_target/'raw_calls')
                try:
                    features = torch.load(packet['feature_uri'], map_location='cpu', weights_only=True).tolist()
                    d = directory(packet, features, meter, settings)
                    d['cost_summary'] = ledger.summary()
                    write(directory_target/'directory.json', d)
                finally:
                    meter.close()
            write(out/'progress.json', dict(stage=phase, question_id=uid, arm=arm, updated_at=now()))
            start = time.monotonic()
            implementation = optimized if arm == 'optimized_scheme4' else control
            result = implementation(packet, deepcopy(d), packet['frames'], backend, cfg, target, settings,
                memory_path=out/phase/'memory'/arm/f'{uid}.json')
            result.update(directory_cost_summary=d['cost_summary'],
                total_measured_seconds=d['cost_summary']['total']+result['cost_summary']['total'],
                wall_seconds=time.monotonic()-start, shared_directory=True)
            write(target/'directory.json', d)
            write(target/'result.json', result)
            if arm == 'optimized_scheme4' and result.get('answer') and not result.get('error'):
                same_evidence_ablation(out, uid, phase, packet, result, backend, cfg)
            return result

        failures, control_failures = [], []
        for uid in ids[:2]:
            r = execute(uid, 'optimized_scheme4', 'smoke')
            if r.get('error') or not r.get('answer') or r.get('incomplete') or r['cost_summary']['budget_violation'] or r['directory_cost_summary']['budget_violation']:
                failures.append(dict(arm='optimized_scheme4', video_id=uid, error=r.get('error'), incomplete=r.get('incomplete')))
            d = read(out/'smoke/directories'/uid/'directory.json')
            probe = refill_probe(out, checked_packet(out, uid, check_images=False), d, backend, cfg)
            if not probe['passed']:
                failures.append(dict(stage='source_link_refill', video_id=uid, result=probe))
        write(out/'verification/gate.json', dict(passed=not failures, failures=failures,
            control_failures=control_failures, accuracy_not_used_for_gate=True, updated_at=now()))
        if failures:
            write(out/'progress.json', dict(stage='smoke_failed', updated_at=now()))
            return 1
        # Validate the changed path and refill before spending model time on
        # unchanged controls. Formal runs still alternate the two arm orders.
        for uid in ids[:2]:
            r = execute(uid, 'adaptive_scheme4', 'smoke')
            if r.get('error') or not r.get('answer') or r.get('incomplete') or r['cost_summary']['budget_violation']:
                control_failures.append(dict(arm='adaptive_scheme4', video_id=uid, error=r.get('error'), incomplete=r.get('incomplete')))
        write(out/'verification/gate.json', dict(passed=True, failures=[], control_failures=control_failures,
            accuracy_not_used_for_gate=True, updated_at=now()))
        for i, uid in enumerate(ids):
            for arm in ARMS if i % 2 == 0 else reversed(ARMS):
                execute(uid, arm, 'formal')
            report(out)
        write(out/'progress.json', dict(stage='complete', examples=len(ids), updated_at=now()))
        return 0
    finally:
        backend.close()


def inventory():
    output = subprocess.check_output(['nvidia-smi', '--query-gpu=index,uuid,memory.free,utilization.gpu',
        '--format=csv,noheader,nounits'], text=True, timeout=20)
    return {v[0].strip(): dict(uuid=v[1].strip(), free_mib=int(v[2]), utilization=int(v[3]))
            for line in output.splitlines() if len(v := line.split(',')) == 4}


def queue(out, poll=30, allowed_gpus=('1', '2', '3'), worker_script='run_optimized_comparison.py'):
    original.verify(out)
    policy = read(out/'resource_policy.json')
    allowed_gpus = set(allowed_gpus)
    if not allowed_gpus:
        raise ValueError('At least one physical GPU must be allowed')
    if worker_script not in ('run_optimized_comparison.py', 'run_five_comparison.py'):
        raise ValueError('Unknown comparison worker')
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write(out/'scheduling_policy.json', dict(allowed_gpu_indices=sorted(allowed_gpus),
            updated_at=now(), minimum_free_mib=policy['minimum_free_mib'],
            stable_observations=policy['stable_observations'], poll_seconds=poll))
        stable = {}
        while True:
            missing = original.missing_assets(out)
            gpus = {index: row for index, row in inventory().items() if index in allowed_gpus}
            stable = {index: stable.get(index, 0)+1 if row['free_mib'] >= policy['minimum_free_mib'] else 0
                      for index, row in gpus.items()}
            write(out/'queue_status.json', dict(state='waiting_for_assets' if missing else 'waiting_for_gpu_memory',
                updated_at=now(), pid=os.getpid(), allowed_gpu_indices=sorted(allowed_gpus),
                missing_assets=missing, gpus=gpus, stable_observations=stable))
            order = sorted(gpus, key=lambda i: (i != policy['preferred_gpu'], -gpus[i]['free_mib']))
            for index in order if not missing else []:
                row = gpus[index]
                if stable[index] < policy['stable_observations']:
                    continue
                with (Path('/tmp')/f'videoagent-{os.getuid()}-{row["uuid"]}.lock').open('a') as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    fresh = inventory().get(index)
                    if not fresh or fresh['uuid'] != row['uuid'] or fresh['free_mib'] < policy['minimum_free_mib']:
                        continue
                    env = dict(os.environ, CUDA_VISIBLE_DEVICES=row['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                        HF_HOME=str(ROOT/'checkpoints/.cache/huggingface'), HF_MODULES_CACHE=str(ROOT/'checkpoints/.cache/modules'),
                        HF_HUB_OFFLINE='1', TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
                    with (out/'worker.log').open('a') as log:
                        proc = subprocess.Popen([sys.executable, '-u', str(out/'source/script'/worker_script),
                            'worker', '--out', str(out)], cwd=out/'source', env=env, stdout=log, stderr=subprocess.STDOUT)
                    while proc.poll() is None:
                        write(out/'queue_status.json', dict(state='running', updated_at=now(), pid=os.getpid(),
                            worker_pid=proc.pid, gpu_index=index, gpu_uuid=row['uuid'],
                            progress=read(out/'progress.json') if (out/'progress.json').exists() else None))
                        time.sleep(min(poll, 30))
                    write(out/'queue_status.json', dict(state='complete' if proc.returncode == 0 else 'failed',
                        updated_at=now(), exit_code=proc.returncode, gpu_uuid=row['uuid']))
                    return proc.returncode
            time.sleep(poll)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'queue', 'worker', 'status'])
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--count', type=int, default=30)
    parser.add_argument('--poll-seconds', type=int, default=30)
    parser.add_argument('--allowed-gpus', nargs='+', default=['1', '2', '3'],
                        help='Allowed physical GPU indices; defaults to 1 2 3 (excludes GPU 0)')
    parser.add_argument('--detach', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve()
    if args.mode == 'prepare':
        freeze(out, args.count)
        if args.detach:
            session = 'script-optimized-' + str(int(time.time()))
            command = [sys.executable, '-u', str(out/'source/script/run_optimized_comparison.py'), 'queue', '--out', str(out),
                       '--allowed-gpus', *args.allowed_gpus]
            script = out/'start_queue.sh'
            script.write_text('#!/bin/bash\nset -euo pipefail\ncd ' + shlex.quote(str(out/'source')) + '\nexec ' +
                shlex.join(command) + ' >> ' + shlex.quote(str(out/'controller.log')) + ' 2>&1\n')
            subprocess.run(['tmux', 'new-session', '-d', '-s', session, 'bash ' + shlex.quote(str(script))], check=True)
            write(out/'launch.json', dict(session=session, command=command, created_at=now(), python=sys.executable))
        print(json.dumps(dict(out=str(out), queued=args.detach), indent=2))
        return 0
    if args.mode == 'status':
        print(json.dumps(read(out/'queue_status.json'), ensure_ascii=False, indent=2))
        return 0
    try:
        return worker(out) if args.mode == 'worker' else queue(out, args.poll_seconds, args.allowed_gpus)
    except Exception:
        write(out/('worker_failure.json' if args.mode == 'worker' else 'controller_failure.json'),
              dict(updated_at=now(), traceback=traceback.format_exc()))
        raise


if __name__ == '__main__':
    raise SystemExit(main())
