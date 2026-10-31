"""Frozen original-3/4, partial-extraction transition and frame-directed-4 comparison."""
import argparse
from copy import deepcopy
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script_core.config import digest, load_config
from script_core.state import write_json as write
from script_core.temporal_entity import SETTINGS

ARMS = ('summary_deep', 'temporal_cross', 'adaptive_partial', 'adaptive_scheme4')


def arm_order(index):
    # Keep the original 3->4 dependency while rotating both experimental variants.
    return (ARMS, (ARMS[2], ARMS[3], ARMS[0], ARMS[1]),
            (ARMS[3], ARMS[0], ARMS[1], ARMS[2]))[index % 3]


def now():
    return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def freeze(out, count):
    if out.exists():
        raise ValueError('Select a new output directory; previous runs retain their spent budgets')
    data = ROOT/'data/EgoSchema/egoschema'
    annotations = read(data/'subset_anno.json')
    captions = read(data/'lavila_subset.json')
    if not 2 <= count <= min(300, len(annotations)):
        raise ValueError('Require 2-300 distinct validation questions from the frozen cohort rule')
    # Match the documented cohort rule where available, without claiming the old files exist here.
    from script.run_four_arm_300_local import cohort_ids
    ids = cohort_ids(annotations)[:count]
    questions = [dict(question_id=u, video_id=u, question=annotations[u]['question'],
        options=[annotations[u][f'option {i}'] for i in range(5)]) for u in ids]
    out.mkdir(parents=True)
    write(out/'cohort.json', ids)
    write(out/'questions.json', questions)
    write(out/'captions.json', {u: [dict(id=f'C{i}', interval=[i, i+1], text=t)
        for i, t in enumerate(captions[u])] for u in ids})
    write(out/'evaluation_only.json', {u: annotations[u]['truth'] for u in ids})
    cfg = load_config(ROOT/'configs/script.yaml')
    write(out/'config.json', cfg)
    write(out/'settings.json', dict(SETTINGS, adaptive_initial_frames=2,
                                   adaptive_expand_frames=3, adaptive_max_steps=16))
    write(out/'experiment.json', dict(created_at=now(), arms=ARMS, examples=count,
        smoke_ids=ids[:2], smoke_gate='execution, grounding, budgets and source-link refill; no accuracy gate',
        initial_run='original 3, original 4, preserved r1 transition, adaptive 4 smoke',
        formal_order='rotate original (3 then 4) pair, preserved transition and adaptive 4 on one physical GPU',
        shared='same immutable source packet and question-independent directory; source directory cost charged to each arm',
        memory_policy='fresh per video for primary accuracy; isolated real-model refill probe for smoke',
        baseline='existing script_core.temporal_pipeline.run(summary_deep), unchanged',
        main_control='original scheme 4: run_cross_replay using the exact scheme-3 pre-answer bundle',
        transition='preserved r1 adaptive_partial: read whole opened scenes, question-directed partial extraction, source links and incremental independent observations; no frame sampling or cross-stage identity',
        transition_provenance=dict(reference='r1 frozen script_core/adaptive_entity.py',
            preserved_as='script_core/adaptive_transition.py',
            original_algorithm_ast_sha256='96cc15ca2d9aa77f4fdfc9a2b87fe3583fc670b7eb76c3c4aea8881ffb65d49a',
            adapter_changes='Only module docstring and operation routing names; original algorithm and contract text retained'),
        adaptive='scheme-4 state assembler and identity validation; query/gap-ranked scenes AND frames, sampled captions only',
        scope='30-question development validation by default, not a held-out performance claim',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()))
    source = out/'source'
    for folder in ('script_core', 'script', 'tests', 'configs', 'kmeans_pytorch', 'data_extraction'):
        for p in (ROOT/folder).rglob('*'):
            if p.is_file() and p.suffix in ('.py', '.yaml', '.json') and '__pycache__' not in p.parts:
                dest = source/p.relative_to(ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
    for p in ROOT.glob('*.py'):
        shutil.copy2(p, source/p.name)
    shutil.copy2(ROOT/'requirements.txt', source/'requirements.txt')
    for name in ('data', 'checkpoints'):
        (source/name).symlink_to((ROOT/name).resolve(), target_is_directory=True)
    files = [p for p in source.rglob('*') if p.is_file() and
             not any(t in ('data', 'checkpoints') for t in p.relative_to(source).parts)]
    write(out/'source_hashes.json', {str(p.relative_to(source)): sha(p) for p in files})
    controls = ('cohort.json', 'questions.json', 'captions.json', 'config.json', 'settings.json',
                'experiment.json', 'evaluation_only.json')
    write(out/'control_hashes.json', {n: sha(out/n) for n in controls})
    (out/'local_changes.patch').write_bytes(subprocess.check_output(['git', 'diff', '--binary'], cwd=ROOT))
    write(out/'queue_status.json', dict(state='prepared', updated_at=now()))


def verify(out):
    for name, expected in read(out/'source_hashes.json').items():
        if sha(out/'source'/name) != expected:
            raise ValueError('Frozen source changed: ' + name)
    for name, expected in read(out/'control_hashes.json').items():
        if sha(out/name) != expected:
            raise ValueError('Frozen control changed: ' + name)


def missing_assets(out):
    cfg = read(out/'config.json')
    missing = []
    for kind in ('visual_encoder', 'vlm', 'detector', 'text_embedding'):
        folder = Path(cfg['models'][kind])
        if not (folder/'config.json').is_file():
            missing.append(str(folder/'config.json'))
        indexes = list(folder.glob('*.index.json'))
        weights = {v for p in indexes for v in read(p).get('weight_map', {}).values()}
        if not weights:
            weights = {'model.safetensors'} if kind in ('vlm', 'detector') else {'pytorch_model.bin'}
        missing.extend(str(folder/n) for n in weights if not (folder/n).is_file())
    data = ROOT/'data/EgoSchema/egoschema/videos'
    missing.extend(str(data/f'{u}.mp4') for u in read(out/'cohort.json') if not (data/f'{u}.mp4').is_file())
    return sorted(missing)


def inventory():
    value = subprocess.check_output(['nvidia-smi',
        '--query-gpu=index,uuid,memory.used,utilization.gpu', '--format=csv,noheader,nounits'],
        text=True, timeout=20)
    return {p[0].strip(): dict(uuid=p[1].strip(), memory_mib=int(p[2]), utilization=int(p[3]))
            for line in value.splitlines() if len(p := line.split(',')) == 4}


def is_idle(row):
    return row['memory_mib'] <= 500 and row['utilization'] <= 5


def queue(out, poll=30):
    """Persistent local waiting queue. Never evicts another GPU process."""
    verify(out)
    with (out/'controller.lock').open('a') as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return queue_loop(out, poll)


def queue_loop(out, poll):
    stable = {}
    while True:
        missing = missing_assets(out)
        try:
            gpus = inventory()
        except (subprocess.SubprocessError, ValueError) as exc:
            write(out/'queue_status.json', dict(state='waiting_for_gpu_inventory',
                updated_at=now(), pid=os.getpid(), error=str(exc)))
            time.sleep(poll)
            continue
        for index, row in gpus.items():
            stable[index] = stable.get(index, 0) + 1 if is_idle(row) else 0
        write(out/'queue_status.json', dict(state='waiting_for_assets' if missing else 'waiting_for_idle_gpu',
            updated_at=now(), pid=os.getpid(), missing_asset_count=len(missing),
            missing_assets=missing, gpus=gpus, idle_observations=stable,
            required_idle_observations=3, poll_seconds=poll))
        if not missing:
            for index, row in gpus.items():
                if stable[index] < 3:
                    continue
                gpu_lock = Path('/tmp')/f'videoagent-{os.getuid()}-{row["uuid"]}.lock'
                lease = gpu_lock.open('a')
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lease.close()
                    continue
                current = inventory().get(index)
                if not current or current['uuid'] != row['uuid'] or not is_idle(current):
                    lease.close()
                    continue
                env = dict(os.environ, CUDA_VISIBLE_DEVICES=row['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HOME=str(ROOT/'checkpoints/.cache/huggingface'),
                    HF_MODULES_CACHE=str(ROOT/'checkpoints/.cache/modules'), HF_HUB_OFFLINE='1',
                    TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='4', MKL_NUM_THREADS='4',
                    PYTHONUNBUFFERED='1')
                command = [sys.executable, '-u', str(out/'source/script/run_adaptive_comparison.py'),
                           'worker', '--out', str(out)]
                with (out/'worker.log').open('a') as log:
                    proc = subprocess.Popen(command, cwd=out/'source', env=env,
                                            stdout=log, stderr=subprocess.STDOUT)
                while proc.poll() is None:
                    write(out/'queue_status.json', dict(state='running', updated_at=now(),
                        pid=os.getpid(), worker_pid=proc.pid, gpu_index=index, gpu_uuid=row['uuid'],
                        progress=read(out/'progress.json') if (out/'progress.json').exists() else None))
                    time.sleep(min(poll, 30))
                code = proc.returncode
                lease.close()
                write(out/'queue_status.json', dict(state='complete' if code == 0 else 'failed',
                    updated_at=now(), pid=os.getpid(), exit_code=code, gpu_uuid=row['uuid'],
                    report=str(out/'report.json'), log=str(out/'worker.log')))
                return code
        time.sleep(poll)


def source_link_probe(out, packet, directory, backend, cfg, arm='adaptive_scheme4'):
    """Real-model refill on saved/reloaded memory; independent of primary QA/scoring."""
    from script_core import adaptive_entity as implementation
    if arm == 'adaptive_partial':
        from script_core import adaptive_transition as implementation
    commit_delta, entity_profiles = implementation.commit_delta, implementation.entity_profiles
    load_memory, new_memory = implementation.load_memory, implementation.new_memory
    rebuild_indexes, validate_memory = implementation.rebuild_indexes, implementation.validate_memory
    VERSION = implementation.VERSION
    operation = 'transition_extract' if arm == 'adaptive_partial' else 'adaptive_extract'
    from script_core.budget import BudgetLedger, Meter
    uid = packet['question']['video_id']
    target = out/'verification/refill'/arm/uid
    if (target/'result.json').exists():
        return read(target/'result.json')
    if (target/'calls.jsonl').exists():
        raise ValueError('Interrupted refill retains its spent budget; use a new experiment directory')
    ledger = BudgetLedger(600, answer_reserve=0, calls_path=target/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, target/'raw_calls')
    result = dict(passed=False, scope='functional source-link refill, no accuracy label')
    try:
        selected_run = read(out/'smoke/arms'/arm/'runs'/uid/'result.json')
        scene = next(s for s in directory['scenes'] if s['id'] == selected_run['opened_scenes'][0])
        selected_ids = set(selected_run.get('read_caption_ids',
            [packet['captions'][i]['id'] for i in selected_run.get('selected_seconds', [])]))
        sources = [c for c in packet['captions'] if c['id'] in scene['caption_ids'] and c['id'] in selected_ids][:4]
        memory = new_memory(uid, packet['captions'], VERSION)
        rebuild_indexes(memory)
        initial, call = meter.json(operation, dict(question='Which objects are mentioned in these captions?',
            options=[], gap='Build only one or two source-linked object anchors', focus=['main objects'],
            fields=['state'], captions=sources, entities=[], page_size=2, source_type='caption'),
            ('entities', 'relations', 'unresolved'))
        commit_delta(memory, initial, sources, scene['id'], dict(request_key='probe-initial'), call['call_id'])
        if not memory['entities']:
            raise ValueError('No entity produced for the source-link refill check')
        write(target/'memory.json', memory)
        before = deepcopy(memory)
        memory = load_memory(target/'memory.json', uid, packet['captions'], VERSION)
        eid = next(iter(memory['entities']))
        linked = memory['indexes']['entity_to_captions'][eid]
        sources = [memory['source_captions'][c] for c in linked]
        followup, call = meter.json(operation, dict(question='Which color or material is explicitly stated for this object?',
            options=[], gap='Supplement only explicitly stated color/material; unknown can remain absent',
            focus=[memory['entities'][eid]['label']], fields=['color', 'material'], captions=sources,
            entities=entity_profiles(memory, scene['id']), page_size=3, source_type='caption'),
            ('entities', 'relations', 'unresolved'))
        # The follow-up must supplement the requested instance, not create another same-name object.
        if any(e['entity_ref'] != eid for e in followup['entities']):
            raise ValueError('Refill did not preserve the requested entity ID')
        commit_delta(memory, followup, sources, scene['id'], dict(request_key='probe-refill'), call['call_id'])
        validate_memory(memory, uid, packet['captions'], VERSION)
        if not set(before['observations']) <= memory['observations'].keys():
            raise ValueError('Refill dropped existing observations')
        write(target/'memory.json', memory)
        result.update(passed=True, reloaded_from_disk=True, entity_id=eid,
            source_caption_ids=linked, previous_observations_preserved=True,
            followup_attributes=memory['entities'][eid]['attributes'])
    except Exception as exc:
        result['error'] = dict(type=type(exc).__name__, message=str(exc))
    finally:
        result['cost_summary'] = ledger.summary()
        result['passed'] &= not ledger.budget_violation
        write(target/'result.json', result)
        meter.close()
    return result


def worker(out):
    verify(out)
    from script_core.backend import preflight
    from script_core.budget import BudgetLedger, Meter, ModelWorker
    from script_core.temporal_pipeline import directory, run as baseline, run_cross_replay
    from script_core.adaptive_entity import run as adaptive
    from script_core.adaptive_transition import run as transition
    from script.run_four_arm_300_local import make_packet
    from script.run_temporal_entity import checked_packet
    import torch
    ids = read(out/'cohort.json')
    cfg = load_config(out/'config.json')
    questions = {q['video_id']: q for q in read(out/'questions.json')}
    captions = read(out/'captions.json')
    settings = read(out/'settings.json')
    write(out/'progress.json', dict(stage='asset_preparation', updated_at=now()))
    asset_started = time.monotonic()
    subprocess.run([sys.executable, str(ROOT/'data_extraction/extract_images.py'),
        '--video-ids', *ids], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT/'data_extraction/extract_features.py'),
        '--model', cfg['models']['visual_encoder'], '--video-ids', *ids,
        '--batch-size', '4'], cwd=ROOT, check=True)
    write(out/'input_hashes.json', {})
    for uid in ids:
        make_packet(out, uid, questions[uid], captions[uid], cfg)
    write(out/'asset_preparation.json', dict(wall_seconds=time.monotonic()-asset_started,
        shared_by_all_arms=True, feature_hashes={u: sha(out/'inputs'/f'{u}.json') for u in ids}))
    cfg, artifacts = preflight(cfg)
    write(out/'artifacts.json', artifacts)
    backend = ModelWorker(cfg)
    try:
        # Account for one common cold start outside the per-query warm comparison.
        setup_ledger = BudgetLedger(600, answer_reserve=0, calls_path=out/'setup/calls.jsonl')
        setup = Meter(backend, setup_ledger, cfg, out/'setup/raw_calls')
        try:
            setup.call('prepare_vlm', {}, build=True)
            setup.call('clip_text', dict(texts=['video']), build=True)
        finally:
            write(out/'setup/cost_summary.json', setup_ledger.summary())
            setup.close()
        def execute(uid, arm, phase):
            target = out/phase/'arms'/arm/'runs'/uid
            if (target/'result.json').exists():
                return read(target/'result.json')
            if any(target.rglob('calls.jsonl')):
                raise ValueError('Interrupted query retains its spent budget: ' + str(target))
            packet = checked_packet(out, uid, check_images=True)
            directory_target = out/phase/'directories'/uid
            if (directory_target/'directory.json').exists():
                d = read(directory_target/'directory.json')
            else:
                if any(directory_target.rglob('calls.jsonl')):
                    raise ValueError('Interrupted directory retains its spent budget: ' + str(directory_target))
                ledger = BudgetLedger(settings['directory_seconds'], answer_reserve=0,
                    calls_path=directory_target/'calls.jsonl')
                meter = Meter(backend, ledger, cfg, directory_target/'raw_calls')
                try:
                    features = torch.load(packet['feature_uri'], map_location='cpu', weights_only=True)
                    d = directory(packet, features.tolist(), meter, settings)
                    d['cost_summary'] = ledger.summary()
                    write(directory_target/'directory.json', d)
                finally:
                    meter.close()
            write(out/'progress.json', dict(stage=phase, question_id=uid, arm=arm, updated_at=now()))
            started = time.monotonic()
            if arm == 'summary_deep':
                result = baseline(packet, deepcopy(d), packet['frames'], backend, cfg, target,
                                  arm, settings)
            elif arm == 'temporal_cross':
                execute(uid, 'summary_deep', phase)
                source = out/phase/'arms/summary_deep/runs'/uid/'evidence_bundle.json'
                if not source.exists():
                    raise ValueError('Scheme 3 produced no complete pre-answer evidence bundle: ' + uid)
                bundle = read(source)
                result = run_cross_replay(packet, bundle, backend, cfg, target, settings)
                result['shared_preparation_cost_summary'] = bundle['preparation_cost_summary']
                result['shared_evidence_bundle'] = str(source)
            elif arm == 'adaptive_partial':
                result = transition(packet, deepcopy(d), packet['frames'], backend, cfg, target,
                    settings, memory_path=out/phase/'memory'/arm/f'{uid}.json')
            else:
                result = adaptive(packet, deepcopy(d), packet['frames'], backend, cfg, target,
                    settings, memory_path=out/phase/'memory'/arm/f'{uid}.json')
            result.update(directory_cost_summary=d['cost_summary'],
                total_measured_seconds=d['cost_summary']['total'] + result['cost_summary']['total'],
                wall_seconds=time.monotonic()-started, shared_directory=True)
            write(target/'directory.json', d)
            write(target/'result.json', result)
            return result
        failures = []
        for arm in ARMS:
            for uid in ids[:2]:
                r = execute(uid, arm, 'smoke')
                if (r.get('error') or not r.get('answer') or r.get('incomplete')
                        or r.get('inventory_incomplete') or r['cost_summary']['budget_violation']
                        or r['directory_cost_summary']['budget_violation']):
                    failures.append(dict(arm=arm, video_id=uid, result=r))
        for arm in ('adaptive_partial', 'adaptive_scheme4'):
            for uid in ids[:2]:
                d = read(out/'smoke/directories'/uid/'directory.json')
                probe = source_link_probe(out, checked_packet(out, uid, check_images=False), d, backend, cfg, arm=arm)
                if not probe['passed']:
                    failures.append(dict(stage='source_link_refill', arm=arm, video_id=uid, result=probe))
        write(out/'verification/gate.json', dict(passed=not failures, failures=failures, updated_at=now()))
        if failures:
            return 1
        for i, uid in enumerate(ids):
            for arm in arm_order(i):
                execute(uid, arm, 'formal')
            report(out)
        write(out/'progress.json', dict(stage='complete', updated_at=now(), examples=len(ids)))
        return 0
    finally:
        backend.close()


