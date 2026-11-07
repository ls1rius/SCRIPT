"""GPU admission. Shared memory admission is the default; legacy exclusivity is explicit."""
from contextlib import contextmanager, ExitStack
import fcntl
import os
from pathlib import Path
import subprocess
import time


def preferences(policy):
    values = policy.get('preference') or str(policy.get('gpu_index', '0,1,2,3')).split(',')
    values = list(dict.fromkeys(str(v).strip() for v in values))
    if not values or any(not v.isdigit() for v in values):
        raise ValueError('GPU indices must be comma-separated integers')
    return values


def inventory():
    raw = subprocess.check_output(['nvidia-smi',
        '--query-gpu=index,uuid,memory.free,utilization.gpu,memory.total,memory.used',
        '--format=csv,noheader,nounits'], text=True)
    rows = {}
    for line in raw.splitlines():
        index, uuid, free, utilization, total, used = [v.strip() for v in line.split(',')]
        rows[index] = dict(index=index, uuid=uuid, free_mib=int(free), utilization=int(utilization),
                           total_mib=int(total),used_mib=int(used))
    return rows


def eligible(row, policy):
    return (row['free_mib'] >= policy.get('minimum_free_mib', 32768)
            and row['utilization'] <= policy.get('maximum_utilization', 10))


@contextmanager
def acquire_exclusive(policy, on_wait):
    indices = preferences(policy)
    stable = {index: 0 for index in indices}
    while True:
        rows = inventory()
        if any(index not in rows for index in indices):
            raise ValueError('Requested GPU is absent from nvidia-smi inventory')
        for index in indices:
            row = rows[index]
            stable[index] = stable[index] + 1 if eligible(row, policy) else 0
        candidates = indices
        if policy.get('selection') == 'most_free_memory':
            candidates = sorted(indices, key=lambda i: (-rows[i]['free_mib'], rows[i]['utilization'], indices.index(i)))
        for index in candidates:
            row = rows[index]
            if stable[index] < policy.get('stable_observations', 3):
                continue
            leases = ExitStack()
            try:
                # Honour both namespaces used by existing H and final-QA jobs.
                handles = []
                for namespace in ['finalqa', 'H']:
                    handle = leases.enter_context((Path('/tmp') /
                        f'videoagent-{namespace}-{os.getuid()}-{row["uuid"]}.lock').open('a'))
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    handles.append(handle)
            except BlockingIOError:
                leases.close()
                continue
            try:
                current = inventory()[index]
                if current['uuid'] != row['uuid'] or not eligible(current, policy):
                    stable[index] = 0
                    continue
                yield current, tuple(handle.fileno() for handle in handles)
                return
            finally:
                leases.close()
        on_wait(dict(watched_gpu_indices=indices, gpu_observations=rows,
                     stable_observations=dict(stable),
                     minimum_free_mib=policy.get('minimum_free_mib', 32768),
                     maximum_utilization=policy.get('maximum_utilization', 10)))
        time.sleep(10)


@contextmanager
def acquire(policy, on_wait):
    if policy.get('admission_mode') == 'exclusive':
        with acquire_exclusive(policy,on_wait) as lease:yield lease
    else:
        from script.gpu_shared_queue import acquire as shared
        with shared(policy,on_wait) as lease:yield lease
