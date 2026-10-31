"""Freeze compact evidence evaluation and wait for a genuinely idle GPU."""
import argparse
from copy import deepcopy
import fcntl
import itertools
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import controlled_memory_io as io

ARMS = ('A_original', 'B_compact_text', 'C_compact_text')


def project(inputs):
    from qa_evidence_view import compact_description
    prefix, description, suffix = io.parts(inputs)
    text, audit = compact_description(json.loads(description))
    result = deepcopy(inputs)
    result['prompts'][0] = prefix + text + suffix
    if io.fixed(result) != io.fixed(inputs):
        raise ValueError('Fixed answer protocol changed')
    return result, audit


def verify(out):
    for name, expected in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name) != expected:
            raise ValueError('Frozen artifact changed: '+name)


def prepare_ready(out):
    parent = Path(io.read(out/'experiment.json')['parent'])
    for uid in io.read(out/'cohort.json'):
        target = out/'final_inputs/C_compact_text'/f'{uid}.json'
        if target.exists():
            continue
        source = parent/'final_inputs/C_semantic'/f'{uid}.json'
        seal = parent/'materialized'/uid/'sealed.json'
        if not source.exists() or not seal.exists():
            continue
        for name, expected in io.read(seal).items():
            if io.sha(seal.parent/name) != expected:
                raise ValueError('Parent memory seal changed: '+uid+'/'+name)
        original = io.read(out/'final_inputs/A_original'/f'{uid}.json')
        full = io.read(source)
        if io.fixed(original) != io.fixed(full):
            raise ValueError('Parent protocol changed: '+uid)
        compact, audit = project(full)
        io.write(out/'source_inputs/C_semantic'/f'{uid}.json', full)
        io.write(target, compact)
        seconds = sum(float(json.loads(line).get('actual_cost') or 0)
            for line in (seal.parent/'calls.jsonl').read_text().splitlines())
        io.write(out/'input_seals'/f'{uid}.json', dict(
            parent_input=str(source), parent_input_sha256=io.sha(source),
            parent_memory_seal=str(seal), parent_memory_sha256=io.sha(seal),
            inherited_extraction_seconds=seconds,
            inputs={arm:io.sha(out/'final_inputs'/arm/f'{uid}.json') for arm in ARMS},
            projection=audit, updated_at=io.now()))


def report(out):
    ids = io.read(out/'cohort.json')
    truth = io.read(out/'evaluation_only.json')
    rows = {arm:{uid:io.read(p) for uid in ids
        if (p := out/'runs'/arm/uid/'result.json').exists()} for arm in ARMS}
    common = [uid for uid in ids if all(uid in rows[a] for a in ARMS)]
    correct = lambda a, uid: not rows[a][uid].get('error') and str(rows[a][uid]['selected_option']) == str(truth[uid])
    stats = {}
    for arm, results in rows.items():
        n = len(results)
        right = sum(correct(arm, uid) for uid in results)
        stats[arm] = dict(completed=n, planned=len(ids), pending=len(ids)-n, correct=right,
            errors=sum(bool(r.get('error')) for r in results.values()),
            accuracy=right/len(ids) if n == len(ids) else None,
            accuracy_among_completed=right/n if n else None,
            mean_qa_seconds=statistics.mean(r['seconds'] for r in results.values()) if n else None,
            mean_input_tokens=statistics.mean(r['input_tokens'] for r in results.values()
                if r.get('input_tokens') is not None) if any(r.get('input_tokens') is not None for r in results.values()) else None)
    comparisons = {}
    for arm in ARMS[1:]:
        corrected = [uid for uid in common if correct(arm, uid) and not correct(ARMS[0], uid)]
        regressed = [uid for uid in common if correct(ARMS[0], uid) and not correct(arm, uid)]
        comparisons[arm+'_vs_A'] = dict(paired=len(common), corrected=corrected, regressed=regressed,
            delta_percentage_points=100*(len(corrected)-len(regressed))/len(common) if common else None)
    new_seconds = sum(float(json.loads(line).get('actual_cost') or 0)
        for stage in ['runs', 'setup'] for f in (out/stage).rglob('calls.jsonl')
        for line in f.read_text().splitlines())
    inherited = sum(io.read(out/'input_seals'/f'{uid}.json')['inherited_extraction_seconds']
        for uid in rows['C_compact_text'])
    result = dict(updated_at=io.now(), planned=len(ids), common_completed=len(common),
        complete=all(len(rs) == len(ids) for rs in rows.values()), arms=stats,
        comparisons=comparisons, new_qa_and_setup_seconds=new_seconds,
        inherited_extraction_seconds_completed_C=inherited,
        normalization_model_calls=0, development_cohort=True,
        scope='Compact presentation only; all read evidence and original old-memory/visual regions retained.')
    io.write(out/'report.json', result)
    return result


