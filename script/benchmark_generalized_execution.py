"""Separate G execution benchmark: frozen serial versus independent batches 2/4."""
import argparse
import ast
from collections import Counter
from copy import deepcopy
from datetime import datetime
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo
from types import SimpleNamespace

sys.dont_write_bytecode = True
if Path(__file__).with_name('batch_extraction.py').exists():
    import batch_extraction as batch
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from script_core import batch_extraction as batch


def now():
    return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def verify(out):
    for name, expected in read(out / 'frozen_hashes.json').items():
        if sha(out / name) != expected:
            raise ValueError('Frozen execution artifact changed: ' + name)


def load_runtime(directory):
    """Load G's copied runtime without requiring F-only modules."""
    directory = Path(directory).resolve()
    name = '_g_execution_' + hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, directory / '__init__.py',
            submodule_search_locations=[str(directory)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
    return SimpleNamespace(**{part: importlib.import_module(name + '.' + part)
        for part in ['backend', 'context', 'schemas', 'prompts', 'config', 'generalized_entity_memory']})


def prepare(reference, out, question_count=8, source_count=64, prior_attempt=None):
    reference, out = reference.resolve(), out.resolve()
    if out.exists() or reference == out or reference in out.parents or out in reference.parents:
        raise ValueError('Use a new independent output directory')
    if question_count < 1 or source_count < 1:
        raise ValueError('Positive sample sizes required')
    original = read(reference / 'frozen_hashes.json')
    for name, expected in original.items():
        if sha(reference / name) != expected:
            raise ValueError('Reference artifact changed: ' + name)
    policy = read(reference / 'experiment.json')
    if policy['arm'] != 'G_generalized_entities':
        raise ValueError('Expected frozen G reference')
    completed = [u for u in read(reference / 'cohort.json')
        if (reference / 'runs/G_generalized_entities' / u / 'result.json').exists()][:question_count]
    pools = []
    for uid in completed:
        pool = []
        for unit in read(reference / 'normalization_sources' / (uid + '.json'))['units']:
            path = reference / 'materialized' / uid / 'source_results' / (unit['id'] + '.json')
            pool.append(dict(question_id=uid, unit=unit, captured_result=read(path),
                captured_path=str(path), captured_sha256=sha(path)))
        pools.append(pool)
    selected = []
    for index in range(max(map(len, pools), default=0)):
        for pool in pools:
            if index < len(pool):
                selected.append(pool[index])
            if len(selected) == source_count:
                break
        if len(selected) == source_count:
            break
    if not selected:
        raise ValueError('No completed G sources')
    for index, request in enumerate(selected):
        request['id'] = f'R{index:04}'
        request['sample_group'] = 'completed_reference'
    # Replay G's existing, source-only functional cases as well. They cover open
    # states, unknown actors, negation and relations absent from many captions.
    source = reference / 'control/run_generalized_entity_experiment.py'
    if source.exists():
        tree = ast.parse(source.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'functional')
        assignment = next(n for n in function.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'sentences' for t in n.targets))
        sentences = ast.literal_eval(assignment.value)
        for index, text in enumerate(sentences):
            path = reference / 'functional/source_results' / f'U{index}.json'
            selected.append(dict(id=f'T{index:04}', question_id=None,
                unit=dict(id=f'U{index}', text=text), sample_group='functional',
                captured_result=read(path), captured_path=str(path), captured_sha256=sha(path)))
    priors = []
    if prior_attempt is not None:
        if not (prior_attempt / 'failures').exists() or read(prior_attempt / 'queue_status.json')['state'] != 'failed':
            raise ValueError('Prior attempt must be a recorded, stopped failure')
        priors = [dict(path=str(prior_attempt.resolve()), report=read(prior_attempt / 'report.json'),
            failures=[read(p) for p in sorted((prior_attempt / 'failures').glob('*.json'))])]
    out.mkdir(parents=True)
    shutil.copytree(reference / 'runtime/qa', out / 'runtime/qa', symlinks=True,
        ignore=shutil.ignore_patterns('__pycache__'))
    root = Path(__file__).resolve().parents[1]
    for name in ['batch_extraction.py', 'generalized_execution.py']:
        shutil.copy2(root / 'script_core' / name, out / 'runtime/qa/script_core' / name)
    control = out / 'control'
    control.mkdir()
    shutil.copy2(Path(__file__), control / Path(__file__).name)
    shutil.copy2(Path(batch.__file__), control / 'batch_extraction.py')
    for name in ['qa_config.json', 'qa_expected_artifacts.json']:
        shutil.copy2(reference / name, out / name)
    write(out / 'requests.json', selected)
    write(out / 'reference_manifest.json', original)
    write(out / 'experiment.json', dict(created_at=now(), reference=str(reference),
        version='G-independent-source-batch-v1', variants=['serial', 'batch2', 'batch4'],
        prior_attempts=priors,
        questions=completed, sources=len(selected), allowed_gpu_indices=['0', '1', '2', '3'],
        minimum_free_mib=32768, stable_observations=3, poll_seconds=10,
        batch_timeout_seconds=150, shared_gpu_allowed=True,
        selection='Fixed round robin over first completed G questions; no answer or correctness reads.',
        invariants='Frozen G prompt, source-specific schema, precision, 1536 output cap, negation/uncertainty guards, full single role review and original retention.',
        scope='Full source extraction plus role review. No new benchmark QA answers or accuracy claims.',
        timing='Interleaved variants on one resident model; shared-GPU observations are not a controlled speedup.',
        accounting='Each initial/review batch charged once; warmup/setup and all failures retained separately.',
        recovery='No automatic retry, smaller-batch fallback, or adoption into the running G experiment.',
        prefix_cache='Deferred: Qwen-VL positions/cache isolation require separate implementation and validation.'))
    frozen = [out / n for n in ['experiment.json', 'requests.json', 'reference_manifest.json',
        'qa_config.json', 'qa_expected_artifacts.json']]
    frozen += [p for base in [control, out / 'runtime/qa'] for p in base.rglob('*.py') if not p.is_symlink()]
    write(out / 'frozen_hashes.json', {str(p.relative_to(out)): sha(p) for p in frozen})
    verify(out)
    write(out / 'queue_status.json', dict(state='prepared', updated_at=now()))
    report(out)
    return dict(out=str(out), sources=len(selected), questions=len(completed))


