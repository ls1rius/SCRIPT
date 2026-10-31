"""Run disjoint video shards through the unchanged frozen G memory builder."""
import argparse
from collections import Counter
import fcntl
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def partition(ids, weights, count):
    groups, loads = [[] for _ in range(count)], [0] * count
    for uid in sorted(ids, key=lambda u: -weights[u]):
        index = min(range(count), key=lambda i: loads[i])
        groups[index].append(uid)
        loads[index] += weights[uid]
    assert len({u for group in groups for u in group}) == len(ids)
    assert sum(map(len, groups)) == len(ids)
    return groups


def worker(out, index):
    sys.path.insert(0, str(out/'runtime/qa'))
    sys.path.insert(0, str(out/'control'))
    import build_g_rl_memories as builder
    import controlled_memory_io as io
    import compact_protocol as protocol
    ids = read(out/'parallel/assignment.json')['shards'][index]
    original_read, original_write = builder.read, io.write
    original_warm = protocol.warm_backend

    def shard_read(path):
        return ids if Path(path) == out/'cohort.json' else original_read(path)

    def shard_write(path, value):
        if Path(path) == out/'progress.json':
            path = out/'parallel'/f'progress-{index}.json'
        return original_write(path, value)

    def shard_warm(root, mode, label):
        return original_warm(root, mode, f'{label}_shard_{index}')

    # Only execution selection, progress destinations and setup accounting vary.
    # G.verify still reads and validates the original complete cohort manifest.
    builder.read, io.write, protocol.warm_backend = shard_read, shard_write, shard_warm
    try:
        return builder.worker(out)
    finally:
        builder.read, io.write, protocol.warm_backend = original_read, original_write, original_warm


def gpu_info():
    value = subprocess.check_output(['nvidia-smi', '--query-gpu=index,uuid,memory.free',
                                     '--format=csv,noheader,nounits'], text=True)
    return [dict(index=i.strip(), uuid=u.strip(), free_mib=int(f))
            for line in value.splitlines() for i, u, f in [line.split(',')]]


def manager(out):
    root = out/'parallel'
    root.mkdir(exist_ok=True)
    lock = (root/'manager.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assignment = read(root/'assignment.json')
    ids = read(out/'cohort.json')
    flattened = [u for group in assignment['shards'] for u in group]
    reused = assignment['reused']
    if len(flattened) != len(set(flattened)) or set(flattened) & set(reused):
        raise ValueError('Overlapping shard assignments')
    if set(flattened + reused) != set(ids):
        raise ValueError('Shard assignments do not cover the full training set')
    waiting = [i for i, group in enumerate(assignment['shards']) if group]
    active, finished, stable = {}, {}, Counter()
    last_launch = 0.
    while waiting or active:
        for index, (proc, launch) in list(active.items()):
            code = proc.poll()
            if code is not None:
                finished[index] = dict(launch, returncode=code, ended_at=time.time())
                write(root/f'result-{index}.json', finished[index])
                del active[index]
        failed = [i for i, info in finished.items() if info['returncode']]
        observations = gpu_info()
        for gpu in observations:
            stable[gpu['uuid']] = stable[gpu['uuid']] + 1 if gpu['free_mib'] >= 32768 else 0
        counts = Counter(info['gpu']['uuid'] for _, info in active.values())
        eligible = [g for g in observations if stable[g['uuid']] >= 3 and counts[g['uuid']] < 2]
        if waiting and eligible and not failed and time.monotonic()-last_launch >= 30:
            gpu = min(eligible, key=lambda g: (counts[g['uuid']], -g['free_mib']))
            index = waiting.pop(0)
            launch_path = root/f'launch-{index}.json'
            if launch_path.exists():
                raise ValueError('Shard already attempted; explicit recovery required')
            command = [sys.executable, '-u', str(Path(__file__).resolve()), '--out', str(out), '--shard', str(index)]
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=gpu['uuid'], OMP_NUM_THREADS='4', MKL_NUM_THREADS='4')
            with (root/f'shard-{index}.log').open('a') as log:
                proc = subprocess.Popen(command, cwd=out/'runtime/qa', env=env,
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            launch = dict(pid=proc.pid, gpu=gpu, command=command, started_at=time.time())
            write(launch_path, launch)
            active[index] = proc, launch
            last_launch = time.monotonic()
            stable[gpu['uuid']] = 0
        completed = sum((out/'materialized'/uid/'sealed.json').exists() for uid in ids)
        write(out/'progress.json', dict(stage='parallel_extraction', completed=completed, planned=len(ids),
              active_shards={i: info for i, (_, info) in active.items()}, waiting_shards=waiting,
              failed_shards=failed, gpus=observations, updated_at=time.time()))
        if failed and not active:
            raise RuntimeError(f'Shards failed: {failed}; results and costs retained, no automatic retry')
        if waiting or active:
            time.sleep(5)
    for uid in ids:
        if not (out/'materialized'/uid/'sealed.json').exists():
            raise ValueError('Missing completed memory: ' + uid)
    write(out/'progress.json', dict(stage='complete', completed=len(ids), planned=len(ids), updated_at=time.time()))
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--shard', type=int)
    args = parser.parse_args()
    result = manager(args.out.resolve()) if args.shard is None else worker(args.out.resolve(), args.shard)
    raise SystemExit(result)
