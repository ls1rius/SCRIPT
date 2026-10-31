"""Independent serial/2/4-source extraction benchmark on an otherwise idle GPU."""
import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
if Path(__file__).with_name('batch_extraction.py').exists():
    import batch_extraction as batch
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from script_core import batch_extraction as batch


def now():return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()
def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n');tmp.replace(path)


def verify(out):
    for name, expected in read(out/'frozen_hashes.json').items():
        if sha(out/name) != expected:raise ValueError('Frozen benchmark artifact changed: '+name)


def prepare(reference, out, question_count=4, request_count=64):
    if out.exists() or reference == out or reference in out.parents:
        raise ValueError('Use a new output directory outside the running reference experiment')
    if question_count < 1 or request_count < 1:raise ValueError('Positive sample sizes required')
    completed = [u for u in read(reference/'cohort.json')
        if (reference/'runs/F_lazy_script'/u/'result.json').exists()][:question_count]
    if not completed:raise ValueError('Reference has no completed F source extractions')
    pools = []
    for uid in completed:
        pool = []
        # Read completed calls only; never access the live database or answer labels.
        for line in (reference/'materialized'/uid/'calls.jsonl').read_text().splitlines():
            call = json.loads(line)
            if call['operation'] != 'f_extract_fields' or call.get('error'):continue
            raw_path = Path(call['output_ref']);raw = read(raw_path)
            if not raw.get('output') or raw.get('error'):continue
            inputs = raw['inputs']
            if set(inputs) != {'unit', 'requested_fields', 'source_type'}:
                raise ValueError('Reference request is not source-only')
            if batch.digest(inputs) != call['input_hash']:
                raise ValueError('Captured input hash changed')
            pool.append(dict(question_id=uid, inputs=inputs,
                captured_output=raw['output'], captured_path=str(raw_path), captured_sha256=sha(raw_path)))
        pools.append(pool)
    # Round robin over fixed question order; selection does not inspect answers.
    selected = []
    for index in range(max(map(len, pools), default=0)):
        for pool in pools:
            if index < len(pool):selected.append(pool[index])
            if len(selected) == request_count:break
        if len(selected) == request_count:break
    if not selected:raise ValueError('No successful source extraction requests to compare')
    for index, request in enumerate(selected):request['id'] = f'R{index:04}'
    parent_policy = read(reference/'experiment.json')
    protected = [reference]
    if parent_policy.get('E_run'):protected.append(Path(parent_policy['E_run']))
    protected_launches = {str(p): read(p/'launch.json') for p in protected}
    protected_uuids = sorted({v['gpu_uuid'] for v in protected_launches.values()})
    out.mkdir(parents=True)
    shutil.copytree(reference/'runtime/qa', out/'runtime/qa', symlinks=True,
        ignore=shutil.ignore_patterns('__pycache__'))
    for name in ['qa_config.json', 'qa_expected_artifacts.json']:shutil.copy2(reference/name, out/name)
    control = out/'control';control.mkdir()
    shutil.copy2(Path(__file__), control/Path(__file__).name)
    shutil.copy2(Path(batch.__file__), control/'batch_extraction.py')
    write(out/'requests.json', selected)
    write(out/'experiment.json', dict(created_at=now(), reference=str(reference),
        questions=completed, requests=len(selected), variants=['serial', 'batch2', 'batch4'],
        protected_launches=protected_launches, protected_gpu_uuids=protected_uuids,
        allowed_gpu_indices=['1', '2'], minimum_free_mib=32768, stable_observations=3,
        poll_seconds=10, batch_timeout_seconds=150,
        policy='One resident model; independent source prompt/schema per row. Original F fields, precision and prompts.',
        resource_policy='Never share a busy GPU; exclude E/F GPUs and leave their processes, files and databases unchanged.',
        selection='Round robin over the first completed reference questions, without reading answers or labels.',
        accounting='Each batch wall time charged once; no duplicate per-source wall-time charges.',
        failures='No automatic retry, split or smaller-batch fallback after failure.',
        scope='Extraction throughput and output agreement only; no final-QA accuracy claim.'))
    frozen = [out/'requests.json', out/'experiment.json', out/'qa_config.json', out/'qa_expected_artifacts.json']
    frozen += [p for base in [control, out/'runtime/qa'] for p in base.rglob('*.py') if not p.is_symlink()]
    write(out/'frozen_hashes.json', {str(p.relative_to(out)): sha(p) for p in frozen})
    verify(out)
    write(out/'queue_status.json', dict(state='prepared', updated_at=now()))
    report(out)
    print(json.dumps(dict(out=str(out), requests=len(selected), questions=len(completed), state='prepared')))


