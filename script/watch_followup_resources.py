"""Stop a frozen follow-up worker after a CUDA OOM without editing its source."""
import argparse
import fcntl
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.run_followup_validation import read, resource_error, write, now


def stop_worker(out, job, state, question_id, proc_root=Path('/proc'), kill=os.kill):
    pid = state['pid']
    process = proc_root/str(pid)
    if not process.exists():
        return False
    try:
        command = (process/'cmdline').read_bytes().split(b'\0')
    except (FileNotFoundError, ProcessLookupError):
        return False
    args = [a.decode() for a in command if a]
    source = out/('source_original' if job['arm'] == 'partial_direct' else 'source')
    expected = [str(source/'script/run_followup_validation.py'), 'worker', '--out', str(out), '--job', job['id']]
    if not any(args[i:i+len(expected)] == expected for i in range(len(args))):
        raise ValueError('Worker identity changed; refusing to signal '+str(pid))
    children = []
    def descendants(parent):
        path = proc_root/str(parent)/'task'/str(parent)/'children'
        try:
            values = path.read_text().split()
        except (FileNotFoundError, ProcessLookupError):
            values = []
        if values:
            for value in values:
                child = int(value)
                children.append(child)
                descendants(child)
    descendants(pid)
    write(out/'workers'/job['id']/'resource_stop.json', dict(updated_at=now(),
        reason='Frozen worker hit CUDA OOM; stop before further failed questions',
        question_id=question_id, pid=pid, children=children, result_retained=True, automatic_retry=False))
    for target in [pid]+children:
        try:
            kill(target, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return True


def watch(out):
    with (out/'resource_guard.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        jobs = {j['id']:j for j in read(out/'jobs.json')}
        stopped = set()
        while True:
            states = read(out/'scheduler_state.json')
            active = {j:s for j,s in states.items() if s['state']=='running'}
            for jid, state in active.items():
                if (jid, state['pid']) in stopped:
                    continue
                job = jobs[jid]
                for uid in job['ids']:
                    path = out/'formal/arms'/job['arm']/'runs'/uid/'result.json'
                    if path.exists() and resource_error(read(path)):
                        if stop_worker(out, job, state, uid):
                            stopped.add((jid,state['pid']))
                        break
            status = read(out/'queue_status.json')
            write(out/'resource_guard_status.json', dict(updated_at=now(), pid=os.getpid(),
                state='watching', stopped_jobs=sorted(j for j,p in stopped), active_jobs=list(active)))
            if status['state'] in ('complete','failed') and not active:
                return
            time.sleep(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    watch(parser.parse_args().out.resolve())
