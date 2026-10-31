"""Frozen five-method comparison on the common 300-question development cohort."""
import argparse
from copy import deepcopy
import json
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
from script import run_optimized_comparison as optimized_runner
from script_core.config import load_config
from script_core.state import write_json as write

read, now = original.read, original.now
ARMS = (*original.ARMS, 'optimized_scheme4')


def arm_order(index):
    # Rotate four units while retaining the original 3 -> 4 evidence dependency.
    units = [('summary_deep', 'temporal_cross'), ('adaptive_partial',),
             ('adaptive_scheme4',), ('optimized_scheme4',)]
    offset = index % len(units)
    return tuple(arm for unit in units[offset:]+units[:offset] for arm in unit)


def freeze(out, count=300):
    optimized_runner.freeze(out, count)
    experiment = read(out/'experiment.json')
    experiment.update(arms=ARMS, examples=count, parent='five preserved implementations',
        main_control='temporal_cross (original scheme 4)',
        baseline='summary_deep (original scheme 3)',
        treatment='adaptive_partial, adaptive_scheme4, optimized_scheme4',
        formal_order='rotate four units; original 3 always precedes paired original 4',
        smoke_gate='diagnostic only; method errors/incomplete evidence/refill failures remain recorded, do not remove arms or questions',
        control_failures='Every method error counts in the fixed cohort denominator; missing scheme-3 evidence becomes a scheme-4 dependency failure',
        infrastructure_failures='Source/input integrity, setup, asset preparation and unexpected runner failures stop the worker with logs',
        prior_results='Independent fresh 300-question run; earlier 30-question development results are retained separately',
        cohort_rule='Existing deterministic original100 + shuffled200 rule; first 30 match prior development cohort',
        scope='300-question development comparison; not a held-out generalization claim')
    write(out/'experiment.json', experiment)
    policy = read(out/'resource_policy.json')
    policy['allowed_gpu_indices'] = ['1', '2', '3']
    write(out/'resource_policy.json', policy)
    cfg = read(out/'config.json')
    cfg['cache']['persistent_directory'] = str(out/'cache/text_tools')
    cfg['cache']['persistent_scope'] = ''
    write(out/'config.json', cfg)
    experiment['reuse_policy'] = 'Within this experiment only: separate text-tool cache per arm; preprocessing directory reused between smoke and formal; original source processing cost stays in the method budget'
    write(out/'experiment.json', experiment)
    controls = read(out/'control_hashes.json')
    write(out/'control_hashes.json', {name: original.sha(out/name) for name in controls})
    original.verify(out)


def dependency_failure(packet, prior):
    """A failed source cannot silently remove the paired scheme-4 question."""
    return dict(arm='temporal_cross', question_id=packet['question']['question_id'],
        answer=None, error=dict(type='EvidenceDependencyError',
            message='Original scheme 3 did not produce complete pre-answer evidence'),
        incomplete=['missing_or_incomplete_scheme3_evidence'],
        cost_summary=deepcopy(prior['cost_summary']),
        shared_preparation_cost_summary=deepcopy(prior['cost_summary']),
        shared_source_failed=True, entity_count=prior.get('entity_count', 0),
        selected_seconds=prior.get('selected_seconds', []),
        read_caption_count=prior.get('read_caption_count', len(prior.get('selected_seconds', []))),
        dependency_source='summary_deep', executed_online_seconds=0.)