def report(out):
    truths = read(out/'evaluation_only.json')
    ids = read(out/'cohort.json')
    results = {}
    by_arm = {}
    import statistics
    for arm in ARMS:
        rows = [read(p) for u in ids if (p := out/'formal/arms'/arm/'runs'/u/'result.json').exists()]
        by_arm[arm] = {r['question_id']: r for r in rows}
        correct = sum(not r.get('error') and str((r.get('answer') or {}).get('selected_option')) ==
                      str(truths[r['question_id']]) for r in rows)
        costs = [r['total_measured_seconds'] for r in rows]
        extraction_op = {'adaptive_scheme4': 'adaptive_extract', 'adaptive_partial': 'transition_extract'}.get(
            arm, 'caption_temporal_observe')
        extraction = [r.get('shared_preparation_cost_summary', r['cost_summary'])['by_operation'].get(
            extraction_op, 0.) for r in rows]
        tokens = []
        input_tokens = []
        extraction_calls = 0
        executed_tokens = 0
        read_counts = []
        for r in rows:
            log = out/'formal/arms'/arm/'runs'/r['question_id']/'calls.jsonl'
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            executed_tokens += sum(c.get('output_tokens', 0) for c in calls)
            if arm == 'temporal_cross':
                shared_log = out/'formal/arms/summary_deep/runs'/r['question_id']/'calls.jsonl'
                shared = [json.loads(line) for line in shared_log.read_text().splitlines()]
                calls = shared[:r['shared_preparation_cost_summary']['call_count']] + calls
            read_counts.append(r.get('read_caption_count', len(r.get('selected_seconds', []))))
            tokens.extend(c.get('output_tokens', 0) for c in calls)
            input_tokens.extend(c.get('input_tokens', 0) for c in calls)
            extraction_calls += sum(c['operation'] in ('caption_temporal_observe', 'adaptive_extract', 'transition_extract') for c in calls)
        results[arm] = dict(completed=len(rows), planned=len(ids), correct=correct,
            accuracy=correct/len(rows) if rows else None, errors=sum(bool(r.get('error')) for r in rows),
            mean_total_seconds=statistics.mean(costs) if costs else None,
            median_total_seconds=statistics.median(costs) if costs else None,
            p90_total_seconds=sorted(costs)[max(0, math.ceil(.9*len(costs))-1)] if costs else None,
            mean_extraction_seconds=statistics.mean(extraction) if extraction else None,
            total_online_output_tokens=sum(tokens), total_online_input_tokens=sum(input_tokens),
            executed_online_output_tokens=executed_tokens,
            mean_read_caption_count=statistics.mean(read_counts) if read_counts else None,
            extraction_calls=extraction_calls,
            mean_entity_count=statistics.mean(r['entity_count'] for r in rows) if rows else None)
    common = set.intersection(*(set(by_arm[a]) for a in ARMS))
    def correct(arm, uid):
        r = by_arm[arm][uid]
        return not r.get('error') and str((r.get('answer') or {}).get('selected_option')) == str(truths[uid])
    write(out/'report.json', dict(updated_at=now(), arms=results,
        common_completed=len(common),
        main_comparison='adaptive_scheme4 versus original temporal_cross',
        corrected_by_adaptive=sorted(u for u in common if correct('adaptive_scheme4', u) and not correct('temporal_cross', u)),
        regressed_by_adaptive=sorted(u for u in common if correct('temporal_cross', u) and not correct('adaptive_scheme4', u)),
        transition_comparisons={control: dict(
            corrected=sorted(u for u in common if correct('adaptive_partial', u) and not correct(control, u)),
            regressed=sorted(u for u in common if correct(control, u) and not correct('adaptive_partial', u)))
            for control in ('summary_deep', 'adaptive_scheme4')},
        shared_scheme3_preparation_included_in_scheme4_tokens=True,
        includes_shared_directory_cost=True, excludes_shared_asset_preparation_and_cold_setup=True,
        accuracy_gate=False, development_set=True))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['launch', 'queue', 'worker', 'status'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--num-examples', type=int, default=30)
    parser.add_argument('--poll-seconds', type=int, default=30)
    args = parser.parse_args()
    out = args.out.resolve()
    if args.mode == 'launch':
        freeze(out, args.num_examples)
        script = out/'source/script/run_adaptive_comparison.py'
        session = 'script-adaptive-' + datetime.now(ZoneInfo('Asia/Singapore')).strftime('%Y%m%d-%H%M%S')
        command = [sys.executable, '-u', str(script), 'queue', '--out', str(out),
                   '--poll-seconds', str(args.poll_seconds)]
        start = out/'start.sh'
        start.write_text('#!/usr/bin/env bash\nset -euo pipefail\ncd ' + shlex.quote(str(out/'source')) +
            '\nexec ' + shlex.join(command) + ' >> ' + shlex.quote(str(out/'controller.log')) + ' 2>&1\n')
        subprocess.run(['tmux', 'new-session', '-d', '-s', session,
                        'bash ' + shlex.quote(str(start))], check=True)
        write(out/'launch.json', dict(session=session, command=command, created_at=now(),
            python=sys.executable, source_root=str(out/'source')))
        print(json.dumps(dict(queued=True, session=session, out=str(out)), indent=2))
        return 0
    if args.mode == 'queue':
        try:
            return queue(out, args.poll_seconds)
        except Exception:
            write(out/'queue_status.json', dict(state='failed', updated_at=now(),
                traceback=traceback.format_exc(), log=str(out/'controller.log')))
            raise
    if args.mode == 'worker':
        try:
            return worker(out)
        except Exception:
            write(out/'worker_failure.json', dict(updated_at=now(), traceback=traceback.format_exc()))
            raise
    print(json.dumps(read(out/'queue_status.json'), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