def inventory():
    raw = subprocess.check_output(['nvidia-smi', '--query-gpu=index,uuid,utilization.gpu,memory.used,memory.free',
        '--format=csv,noheader,nounits'], text=True)
    rows = {}
    for line in raw.splitlines():
        index, uuid, utilization, used, free = [v.strip() for v in line.split(',')]
        rows[uuid] = dict(index=index, uuid=uuid, utilization=int(utilization), used_mib=int(used),
            free_mib=int(free), compute_pids=[])
    raw = subprocess.check_output(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid',
        '--format=csv,noheader,nounits'], text=True)
    for line in raw.splitlines():
        uuid, pid = [v.strip() for v in line.split(',')]
        if uuid in rows:
            rows[uuid]['compute_pids'].append(pid)
    return rows


def allowed(row, policy):
    return row['index'] in policy['allowed_gpu_indices'] and row['free_mib'] >= policy['minimum_free_mib']


def report(out):
    policy, requests = read(out / 'experiment.json'), read(out / 'requests.json')
    calls = [read(p) for p in sorted((out / 'calls').glob('*.json'))]
    results = {v: {p.stem: read(p) for p in (out / 'results' / v).glob('*.json')}
        for v in policy['variants']}
    serial = results['serial']
    variants = {}
    initial = {v: {r['request_id']: r for c in calls if c['variant'] == v and c['phase'] == 'initial'
        for r in c.get('items', [])} for v in policy['variants']}
    for variant, rows in results.items():
        measured = [c for c in calls if c['variant'] == variant]
        matched = sorted(rows.keys() & serial.keys())
        differences = [uid for uid in matched if rows[uid]['result'] != serial[uid]['result']]
        seconds = sum(c['seconds'] for c in measured)
        variants[variant] = dict(completed=len(rows), planned=len(requests), seconds=seconds,
            mean_seconds_per_source=seconds / len(rows) if rows else None,
            model_batches=len(measured), model_source_requests=sum(len(c['request_ids']) for c in measured),
            role_reviews=sum(bool(r['result']['role_review']) for r in rows.values()),
            source_errors=sum(r['result']['error'] is not None for r in rows.values()),
            retained_original_sources=sum(not r['result']['complete_simple'] for r in rows.values()),
            failed_batches=sum(c['error'] is not None for c in measured),
            paired_serial_sources=len(matched), differing_source_ids=differences,
            exact_source_result_matches=len(matched) - len(differences),
            accepted_fact_matches=sum(rows[u]['result']['accepted'] == serial[u]['result']['accepted'] for u in matched),
            captured_reference_matches=sum(r['matches_captured_reference'] for r in rows.values()),
            initial_input_token_matches=sum(initial[variant][u]['output']['usage']['input_tokens'] ==
                initial['serial'][u]['output']['usage']['input_tokens']
                for u in initial[variant].keys() & initial['serial'].keys()),
            peak_allocated_bytes=max((c['peak_allocated_bytes'] for c in measured), default=0),
            equivalent_on_sample=(len(matched) == len(requests) and not differences
                and all(r['result']['error'] is None for r in rows.values())),
            observed_speed_ratio_vs_serial=None)
    for name, value in variants.items():
        base = variants['serial']
        if (name != 'serial' and value['completed'] == base['completed'] == len(requests)
                and not value['failed_batches'] and not base['failed_batches'] and value['seconds']):
            value['observed_speed_ratio_vs_serial'] = base['seconds'] / value['seconds']
    warmups = [read(p) for p in (out / 'warmups').glob('*.json')]
    setup = read(out / 'setup.json') if (out / 'setup.json').exists() else {}
    result = dict(updated_at=now(), variants=variants,
        complete=all(v['completed'] == len(requests) and not v['failed_batches'] for v in variants.values()),
        setup_seconds=setup.get('seconds', 0), warmup_seconds=sum(w['seconds'] for w in warmups),
        measured_seconds_total=setup.get('seconds', 0) + sum(w['seconds'] for w in warmups) + sum(c['seconds'] for c in calls),
        prior_attempt_measured_seconds=sum(p['report']['measured_seconds_total'] for p in policy.get('prior_attempts', [])),
        scope=policy['scope'], timing=policy['timing'], prefix_cache=policy['prefix_cache'])
    write(out / 'report.json', result)
    return result


