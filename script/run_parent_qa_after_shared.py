"""Finish unspent original-protocol QA after compact C/D releases its model."""
import argparse
import fcntl
import importlib
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

try:
    from .run_compact_resident import drain
except ImportError:
    from run_compact_resident import drain


def next_range(out,io):
    ids=io.read(out/'cohort.json')
    ready=lambda u:all((out/'final_inputs'/a/f'{u}.json').exists() for a in io.ARMS)
    pending=lambda u:any(not (out/'runs'/a/u/'result.json').exists() for a in io.ARMS)
    for start,u in enumerate(ids):
        if not ready(u) or not pending(u):continue
        end=start+1
        while end<min(start+40,len(ids)) and ready(ids[end]):end+=1
        return start,end
    return None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['controller','worker'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--record',type=Path,required=True)
    args=parser.parse_args();out=args.out.resolve();record=args.record.resolve()
    sys.path.insert(0,str(out/'control'))
    protocol=importlib.import_module('run_controlled_memory_experiment');io=protocol.io
    try:
        protocol.verify(out)
        request=io.read(record/'request.json')
        for name,expected in io.read(record/'hashes.json').items():
            if io.sha(record/name)!=expected:raise ValueError('Parent continuation artifact changed')
        if args.mode=='worker':
            adapter=SimpleNamespace(io=io,prepare_ready=lambda out:None,report=io.report,
                next_range=lambda out:next_range(out,io),worker=protocol.qa_worker)
            return drain(out,adapter,protocol)
        while not all(io.read(Path(p)/'report.json')['complete'] for p in request['dependencies']):
            io.write(record/'status.json',dict(state='waiting_for_compact_and_D_to_finish',
                controller_pid=os.getpid(),updated_at=io.now()))
            time.sleep(10)
        with (out/'controller.lock').open('a') as controller:
            fcntl.flock(controller,fcntl.LOCK_EX|fcntl.LOCK_NB)
            while True:
                row=protocol.inventory()['3']
                if row['uuid']!=request['gpu_uuid']:raise ValueError('Physical GPU identity changed')
                if row['free_mib']>=request['minimum_free_mib']:break
                io.write(record/'status.json',dict(state='waiting_for_initial_gpu_memory',gpu=row,updated_at=io.now()))
                time.sleep(10)
            with (Path('/tmp')/f'videoagent-compact-shared-{os.getuid()}-{row["uuid"]}.lock').open('a') as lease:
                while True:
                    try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:time.sleep(5)
                env=dict(os.environ,CUDA_VISIBLE_DEVICES=row['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                    OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
                command=[sys.executable,'-u',str(Path(__file__).resolve()),'worker','--out',str(out),'--record',str(record)]
                with (record/'worker.log').open('a') as log:
                    proc=subprocess.Popen(command,cwd=out,env=env,stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
                while proc.poll() is None:
                    io.report(out)
                    state=dict(state='running',controller_pid=os.getpid(),worker_pid=proc.pid,
                        phase='resident_qa_continuation',updated_at=io.now())
                    io.write(record/'status.json',state);io.write(out/'queue_status.json',state)
                    time.sleep(5)
                summary=io.report(out)
                state=dict(state='complete' if proc.returncode==0 and summary['complete'] else 'failed',
                    returncode=proc.returncode,updated_at=io.now())
                io.write(record/'status.json',state);io.write(out/'queue_status.json',state)
                return proc.returncode or (0 if summary['complete'] else 2)
    except Exception as exc:
        import traceback
        io.write(record/f'{args.mode}_failure.json',dict(type=type(exc).__name__,message=str(exc),
            traceback=traceback.format_exc(),updated_at=io.now()))
        raise


if __name__=='__main__':raise SystemExit(main())