def calls_at(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def report(out):
    ids, truths = read(out/'cohort.json'), read(out/'evaluation_only.json')
    by_arm, summaries = {}, {}
    for arm in ARMS:
        rows = [read(p) for uid in ids
                if (p := out/'formal/arms'/arm/'runs'/uid/'result.json').exists()]
        by_arm[arm] = {r['question_id']: r for r in rows}
        own_calls, attributed = [], []
        for r in rows:
            uid = r['question_id']
            calls = calls_at(out/'formal/arms'/arm/'runs'/uid/'calls.jsonl')
            own_calls.extend(calls)
            if arm == 'temporal_cross' and 'shared_preparation_cost_summary' in r:
                shared = calls_at(out/'formal/arms/summary_deep/runs'/uid/'calls.jsonl')
                calls = shared[:r['shared_preparation_cost_summary']['call_count']] + calls
            attributed.extend(calls)
        correct = sum(not r.get('error') and str((r.get('answer') or {}).get('selected_option')) == str(truths[r['question_id']]) for r in rows)
        costs = [r['total_measured_seconds'] for r in rows]
        summaries[arm] = dict(completed=len(rows), planned=len(ids), correct=correct,
            accuracy_among_attempted=correct/len(rows) if rows else None,
            correct_over_fixed_cohort=correct/len(ids),
            errors=sum(bool(r.get('error')) for r in rows),
            incomplete=sum(bool(r.get('incomplete') or r.get('inventory_incomplete')) for r in rows),
            dependency_failures=sum(bool(r.get('shared_source_failed')) for r in rows),
            mean_seconds=statistics.mean(costs) if rows else None,
            median_seconds=statistics.median(costs) if rows else None,
            p90_seconds=sorted(costs)[max(0, (9*len(costs)+9)//10-1)] if rows else None,
            mean_read_captions=statistics.mean(r.get('read_caption_count', len(r.get('selected_seconds', []))) for r in rows) if rows else None,
            mean_planning_calls=statistics.mean(r.get('planning_calls', len(r.get('reviews', []))) for r in rows) if rows else None,
            input_tokens=sum((c.get('input_tokens', 0) or 0)+c.get('tool_cache', {}).get('source_input_tokens', 0) for c in attributed),
            output_tokens=sum((c.get('output_tokens', 0) or 0)+c.get('tool_cache', {}).get('source_output_tokens', 0) for c in attributed),
            executed_input_tokens=sum(c.get('input_tokens', 0) or 0 for c in own_calls),
            executed_output_tokens=sum(c.get('output_tokens', 0) or 0 for c in own_calls),
            text_cache_hits=sum(bool(c.get('tool_cache', {}).get('hit')) for c in own_calls),
            reused_processing_seconds=sum(r['cost_summary'].get('shared_dependency_seconds', 0.) for r in rows),
            executed_call_seconds=sum(c['actual_cost'] for c in own_calls),
            extraction_calls=sum(c['operation'] in ('caption_temporal_observe', 'transition_extract', 'adaptive_extract', 'optimized_extract', 'optimized_events') for c in attributed),
            rejected_fields=sum(r.get('rejected_field_count', 0) for r in rows))
    common = set.intersection(*(set(by_arm[arm]) for arm in ARMS))
    def correct(arm, uid):
        r = by_arm[arm][uid]
        return not r.get('error') and str((r.get('answer') or {}).get('selected_option')) == str(truths[uid])
    pairs = [('summary_deep', 'temporal_cross'), ('summary_deep', 'adaptive_partial'),
             ('temporal_cross', 'adaptive_scheme4'), ('adaptive_partial', 'adaptive_scheme4'),
             ('adaptive_scheme4', 'optimized_scheme4'), ('temporal_cross', 'optimized_scheme4')]
    comparisons = {}
    for control, treatment in pairs:
        paired = set(by_arm[control]) & set(by_arm[treatment])
        comparisons[f'{treatment}_vs_{control}'] = dict(paired_count=len(paired),
            corrected=sorted(u for u in paired if correct(treatment, u) and not correct(control, u)),
            regressed=sorted(u for u in paired if correct(control, u) and not correct(treatment, u)))
    ablations = {u: read(p) for u in ids if (p := out/'formal/ablation'/u/'result.json').exists()}
    write(out/'report.json', dict(updated_at=now(), arms=summaries,
        planned_total_answers=len(ids)*len(ARMS), common_completed=len(common), comparisons=comparisons,
        denominator_policy='Failed, unanswered and dependency-failed questions stay in the fixed cohort; pending questions separately visible',
        same_evidence_ablation=dict(completed=len(ablations),
            correct=sum(not a.get('error') and str((a.get('answer') or {}).get('selected_option')) == str(truths[u]) for u, a in ablations.items()),
            extra_seconds=sum(a['cost_summary']['total'] for a in ablations.values())),
        includes_shared_directory_cost=True, includes_scheme3_preparation_in_scheme4=True,
        excludes_shared_asset_preparation_and_cold_setup=True,
        ablation_cost_separate=True, shared_gpu_timing=True, development_cohort=True))
    return summaries


def worker(out):
    original.verify(out)
    from script_core.backend import preflight
    from script_core.budget import BudgetLedger, Meter, ModelWorker
    from script_core.temporal_pipeline import directory, run as baseline, run_cross_replay
    from script_core.adaptive_entity import run as adaptive
    from script_core.adaptive_transition import run as transition
    from script_core.optimized_entity import run as optimized
    from script.run_four_arm_300_local import make_packet
    from script.run_temporal_entity import checked_packet
    import torch
    ids = read(out/'cohort.json')
    cfg, settings = load_config(out/'config.json'), read(out/'settings.json')
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
            if any(target.rglob('calls.jsonl')):
                raise ValueError('Interrupted query retains spent budget; use a fresh run: '+str(target))
            packet = checked_packet(out, uid, check_images=True)
            directory_target = out/phase/'directories'/uid
            if (directory_target/'directory.json').exists():
                d = read(directory_target/'directory.json')
            else:
                if any(directory_target.rglob('calls.jsonl')):
                    raise ValueError('Interrupted directory retains spent budget')
                from script_core.directory_cache import load_or_build
                d = load_or_build(out, packet, cfg, settings, backend)
                write(directory_target/'directory.json', d)
            write(out/'progress.json', dict(stage=phase, question_id=uid, arm=arm, updated_at=now()))
            start = time.monotonic()
            if arm == 'summary_deep':
                result = baseline(packet, deepcopy(d), packet['frames'], backend, cfg, target, arm, settings)
            elif arm == 'temporal_cross':
                prior = execute(uid, 'summary_deep', phase)
                source = out/phase/'arms/summary_deep/runs'/uid/'evidence_bundle.json'
                bundle = read(source) if source.exists() else None
                if bundle is None or bundle.get('incomplete'):
                    result = dependency_failure(packet, prior)
                else:
                    result = run_cross_replay(packet, bundle, backend, cfg, target, settings)
                    result['shared_preparation_cost_summary'] = bundle['preparation_cost_summary']
                    result['shared_evidence_bundle'] = str(source)
            else:
                implementation = {'adaptive_partial': transition, 'adaptive_scheme4': adaptive,
                                  'optimized_scheme4': optimized}[arm]
                result = implementation(packet, deepcopy(d), packet['frames'], backend, cfg, target,
                    settings, memory_path=out/phase/'memory'/arm/f'{uid}.json')
            result.update(directory_cost_summary=d['cost_summary'],
                total_measured_seconds=d['cost_summary'].get('budgeted_total', d['cost_summary']['total'])+
                    result['cost_summary'].get('budgeted_total', result['cost_summary']['total']),
                actual_execution_seconds=d['cost_summary']['total']+result['cost_summary']['total'],
                wall_seconds=time.monotonic()-start, shared_directory=True)
            write(target/'directory.json', d)
            write(target/'result.json', result)
            if arm == 'optimized_scheme4' and result.get('answer') and not result.get('error'):
                optimized_runner.same_evidence_ablation(out, uid, phase, packet, result, backend, cfg)
            return result

        issues = []
        for i, uid in enumerate(ids[:2]):
            for arm in arm_order(i):
                r = execute(uid, arm, 'smoke')
                if r.get('error') or not r.get('answer') or r.get('incomplete') or r.get('inventory_incomplete') or r['cost_summary']['budget_violation']:
                    issues.append(dict(arm=arm, video_id=uid, error=r.get('error'), incomplete=r.get('incomplete')))
            d = read(out/'smoke/directories'/uid/'directory.json')
            packet = checked_packet(out, uid, check_images=False)
            for arm in ('adaptive_partial', 'adaptive_scheme4', 'optimized_scheme4'):
                probe = (optimized_runner.refill_probe(out, packet, d, backend, cfg) if arm == 'optimized_scheme4'
                         else original.source_link_probe(out, packet, d, backend, cfg, arm=arm))
                if not probe['passed']:
                    issues.append(dict(stage='refill', arm=arm, video_id=uid, error=probe.get('error')))
        write(out/'verification/gate.json', dict(infrastructure_passed=True, all_method_checks_passed=not issues,
            diagnostic_issues=issues, formal_allowed=True, accuracy_not_used_for_gate=True,
            policy='Compare all frozen methods, retaining method failures in denominator', updated_at=now()))
        for i, uid in enumerate(ids):
            for arm in arm_order(i):
                execute(uid, arm, 'formal')
                report(out)
        write(out/'progress.json', dict(stage='complete', examples=len(ids), arms=list(ARMS), updated_at=now()))
        return 0
    finally:
        backend.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'queue', 'worker', 'status'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--count', type=int, default=300)
    parser.add_argument('--detach', action='store_true')
    parser.add_argument('--allowed-gpus', nargs='+', default=['1', '2', '3'], choices=['1', '2', '3'])
    parser.add_argument('--poll-seconds', type=int, default=30)
    args = parser.parse_args()
    out = args.out.resolve()
    if args.mode == 'prepare':
        freeze(out, args.count)
        if args.detach:
            command = [sys.executable, '-u', str(out/'source/script/run_five_comparison.py'),
                'queue', '--out', str(out), '--allowed-gpus', *args.allowed_gpus,
                '--poll-seconds', str(args.poll_seconds)]
            start = out/'start_queue.sh'
            start.write_text('#!/bin/bash\nset -euo pipefail\ncd '+shlex.quote(str(out/'source'))+
                '\nexec '+shlex.join(command)+' >> '+shlex.quote(str(out/'controller.log'))+' 2>&1\n')
            session = 'script-five-'+str(int(time.time()))
            subprocess.run(['tmux', 'new-session', '-d', '-s', session, 'bash '+shlex.quote(str(start))], check=True)
            write(out/'launch.json', dict(session=session, command=command, created_at=now(), python=sys.executable))
        print(json.dumps(dict(out=str(out), examples=args.count, arms=list(ARMS), queued=args.detach), indent=2))
        return 0
    if args.mode == 'status':
        print(json.dumps(read(out/'queue_status.json'), ensure_ascii=False, indent=2))
        return 0
    try:
        return (worker(out) if args.mode == 'worker' else optimized_runner.queue(
            out, args.poll_seconds, args.allowed_gpus, worker_script='run_five_comparison.py'))
    except Exception:
        write(out/('worker_failure.json' if args.mode == 'worker' else 'controller_failure.json'),
              dict(updated_at=now(), traceback=traceback.format_exc()))
        raise


if __name__ == '__main__':
    raise SystemExit(main())
