"""Pipeline shared extraction and frozen QA experiments on GPUs 0 and 3."""
import argparse
from contextlib import ExitStack
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

PARENT_ARMS = ('A_original', 'B_lossless_layout', 'C_semantic')
COMPACT_ARMS = ('A_original', 'B_compact_text', 'C_compact_text')


def read(path):
    return json.loads(Path(path).read_text())


def process(pid):
    try:
        fields = Path('/proc', str(pid), 'stat').read_text().rsplit(') ', 1)[1].split()
        return dict(alive=fields[0] != 'Z', start_ticks=fields[19])
    except OSError:
        return dict(alive=False)


def alive(job):
    p = process(job['pid'])
    return p['alive'] and p.get('start_ticks') == job['start_ticks']


def overlaps(start, end, job):
    return start < job['end'] and end > job['start']


def next_normalize(parent, ids, active):
    for start in range(0, len(ids), 20):
        end = min(start+20, len(ids))
        if any(j['kind'] == 'normalize' and overlaps(start, end, j) for j in active):
            continue
        missing = [u for u in ids[start:end] if not (parent/'final_inputs/C_semantic'/f'{u}.json').exists()]
        if not missing:
            continue
        if any((parent/'materialized'/u).exists() for u in missing):
            raise ValueError('Unowned, partially spent extraction; explicit recovery needed')
        return start, end
    return None


def next_qa(root, ids, kind, active):
    arms = PARENT_ARMS if kind == 'parent_qa' else COMPACT_ARMS
    def ready(index):
        uid = ids[index]
        return (not any(j['kind'] == kind and j['start'] <= index < j['end'] for j in active)
            and all((root/'final_inputs'/a/f'{uid}.json').exists() for a in arms)
            and ((root/'materialized'/uid/'sealed.json').exists() if kind == 'parent_qa'
                 else (root/'input_seals'/f'{uid}.json').exists()))
    for start, uid in enumerate(ids):
        if all((root/'runs'/a/uid/'result.json').exists() for a in arms) or not ready(start):
            continue
        end = start+1
        while end < min(start+40, len(ids)) and ready(end):
            end += 1
        return start, end
    return None


def complete(job, parent, compact, ids):
    if job['kind'] == 'normalize':
        return all((parent/'materialized'/u/'sealed.json').exists() for u in ids[job['start']:job['end']])
    root, arms = (parent, PARENT_ARMS) if job['kind'] == 'parent_qa' else (compact, COMPACT_ARMS)
    return all((root/'runs'/a/u/'result.json').exists() for a in arms for u in ids[job['start']:job['end']])


def adopt(root, kind, io):
    state = read(root/'queue_status.json')
    pid = state['controller_pid']
    command = Path('/proc', str(pid), 'cmdline').read_bytes().split(b'\0')
    if str(root).encode() not in command or Path('/proc', str(pid)).stat().st_uid != os.getuid():
        raise ValueError('Controller identity mismatch')
    os.kill(pid, signal.SIGSTOP)
    jobs = []
    try:
        children = Path('/proc', str(pid), 'task', str(pid), 'children').read_text().split()
        for child in children:
            info = process(int(child))
            if not info['alive']:
                continue
            tokens = [v.decode() for v in Path('/proc', child, 'cmdline').read_bytes().split(b'\0') if v]
            if '--out' not in tokens or tokens[tokens.index('--out')+1] != str(root):
                raise ValueError('Unexpected controller child')
            job_kind = 'normalize' if kind == 'parent' and 'normalize' in tokens else kind+'_qa'
            jobs.append(dict(kind=job_kind, pid=int(child), start_ticks=info['start_ticks'],
                gpu=str(state['gpu_index']), start=int(tokens[tokens.index('--start')+1]),
                end=int(tokens[tokens.index('--end')+1]), phase=tokens[tokens.index('--phase')+1], adopted=True))
        os.kill(pid, signal.SIGTERM)
        os.kill(pid, signal.SIGCONT)
        for _ in range(30):
            if not process(pid)['alive']:
                break
            time.sleep(.1)
        return jobs
    finally:
        if process(pid)['alive']:
            os.kill(pid, signal.SIGCONT)


