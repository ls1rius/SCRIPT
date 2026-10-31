"""Use one resident QA model for the compact A/B/C and state-segment D runs."""
from pathlib import Path
import sys
_PROJECT_ROOT = next(p for p in Path(__file__).resolve().parents if (p/'script_core').is_dir())
sys.path[:0] = [str(_PROJECT_ROOT), str(_PROJECT_ROOT/'script')]

import argparse
from contextlib import ExitStack
import fcntl
import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

from run_compact_resident import drain_many


def tasks(compact,segments):
    sys.path.insert(0,str(compact/'control'))
    cr=importlib.import_module('run_compact_evidence_experiment')
    spec=importlib.util.spec_from_file_location('state_segment_experiment',segments/'control/run_state_segment_experiment.py')
    dr=importlib.util.module_from_spec(spec);spec.loader.exec_module(dr)
    return [(compact,cr),(segments,dr)]


def controller(compact,segments,record):
    work=tasks(compact,segments);io=work[0][1].io
    for name,expected in io.read(record/'hashes.json').items():
        if io.sha(record/name)!=expected:
            raise ValueError('Shared QA controller artifact changed: '+name)
    for out,run in work:
        run.verify(out)
    request=io.read(record/'request.json')
    with ExitStack() as stack:
        for out,_ in work:
            handle=stack.enter_context((out/'controller.lock').open('a'))
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            io.write(out/'controller.json',dict(pid=os.getpid(),control=str(record),started_at=io.now()))
        while True:
            row=work[0][1].inventory()['3']
            if row['uuid']!=request['gpu_uuid']:
                raise ValueError('Physical GPU identity changed')
            for out,run in work:
                run.prepare_ready(out);run.report(out)
                io.write(out/'queue_status.json',dict(state='waiting_for_initial_gpu_memory',
                    controller_pid=os.getpid(),model_resident=False,gpu_index='3',gpu=row,
                    minimum_free_mib=request['minimum_free_mib'],updated_at=io.now()))
            if row['free_mib']>=request['minimum_free_mib']:
                break
            time.sleep(10)
        lease=stack.enter_context((Path('/tmp')/f'videoagent-compact-shared-{os.getuid()}-{row["uuid"]}.lock').open('a'))
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        env=dict(os.environ,CUDA_VISIBLE_DEVICES=row['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',
            HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
            OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
        command=[sys.executable,'-u',str(Path(__file__).resolve()),'worker',
            '--compact',str(compact),'--segments',str(segments),'--record',str(record)]
        with (record/'worker.log').open('a') as log:
            proc=subprocess.Popen(command,cwd=compact,env=env,stdin=subprocess.DEVNULL,
                stdout=log,stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
        launch=dict(controller_pid=os.getpid(),worker_pid=proc.pid,command=command,
            gpu_index='3',gpu_uuid=row['uuid'],started_at=io.now(),model_lifecycle='resident_across_C_and_D')
        io.write(record/'launch.json',launch)
        while proc.poll() is None:
            for out,run in work:
                run.report(out)
            io.write(record/'status.json',dict(launch,state='running',updated_at=io.now()))
            time.sleep(5)
        complete=all(run.report(out)['complete'] for out,run in work)
        state='complete' if not proc.returncode and complete else 'failed'
        io.write(record/'status.json',dict(launch,state=state,returncode=proc.returncode,updated_at=io.now()))
        if state=='failed':
            for out,run in work:
                if not run.report(out)['complete']:
                    io.write(out/'queue_status.json',dict(launch,state=state,returncode=proc.returncode,updated_at=io.now()))
        return proc.returncode or (0 if complete else 2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['controller','worker'])
    parser.add_argument('--compact',type=Path,required=True)
    parser.add_argument('--segments',type=Path,required=True)
    parser.add_argument('--record',type=Path,required=True)
    args=parser.parse_args()
    compact,segments,record=args.compact.resolve(),args.segments.resolve(),args.record.resolve()
    try:
        if args.mode=='controller':
            return controller(compact,segments,record)
        work=tasks(compact,segments)
        protocol=importlib.import_module('compact_protocol')
        for out,run in work:
            run.verify(out)
        return drain_many(work,protocol)
    except Exception as exc:
        import traceback
        sys.path.insert(0,str(compact/'control'))
        import controlled_memory_io as io
        io.write(record/f'{args.mode}_failure.json',dict(type=type(exc).__name__,
            message=str(exc),traceback=traceback.format_exc(),updated_at=io.now()))
        raise


if __name__=='__main__':
    raise SystemExit(main())