def worker(out, gpu_uuid):
    verify(out)
    policy = read(out / 'experiment.json')
    if not gpu_uuid or os.environ.get('CUDA_VISIBLE_DEVICES') != gpu_uuid:
        raise ValueError('Worker requires queue-selected GPU UUID')
    if not allowed(inventory()[gpu_uuid], policy):
        raise RuntimeError('Insufficient free GPU memory before model load')
    if (out / 'setup.json').exists() or (out / 'calls').exists():
        raise ValueError('An existing benchmark attempt cannot be retried automatically')
    runtime = load_runtime(out / 'runtime/qa/script_core')
    execution = importlib.import_module(runtime.backend.__package__ + '.generalized_execution')
    execution.bind_runtime(runtime)
    started = time.monotonic()
    cfg, artifacts = runtime.backend.preflight(runtime.config.load_config(out / 'qa_config.json'))
    expected = read(out / 'qa_expected_artifacts.json')
    for key in ['vlm', 'visual_encoder', 'detector', 'text_embedding']:
        if artifacts[key]['hash'] != expected[key]['hash']:
            raise ValueError('Model snapshot changed: ' + key)
    if artifacts['software'] != expected['software']:
        raise ValueError('Runtime software changed')
    if not allowed(inventory()[gpu_uuid], policy):
        raise RuntimeError('Insufficient free GPU memory after preflight')
    import torch
    extractor = execution.GeneralizedExtractor(cfg, runtime)
    extractor.models.prepare_vlm()
    torch.cuda.synchronize()
    write(out / 'setup.json', dict(seconds=time.monotonic() - started, artifacts=artifacts, updated_at=now()))
    requests = read(out / 'requests.json')
    _, processor = extractor.models.load('vlm')
    write(out / 'validation/initial_prompt_hashes.json', {r['id']: batch.digest(
        execution.render_request(execution.source_inputs(r['unit'], runtime.generalized), runtime, processor)['messages'])
        for r in requests})
    candidates = [r for r in requests if not runtime.generalized.NEGATION.search(runtime.generalized.clean_unit(r['unit'])['text'])
        and not runtime.generalized.UNCERTAINTY.search(runtime.generalized.clean_unit(r['unit'])['text'])]
    if len(candidates) < 4:
        raise ValueError('Four unguarded warmup sources are required')
    controls = {}
    for name, size in [('serial', 1), ('native1', 1), ('batch2', 2), ('batch4', 4)]:
        group = candidates[:size]
        inputs = [execution.source_inputs(r['unit'], runtime.generalized) for r in group]
        rec = dict(name=name, error=None, outputs=[])
        write(out / 'pending_call.json', dict(warmup=name, started_at=now()))
        torch.cuda.synchronize()
        began = time.monotonic()
        extractor.models.deadline = began + policy['batch_timeout_seconds']
        try:
            rec['outputs'] = [extractor.serial(inputs[0])] if name == 'serial' else extractor.generate(inputs)
            torch.cuda.synchronize()
            controls[name] = execution.inspect_output(group[0]['unit'], inputs[0], rec['outputs'][0], runtime)
        except Exception as exc:
            rec['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            rec.update(seconds=time.monotonic() - began, updated_at=now())
            write(out / 'warmups' / (name + '.json'), rec)
            (out / 'pending_call.json').unlink()
        if name == 'native1':
            passed = (controls['serial'] == controls['native1'] and rec['outputs'][0]['usage']['input_tokens'] ==
                read(out / 'warmups/serial.json')['outputs'][0]['usage']['input_tokens'])
            write(out / 'validation/native1_equivalence.json', dict(passed=passed, updated_at=now()))
            if not passed:
                raise ValueError('Size-one executor differs from frozen serial path; stop before benchmark')
    counters = Counter()
    for offset in range(0, len(requests), 4):
        group = requests[offset:offset + 4]
        # Rotate the order to spread shared-GPU temporal load across variants.
        order = policy['variants']
        rotation = (offset // 4) % len(order)
        for variant in order[rotation:] + order[:rotation]:
            size = 1 if variant == 'serial' else int(variant.removeprefix('batch'))

            def invoke(phase, jobs):
                counters[variant] += 1
                rec = dict(variant=variant, phase=phase, request_ids=[u for u, _ in jobs], items=[], error=None)
                write(out / 'pending_call.json', dict(variant=variant, phase=phase,
                    request_ids=rec['request_ids'], started_at=now()))
                torch.cuda.synchronize()
                torch.cuda.reset_peak_memory_stats()
                began = time.monotonic()
                extractor.models.deadline = began + policy['batch_timeout_seconds']
                try:
                    outputs = [extractor.serial(jobs[0][1])] if variant == 'serial' else extractor.generate([inp for _, inp in jobs])
                    torch.cuda.synchronize()
                    rec['items'] = [dict(request_id=uid, inputs=inp, output=output)
                        for (uid, inp), output in zip(jobs, outputs)]
                    return outputs
                except Exception as exc:
                    rec['error'] = f'{type(exc).__name__}: {exc}'
                    raise
                finally:
                    rec.update(seconds=time.monotonic() - began, peak_allocated_bytes=torch.cuda.max_memory_allocated(),
                        observed_gpu=inventory()[gpu_uuid], updated_at=now())
                    write(out / 'calls' / f'{variant}-{counters[variant]:05}.json', rec)
                    (out / 'pending_call.json').unlink()

            values = execution.extract_sources(group, size, runtime, invoke)
            for request in group:
                value = values[request['id']]
                write(out / 'results' / variant / (request['id'] + '.json'), dict(result=value,
                    matches_captured_reference=value == request['captured_result']))
            write(out / 'progress.json', dict(variant=variant, completed_through=offset + len(group), updated_at=now()))
            report(out)
    # Frozen reference hashes only; its mutable reports are expected to advance.
    for name, expected_hash in read(out / 'reference_manifest.json').items():
        if sha(Path(policy['reference']) / name) != expected_hash:
            raise ValueError('Running reference frozen file changed: ' + name)
    write(out / 'validation/reference_unchanged.json', dict(passed=True, updated_at=now()))
    return 0


def queue(out):
    verify(out)
    policy = read(out / 'experiment.json')
    with (out / 'controller.lock').open('a') as controller:
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (out / 'launch.json').exists():
            raise ValueError('Benchmark already attempted; keep previous costs and results')
        stable = Counter()
        while True:
            rows = inventory()
            for uuid, row in rows.items():
                stable[uuid] = stable[uuid] + 1 if allowed(row, policy) else 0
            ready = [r for u, r in rows.items() if stable[u] >= policy['stable_observations']]
            if ready:
                chosen = max(ready, key=lambda r: r['free_mib'])
                lease = (Path('/tmp') / ('videoagent-G-execution-' + chosen['uuid'] + '.lock')).open('a')
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lease.close()
                    stable[chosen['uuid']] = 0
                else:
                    break
            write(out / 'queue_status.json', dict(state='waiting_for_free_memory', controller_pid=os.getpid(),
                minimum_free_mib=policy['minimum_free_mib'], observed=list(rows.values()), updated_at=now()))
            time.sleep(policy['poll_seconds'])
        with lease:
            if not allowed(inventory()[chosen['uuid']], policy):
                raise RuntimeError('GPU memory changed before launch')
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=chosen['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
            command = [sys.executable, '-u', str(Path(__file__).resolve()), 'worker', '--out', str(out), '--gpu-uuid', chosen['uuid']]
            logs = out / 'logs'
            logs.mkdir(exist_ok=True)
            with (logs / 'worker.log').open('a') as log:
                proc = subprocess.Popen(command, cwd=out, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lease.fileno(),))
            launch = dict(controller_pid=os.getpid(), worker_pid=proc.pid, command=command,
                gpu_index=chosen['index'], gpu_uuid=chosen['uuid'], initial_gpu=chosen, started_at=now())
            write(out / 'launch.json', launch)
            while proc.poll() is None:
                pending = out / 'pending_call.json'
                if pending.exists() and time.time() - pending.stat().st_mtime > policy['batch_timeout_seconds'] + 30:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    write(out / 'failures/watchdog.json', dict(pending=read(pending), updated_at=now()))
                report(out)
                write(out / 'queue_status.json', dict(launch, state='running', updated_at=now()))
                time.sleep(5)
            summary = report(out)
            if (out / 'pending_call.json').exists():
                write(out / 'failures/interrupted_call.json', dict(pending=read(out / 'pending_call.json'),
                    elapsed_wall_estimate_seconds=max(0, time.time() - (out / 'pending_call.json').stat().st_mtime), updated_at=now()))
            write(out / 'queue_status.json', dict(launch,
                state='complete' if proc.returncode == 0 and summary['complete'] else 'failed',
                returncode=proc.returncode, updated_at=now()))
            return proc.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'queue', 'worker', 'report'])
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--questions', type=int, default=8)
    parser.add_argument('--sources', type=int, default=64)
    parser.add_argument('--gpu-uuid')
    parser.add_argument('--prior-attempt', type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    try:
        if args.mode == 'prepare':
            if args.reference is None:
                parser.error('prepare requires --reference')
            print(json.dumps(prepare(args.reference, out, args.questions, args.sources, args.prior_attempt)))
        elif args.mode == 'queue':
            return queue(out)
        elif args.mode == 'worker':
            return worker(out, args.gpu_uuid)
        else:
            print(json.dumps(report(out), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        if args.mode != 'prepare' and out.exists():
            write(out / 'failures' / (args.mode + '.json'), dict(error=f'{type(exc).__name__}: {exc}', updated_at=now()))
        raise


if __name__ == '__main__':
    raise SystemExit(main())