def inventory():
    text = subprocess.check_output(['nvidia-smi', '--query-gpu=index,uuid,utilization.gpu,memory.used,memory.free',
        '--format=csv,noheader,nounits'], text=True)
    gpu_rows = {}
    for line in text.splitlines():
        i, uuid, utilization, used, free = [v.strip() for v in line.split(',')]
        gpu_rows[uuid] = dict(index=i, uuid=uuid, utilization=int(utilization), used_mib=int(used),
            free_mib=int(free), compute_pids=[])
    active = subprocess.check_output(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid',
        '--format=csv,noheader,nounits'], text=True)
    for line in active.splitlines():
        values = [v.strip() for v in line.split(',')]
        if len(values) == 2 and values[0] in gpu_rows:gpu_rows[values[0]]['compute_pids'].append(values[1])
    return gpu_rows


def allowed(row, policy):
    return batch.eligible_gpu(row, policy['protected_gpu_uuids'], policy['allowed_gpu_indices'], policy['minimum_free_mib'])


def report(out):
    requests = read(out/'requests.json');policy = read(out/'experiment.json')
    calls = [read(p) for p in sorted((out/'batches').glob('*.json'))] if (out/'batches').exists() else []
    result = batch.benchmark_report(requests, calls, policy['variants'])
    result['setup_seconds'] = read(out/'setup.json')['seconds'] if (out/'setup.json').exists() else 0
    warmups = [read(p) for p in (out/'warmups').glob('*.json')] if (out/'warmups').exists() else []
    result['warmup_seconds'] = sum(w['seconds'] for w in warmups)
    result['measured_seconds_total'] = result['setup_seconds']+result['warmup_seconds']+sum(c['seconds'] for c in calls)
    interrupted = out/'failures/interrupted_batch.json'
    result['interrupted_unlogged_wall_estimate_seconds'] = read(interrupted)['elapsed_wall_estimate_seconds'] if interrupted.exists() else 0
    result.update(updated_at=now(), requests=len(requests), reference=policy['reference'])
    write(out/'report.json', result)
    return result