def prepare(parent, out, detach):
    if out.exists():
        raise ValueError('Use a new experiment directory')
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root/'script_core'))
    parent_manifest = io.read(parent/'frozen_hashes.json')
    out.mkdir(parents=True)
    for name in ['cohort.json', 'evaluation_only.json', 'historical_only.json',
                 'qa_config.json', 'qa_expected_artifacts.json']:
        shutil.copy2(parent/name, out/name)
    shutil.copytree(parent/'runtime/qa', out/'runtime/qa', symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(parent/'packets', out/'packets')
    control = out/'control'
    control.mkdir()
    for src, name in [(Path(__file__), Path(__file__).name),
            (root/'script_core/qa_evidence_view.py', 'qa_evidence_view.py'),
            (parent/'control/run_controlled_memory_experiment.py', 'compact_protocol.py'),
            (parent/'control/controlled_memory_io.py', 'controlled_memory_io.py')]:
        shutil.copy2(src, control/name)
    for uid in io.read(out/'cohort.json'):
        for arm in ['A_original', 'B_lossless_layout']:
            path = parent/'final_inputs'/arm/f'{uid}.json'
            if io.sha(path) != parent_manifest[str(path.relative_to(parent))]:
                raise ValueError('Frozen parent input changed')
            original = io.read(path)
            if arm == 'A_original':
                io.write(out/'final_inputs'/arm/f'{uid}.json', original)
            else:
                compact, audit = project(original)
                io.write(out/'final_inputs/B_compact_text'/f'{uid}.json', compact)
                io.write(out/'audits/B_compact_text'/f'{uid}.json', audit)
    io.write(out/'experiment.json', dict(created_at=io.now(), parent=str(parent),
        arms=list(ARMS), planned=len(io.read(out/'cohort.json')), minimum_free_mib=43008,
        maximum_idle_utilization=10, require_no_compute_processes=True,
        stable_observations=3, poll_seconds=10, allowed_gpus=['0','1','2','3'],
        evidence_selection=False, original_answer_protocol=True,
        policy='Fresh A/B/C answers, reuse sealed parent normalization; wait for remaining C inputs.',
        failures='No automatic retries; preserve spent failures; infrastructure errors stop.',
        inherited_cost_policy='Parent extraction ledgers linked and counted separately; no new extraction.',
        baseline_gate='First 20 A predictions must match frozen history; no accuracy threshold.'))
    # Dynamic C inputs have independent seals; A/B and all code are fixed now.
    io.write(out/'frozen_hashes.json', {str(p.relative_to(out)):io.sha(p)
        for p in out.rglob('*') if p.is_file() and not p.is_symlink()})
    prepare_ready(out)
    report(out)
    io.write(out/'queue_status.json', dict(state='prepared', updated_at=io.now()))
    if detach:
        command = [sys.executable, '-u', str(control/Path(__file__).name), 'queue', '--out', str(out)]
        with (out/'controller.log').open('a') as log:
            proc = subprocess.Popen(command, cwd=out, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        io.write(out/'controller.json', dict(pid=proc.pid, command=command, started_at=io.now()))
    print(json.dumps(dict(out=str(out), detached=detach), ensure_ascii=False), flush=True)


def inventory():
    raw = subprocess.check_output(['nvidia-smi', '--query-gpu=index,uuid,memory.free,utilization.gpu',
        '--format=csv,noheader,nounits'], text=True)
    apps = subprocess.check_output(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid',
        '--format=csv,noheader,nounits'], text=True)
    busy = {}
    for line in apps.splitlines():
        uuid, pid = [s.strip() for s in line.split(',')]
        busy.setdefault(uuid, []).append(pid)
    result = {}
    for line in raw.splitlines():
        index, uuid, free, utilization = [s.strip() for s in line.split(',')]
        result[index] = dict(uuid=uuid, free_mib=int(free), utilization=int(utilization),
                             compute_pids=busy.get(uuid, []))
    return result


def eligible(row, policy):
    return (row['free_mib'] >= policy['minimum_free_mib']
        and row['utilization'] <= policy['maximum_idle_utilization']
        and not row['compute_pids'])


def next_range(out):
    ids = io.read(out/'cohort.json')
    pending = lambda uid: any(not (out/'runs'/arm/uid/'result.json').exists() for arm in ARMS)
    for i, uid in enumerate(ids):
        if not pending(uid):
            continue
        if not (out/'input_seals'/f'{uid}.json').exists():
            continue
        end = i+1
        while end < min(i+40, len(ids)) and (out/'input_seals'/f'{ids[end]}.json').exists():
            end += 1
        return i, end
    return None


def worker(out, start, end, phase):
    import compact_protocol as protocol
    protocol.io.ARMS = ARMS
    protocol.io.ORDERS = tuple(itertools.permutations(ARMS))
    for uid in io.read(out/'cohort.json')[start:end]:
        seal = io.read(out/'input_seals'/f'{uid}.json')
        for arm, expected in seal['inputs'].items():
            if io.sha(out/'final_inputs'/arm/f'{uid}.json') != expected:
                raise ValueError('Dynamic input seal changed')
    return protocol.qa_worker(out, start, end, phase)


def queue(out):
    verify(out)
    policy = io.read(out/'experiment.json')
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (out/'launch.json').exists():
            raise ValueError('Worker already attempted; inspect retained state')
        stable = {}
        serial = 0
        while True:
            prepare_ready(out)
            summary = report(out)
            if summary['complete']:
                io.write(out/'queue_status.json', dict(state='complete', updated_at=io.now()))
                return 0
            span = next_range(out)
            rows = inventory()
            stable = {i:stable.get(i, 0)+1 if eligible(row, policy) else 0
                      for i, row in rows.items() if i in policy['allowed_gpus']}
            lease = None
            if span:
                for index in sorted(stable, key=lambda i:-rows[i]['free_mib']):
                    if stable[index] < policy['stable_observations']:
                        continue
                    handles = []
                    for family in ['videoagent-finalqa', 'videoagent']:
                        handle = (Path('/tmp')/f'{family}-{os.getuid()}-{rows[index]["uuid"]}.lock').open('a')
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            handles.append(handle)
                        except BlockingIOError:
                            handle.close()
                            break
                    fresh = inventory().get(index)
                    if len(handles) != 2 or not fresh or fresh['uuid'] != rows[index]['uuid'] or not eligible(fresh, policy):
                        for h in handles: h.close()
                        continue
                    lease = handles
                    gpu = index
                    break
            if lease is None:
                io.write(out/'queue_status.json', dict(state='waiting_for_idle_gpu' if span else 'waiting_for_parent_inputs',
                    controller_pid=os.getpid(), ready_range=list(span) if span else None,
                    gpus=rows, stable_observations=stable, updated_at=io.now()))
                time.sleep(policy['poll_seconds'])
                continue
            start, end = span
            if not (out/'validation/baseline_reproduction.json').exists():
                start, end, phase = 0, 20, 'baseline_smoke'
            else:
                phase = f'compact_qa_{start}_{end}_{serial}'
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=rows[gpu]['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
            command = [sys.executable, '-u', str(Path(__file__).resolve()), 'worker', '--out', str(out),
                       '--start', str(start), '--end', str(end), '--phase', phase]
            (out/'logs').mkdir(exist_ok=True)
            try:
                with (out/'logs'/f'{phase}.log').open('a') as log:
                    proc = subprocess.Popen(command, cwd=out, env=env, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, pass_fds=tuple(h.fileno() for h in lease))
                launch = dict(gpu_index=gpu, gpu_uuid=rows[gpu]['uuid'], worker_pid=proc.pid,
                    controller_pid=os.getpid(), phase=phase, start=start, end=end, started_at=io.now())
                io.write(out/'workers'/f'{phase}.json', launch)
                if not (out/'launch.json').exists(): io.write(out/'launch.json', launch)
                while proc.poll() is None:
                    report(out)
                    io.write(out/'queue_status.json', dict(launch, state='running', updated_at=io.now()))
                    time.sleep(5)
                report(out)
                if proc.returncode:
                    io.write(out/'queue_status.json', dict(launch, state='failed', returncode=proc.returncode, updated_at=io.now()))
                    return proc.returncode
            finally:
                for h in lease: h.close()
            stable = {}
            serial += 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'queue', 'worker', 'report'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--detach', action='store_true')
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--end', type=int, default=20)
    parser.add_argument('--phase', default='baseline_smoke')
    args = parser.parse_args()
    out = args.out.resolve()
    try:
        if args.mode == 'prepare':
            prepare(args.parent.resolve(), out, args.detach)
        elif args.mode == 'queue':
            raise SystemExit(queue(out))
        elif args.mode == 'worker':
            raise SystemExit(worker(out, args.start, args.end, args.phase))
        else:
            print(json.dumps(report(out), ensure_ascii=False, indent=2))
    except Exception as exc:
        import traceback
        if out.exists():
            io.write(out/'failures'/f'{args.mode}_{args.phase}.json', dict(type=type(exc).__name__,
                message=str(exc), traceback=traceback.format_exc(), updated_at=io.now()))
        raise