def run(parent, compact, control, resume_state=None, normalization_only=False, minimum_free_mib=43008):
    if normalization_only and compact is not None:
        raise ValueError('Normalization-only continuation requires parent-only mode')
    if compact is not None:
        sys.path.insert(0, str(compact/'control'))
    import run_compact_evidence_experiment as cr
    spec = importlib.util.spec_from_file_location('parent_frozen_io', parent/'control/controlled_memory_io.py')
    io = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(io)
    for name, expected in read(parent/'frozen_hashes.json').items():
        if io.sha(parent/name) != expected:
            raise ValueError('Parent frozen artifact changed: '+name)
    if compact is not None:
        cr.verify(compact)
    roots = [parent] if compact is None else [parent, compact]
    for root in roots:
        if not read(root/'validation/baseline_reproduction.json')['passed']:
            raise ValueError('Baseline reproduction did not pass')
    ids = read(parent/'cohort.json')
    if compact is not None and ids != read(compact/'cohort.json'):
        raise ValueError('Cohorts differ')
    if resume_state is not None:
        state = read(resume_state)
        if state['parent'] != str(parent):
            raise ValueError('Handoff parent differs')
        active = state['active']
        if compact is None and any(j['kind'] not in ('normalize', 'parent_qa') for j in active):
            raise ValueError('Cancelled compact workers must be stopped before handoff')
        for job in active:
            if not alive(job) and not complete(job, parent, compact, ids):
                raise ValueError('Handoff worker failed or disappeared: '+json.dumps(job))
    else:
        if compact is None:
            raise ValueError('Parent-only mode requires an explicit worker handoff')
        active = None
    control.mkdir(parents=True, exist_ok=False)
    (control/'scheduler.py').write_bytes(Path(__file__).read_bytes())
    io.write(control/'request.json', dict(created_at=io.now(), gpus=['0','3'],
        policy='One extraction per GPU; one additional QA lane on GPU 3; ready evidence consumed first.',
        minimum_free_mib=minimum_free_mib, inference_unchanged=True, no_extraction_duplicates=True,
        compact_enabled=compact is not None,
        normalization_only=normalization_only,
        resume_state=str(resume_state) if resume_state is not None else None))
    if active is None:
        active = adopt(parent, 'parent', io) + adopt(compact, 'compact', io)
    io.write(control/'adoption.json', active)
    procs = {}
    leases = {}
    with ExitStack() as stack:
        for root in roots:
            h = stack.enter_context((root/'controller.lock').open('a'))
            fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
            io.write(root/'controller.json', dict(pid=os.getpid(), control=str(control), started_at=io.now()))
        serial = 0
        while True:
            for job in list(active):
                proc = procs.get(job['pid'])
                if (proc is not None and proc.poll() is None) or (proc is None and alive(job)):
                    continue
                if (proc is not None and proc.returncode) or not complete(job, parent, compact, ids):
                    raise ValueError('Worker failed or stopped incompletely: '+json.dumps(job))
                active.remove(job)
            if compact is not None:
                cr.prepare_ready(compact)
            parent_report = io.report(parent)
            if normalization_only and all((parent/'final_inputs/C_semantic'/f'{u}.json').exists()
                    and (parent/'materialized'/u/'sealed.json').exists() for u in ids):
                state=dict(state='normalization_complete_waiting_for_answers',updated_at=io.now(),
                    controller_pid=os.getpid(),active=active,control=str(control))
                io.write(control/'status.json',state)
                io.write(parent/'queue_status.json',state)
                return
            compact_report = cr.report(compact) if compact is not None else dict(complete=True)
            if parent_report['complete'] and compact_report['complete']:
                for root in roots:
                    io.write(root/'queue_status.json', dict(state='complete', updated_at=io.now()))
                return
            inventory = cr.inventory()
            for gpu in ['0','3']:
                row = inventory[gpu]
                norm = next_normalize(parent, ids, active)
                candidate = None
                if norm and not any(j['gpu'] == gpu and j['kind'] == 'normalize' for j in active):
                    candidate = ('normalize', *norm)
                elif not normalization_only and not any(j['gpu'] == gpu and j['kind'] != 'normalize' for j in active):
                    if gpu == '3' or not any(j['gpu'] == gpu for j in active):
                        candidates = [(parent, 'parent_qa')]
                        if compact is not None:
                            candidates.append((compact, 'compact_qa'))
                        for root, kind in candidates:
                            span = next_qa(root, ids, kind, active)
                            if span:
                                candidate = (kind, *span)
                                break
                if candidate is None or row['free_mib'] < minimum_free_mib:
                    continue
                if gpu not in leases:
                    h = (Path('/tmp')/f'videoagent-finalqa-{os.getuid()}-{row["uuid"]}.lock').open('a')
                    try:
                        fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        h.close()
                        continue
                    leases[gpu] = stack.enter_context(h)
                kind, start, end = candidate
                root = compact if kind == 'compact_qa' else parent
                phase = f'pipeline_g{gpu}_{kind}_{start}_{end}_{serial}'
                serial += 1
                entry = root/'control'/('run_compact_evidence_experiment.py' if kind == 'compact_qa'
                                        else 'run_controlled_memory_experiment.py')
                mode = 'worker' if kind == 'compact_qa' else 'normalize' if kind == 'normalize' else 'qa'
                command = [sys.executable,'-u',str(entry),mode,'--out',str(root),
                    '--start',str(start),'--end',str(end),'--phase',phase]
                env = dict(os.environ, CUDA_VISIBLE_DEVICES=row['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                    OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
                with (root/'logs'/f'{phase}.log').open('a') as log:
                    proc = subprocess.Popen(command,cwd=root,env=env,stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,pass_fds=(leases[gpu].fileno(),))
                job = dict(kind=kind,pid=proc.pid,start_ticks=process(proc.pid)['start_ticks'],
                    gpu=gpu,start=start,end=end,phase=phase,adopted=False)
                active.append(job)
                procs[proc.pid] = proc
                io.write(root/'workers'/f'{phase}.json', dict(job, command=command, started_at=io.now()))
            io.write(control/'status.json', dict(state='running',controller_pid=os.getpid(),
                active=active,gpus=inventory,updated_at=io.now()))
            statuses = [(parent, ['normalize', 'parent_qa'])]
            if compact is not None:
                statuses.append((compact, ['compact_qa']))
            for root, kinds in statuses:
                jobs = [j for j in active if j['kind'] in kinds]
                io.write(root/'queue_status.json', dict(state='running' if jobs else 'waiting_for_shared_evidence',
                    controller_pid=os.getpid(),control=str(control),active_workers=jobs,updated_at=io.now()))
            time.sleep(10)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--parent',type=Path,required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--compact',type=Path)
    mode.add_argument('--parent-only',action='store_true')
    p.add_argument('--resume-state',type=Path)
    p.add_argument('--normalization-only',action='store_true')
    p.add_argument('--minimum-free-mib',type=int,default=43008)
    p.add_argument('--control',type=Path,required=True)
    args = p.parse_args()
    try:
        run(args.parent.resolve(), args.compact.resolve() if args.compact is not None else None,
            args.control.resolve(), args.resume_state.resolve() if args.resume_state is not None else None,
            args.normalization_only,args.minimum_free_mib)
    except Exception as exc:
        import traceback
        args.control.mkdir(parents=True,exist_ok=True)
        (args.control/'failure.json').write_text(json.dumps(dict(type=type(exc).__name__,
            message=str(exc),traceback=traceback.format_exc()),ensure_ascii=False,indent=2))
        raise