def queue(out):
    verify(out);policy = read(out/'experiment.json')
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Benchmark already attempted; preserve existing results and costs')
        stable = {}
        while True:
            rows = inventory()
            for uuid, row in rows.items():stable[uuid] = stable.get(uuid, 0)+1 if allowed(row, policy) else 0
            ready = [r for uuid, r in rows.items() if stable.get(uuid, 0) >= policy['stable_observations']]
            if ready:
                chosen = max(ready, key=lambda row:row['free_mib'])
                lease = (Path('/tmp')/('videoagent-extraction-batch-'+chosen['uuid']+'.lock')).open('a')
                try:fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lease.close();stable[chosen['uuid']] = 0;ready = []
                if ready:break
            write(out/'queue_status.json', dict(state='waiting_for_idle_gpu', controller_pid=os.getpid(),
                protected_gpu_uuids=policy['protected_gpu_uuids'], allowed_gpu_indices=policy['allowed_gpu_indices'],
                observed=list(rows.values()), updated_at=now()))
            time.sleep(policy['poll_seconds'])
        with lease:
            # Recheck after acquiring the lease and before creating a GPU worker.
            if not allowed(inventory()[chosen['uuid']], policy):raise RuntimeError('GPU became occupied before launch')
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=chosen['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
            logs = out/'logs';logs.mkdir(exist_ok=True)
            command = [sys.executable, '-u', str(Path(__file__).resolve()), 'worker', '--out', str(out),
                '--gpu-uuid', chosen['uuid']]
            with (logs/'worker.log').open('a') as log:
                proc = subprocess.Popen(command, cwd=out, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, pass_fds=(lease.fileno(),), start_new_session=True)
            launch = dict(controller_pid=os.getpid(), worker_pid=proc.pid, gpu_uuid=chosen['uuid'],
                gpu_index=chosen['index'], started_at=now(), command=command)
            write(out/'launch.json', launch)
            while proc.poll() is None:
                pending = out/'pending_batch.json'
                if pending.exists() and time.time()-pending.stat().st_mtime > policy['batch_timeout_seconds']+30:
                    # Only this isolated worker process is terminated; no external GPU process is touched.
                    proc.terminate()
                    try:proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait()
                    write(out/'failures/watchdog.json', dict(error='Batch hard deadline exceeded', pending=read(pending), updated_at=now()))
                report(out)
                write(out/'queue_status.json', dict(launch, state='running', updated_at=now()))
                time.sleep(5)
            summary = report(out)
            pending = out/'pending_batch.json'
            if pending.exists():
                write(out/'failures/interrupted_batch.json', dict(pending=read(pending),
                    elapsed_wall_estimate_seconds=max(0,time.time()-pending.stat().st_mtime), updated_at=now()))
                summary = report(out)
            write(out/'queue_status.json', dict(launch, state='complete' if proc.returncode == 0 and summary['complete'] else 'failed',
                returncode=proc.returncode, updated_at=now()))
            return proc.returncode


def worker(out, gpu_uuid):
    verify(out);policy = read(out/'experiment.json')
    if not gpu_uuid or os.environ.get('CUDA_VISIBLE_DEVICES') != gpu_uuid:
        raise ValueError('Use the idle-GPU queue to launch an isolated worker')
    if not allowed(inventory()[gpu_uuid], policy):raise RuntimeError('Worker refuses to load a model on a protected or occupied GPU')
    if (out/'setup.json').exists() or (out/'batches').exists():raise ValueError('Existing benchmark attempt cannot be automatically retried')
    runtime = batch.load_runtime(out/'runtime/qa/script_core')
    started = time.monotonic()
    cfg, artifacts = runtime.backend.preflight(runtime.config.load_config(out/'qa_config.json'))
    expected = read(out/'qa_expected_artifacts.json')
    for key in ['vlm', 'visual_encoder', 'detector', 'text_embedding']:
        if artifacts[key]['hash'] != expected[key]['hash']:raise ValueError('Model snapshot changed: '+key)
    if artifacts['software'] != expected['software']:raise ValueError('Runtime software changed')
    if not allowed(inventory()[gpu_uuid], policy):raise RuntimeError('GPU became occupied during CPU preflight')
    import torch
    extractor = batch.BatchExtractor(cfg, runtime)
    extractor.models.prepare_vlm()
    torch.cuda.synchronize()
    write(out/'setup.json', dict(seconds=time.monotonic()-started, artifacts=artifacts, updated_at=now()))
    requests = read(out/'requests.json')
    # Record exact rendered prompts before running controls or microbatches.
    _, processor = extractor.models.load('vlm')
    prompt_hashes = {r['id']: batch.digest(batch.render_request(r['inputs'], runtime, processor)['messages']) for r in requests}
    write(out/'validation/prompt_hashes.json', prompt_hashes)
    # Record warmup costs and test the native size-one path against the unchanged
    # frozen F dispatch before measuring any proposed speedup.
    controls = {}
    for name, size in [('serial',1),('native1',1),('batch2',2),('batch4',4)]:
        group = requests[:size]
        record = dict(name=name, request_ids=[r['id'] for r in group], outputs=[], error=None)
        write(out/'pending_batch.json', dict(warmup=name, request_ids=record['request_ids'], started_at=now()))
        started = time.monotonic();extractor.models.deadline = started+policy['batch_timeout_seconds']
        failure = None
        try:
            outputs = ([extractor.serial(group[0]['inputs'])] if name == 'serial'
                else extractor.generate([r['inputs'] for r in group]))
            torch.cuda.synchronize()
            record['outputs'] = outputs
            controls[name] = batch.inspect_output(group[0]['inputs'],outputs[0],runtime)
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}';failure = exc
        finally:
            record.update(seconds=time.monotonic()-started, updated_at=now())
            write(out/'warmups'/(name+'.json'),record);(out/'pending_batch.json').unlink()
            report(out)
        if failure is not None:raise failure
        if name == 'native1':
            native_tokens = record['outputs'][0]['usage']['input_tokens']
            serial_tokens = read(out/'warmups/serial.json')['outputs'][0]['usage']['input_tokens']
            passed = (controls['serial']['error'] is None and controls['native1']['error'] is None
                and controls['serial']['data'] == controls['native1']['data'] and native_tokens == serial_tokens)
            write(out/'validation/serial_equivalence.json',dict(passed=passed,
                input_tokens_match=native_tokens==serial_tokens, updated_at=now()))
            if not passed:raise ValueError('Native size-one execution differs from the frozen F control; benchmark stopped')
    for variant in policy['variants']:
        size = 1 if variant == 'serial' else int(variant.removeprefix('batch'))
        for offset in range(0, len(requests), size):
            group = requests[offset:offset+size]
            write(out/'progress.json', dict(variant=variant, completed_sources=offset, planned=len(requests), updated_at=now()))
            record = dict(variant=variant, offset=offset, request_ids=[r['id'] for r in group], items=[], error=None)
            write(out/'pending_batch.json', dict(variant=variant, offset=offset, request_ids=record['request_ids'], started_at=now()))
            torch.cuda.synchronize();torch.cuda.reset_peak_memory_stats()
            started = time.monotonic()
            extractor.models.deadline = started+policy['batch_timeout_seconds']
            failure = None
            try:
                outputs = ([extractor.serial(group[0]['inputs'])] if variant == 'serial'
                    else extractor.generate([r['inputs'] for r in group]))
                torch.cuda.synchronize()
                if len(outputs) != len(group):raise ValueError('Missing or extra batch responses')
                for request, output in zip(group, outputs):
                    inspected = batch.inspect_output(request['inputs'], output, runtime)
                    captured = batch.inspect_output(request['inputs'], request['captured_output'], runtime)
                    record['items'].append(dict(request_id=request['id'], output=output, inspection=inspected,
                        matches_captured_reference=inspected['data'] is not None and inspected['data'] == captured['data'],
                        prompt_sha256=prompt_hashes[request['id']]))
            except Exception as exc:
                record['error'] = f'{type(exc).__name__}: {exc}';failure = exc
            finally:
                record.update(seconds=time.monotonic()-started, peak_allocated_bytes=torch.cuda.max_memory_allocated(), updated_at=now())
                write(out/'batches'/f'{variant}-{offset:05}.json', record)
                (out/'pending_batch.json').unlink()
                report(out)
            if failure is not None:raise failure
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'queue', 'worker', 'report'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--questions', type=int, default=4)
    parser.add_argument('--requests', type=int, default=64)
    parser.add_argument('--gpu-uuid')
    args = parser.parse_args();out = args.out.resolve()
    try:
        if args.mode == 'prepare':
            if args.reference is None:parser.error('prepare requires --reference')
            prepare(args.reference.resolve(), out, args.questions, args.requests)
        elif args.mode == 'queue':return queue(out)
        elif args.mode == 'worker':return worker(out, args.gpu_uuid)
        else:print(json.dumps(report(out), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Never write errors into an existing reference directory on prepare failure.
        if args.mode != 'prepare' and out.exists():write(out/'failures'/(args.mode+'.json'), dict(error=f'{type(exc).__name__}: {exc}', updated_at=now()))
        raise


if __name__ == '__main__':raise SystemExit(main())
