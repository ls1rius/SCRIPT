"""Memory-based admission with short atomic locks and live per-job reservations.

Already allocated CUDA memory is counted by nvidia-smi; only the unallocated part
of each admitted job's capacity is additionally reserved. Busy GPUs are eligible.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

REGISTRY = Path('/tmp') / f'videoagent-memory-reservations-{os.getuid()}.json'


def process_snapshot():
    result={}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            # comm can contain whitespace and parentheses; fields after final ')'.
            tail=path.read_text().rsplit(')',1)[1].split()
            if tail[0]=='Z':continue
            result[int(path.parent.name)]=dict(parent=int(tail[1]),start=tail[19])
        except (OSError,ValueError,IndexError):continue
    return result


def allocations():
    raw=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid,used_memory',
                                 '--format=csv,noheader,nounits'],text=True)
    result=[]
    for line in raw.splitlines():
        gpu,pid,used=[v.strip() for v in line.split(',')]
        result.append(dict(uuid=gpu,pid=int(pid),mib=int(used)))
    return result


def effective_free(rows,leases,processes,usage):
    active={key:r for key,r in leases.items() if processes.get(r['pid'],{}).get('start')==r['start']}
    free={i:r['free_mib'] for i,r in rows.items()}
    covered={i:set() for i in rows}
    committed={i:0 for i in rows}
    for reservation in active.values():
        descendants={reservation['pid']}
        while True:
            expanded=descendants|{pid for pid,p in processes.items() if p['parent'] in descendants}
            if expanded==descendants:break
            descendants=expanded
        used=sum(r['mib'] for r in usage if r['uuid']==reservation['uuid'] and r['pid'] in descendants)
        for i,row in rows.items():
            if row['uuid']==reservation['uuid']:
                free[i]-=max(0,reservation['capacity_mib']-used)
                covered[i].update(descendants)
                committed[i]+=reservation['capacity_mib']
    for i,row in rows.items():
        if 'total_mib' in row:
            outside=sum(r['mib'] for r in usage if r['uuid']==row['uuid'] and r['pid'] not in covered[i])
            # Independent hard ceiling: NVML device-free and per-process queries
            # can straddle a cudaMalloc. Never allow that sampling race to spend
            # the same physical memory twice. Driver/system overhead is reserved.
            ceiling=row['total_mib']-committed[i]-outside-1024
            free[i]=min(free[i],ceiling)
    return free,active


@contextmanager
def locked_registry():
    with REGISTRY.with_suffix('.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        leases=json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}
        yield leases
        tmp=REGISTRY.with_suffix(f'.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(leases,indent=2));os.replace(tmp,REGISTRY)


def release(key):
    with locked_registry() as leases:leases.pop(key,None)


@contextmanager
def acquire(policy,on_wait):
    from script.gpu_queue import inventory,preferences
    indices=preferences(policy);stable={i:0 for i in indices}
    capacity=int(policy.get('minimum_free_mib',32768))
    if capacity<=0:raise ValueError('Positive GPU memory capacity required')
    key=f'{os.getpid()}-{uuid.uuid4().hex}'
    admitted=None
    try:
        while admitted is None:
            # Inventory and reservation update are atomic among participating jobs.
            with locked_registry() as leases:
                before=inventory();processes=process_snapshot();usage=allocations();rows=inventory()
                for i,row in rows.items():
                    if i not in before or row['uuid']!=before[i]['uuid']:raise ValueError('GPU inventory changed during admission')
                    row['free_mib']=min(row['free_mib'],before[i]['free_mib'])
                if any(i not in rows for i in indices):raise ValueError('Requested GPU absent')
                free,active=effective_free(rows,leases,processes,usage)
                leases.clear();leases.update(active)
                for i in indices:stable[i]=stable[i]+1 if free[i]>=capacity else 0
                for i in sorted(indices,key=lambda i:(-free[i],indices.index(i))):
                    if stable[i]<policy.get('stable_observations',3):continue
                    admitted=dict(rows[i],effective_free_mib=free[i],reservation_id=key)
                    leases[key]=dict(pid=os.getpid(),start=processes[os.getpid()]['start'],uuid=rows[i]['uuid'],
                        capacity_mib=capacity,created_at=time.time())
                    break
            if admitted is None:
                on_wait(dict(watched_gpu_indices=indices,gpu_observations=rows,effective_free_mib=free,
                    stable_observations=dict(stable),minimum_free_mib=capacity,maximum_utilization=100,
                    scheduling='shared_memory_reservations'))
                time.sleep(10)
        # No whole-card lock is inherited by a worker. Other jobs may use surplus.
        yield admitted,()
    finally:
        release(key)
