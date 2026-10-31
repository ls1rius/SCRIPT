"""Move future controlled-study stages to another GPU; drain in-flight work intact."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ARMS=('A_original','B_lossless_layout','C_semantic')


def read(p):return json.loads(Path(p).read_text())


def live(pid,start_ticks=None):
    try:
        stat=Path('/proc',str(pid),'stat').read_text().rsplit(') ',1)[1].split()
        return stat[0]!='Z' and (start_ticks is None or stat[19]==str(start_ticks))
    except OSError:return False


def next_stage(out,drain=None):
    ids=read(out/'cohort.json')
    for start in range(0,len(ids),20):
        end=min(start+20,len(ids));block=ids[start:end]
        if all((out/'runs'/a/u/'result.json').exists() for a in ARMS for u in block):continue
        if drain and live(drain['pid'],drain['start_ticks']) and start<drain['end'] and end>drain['start']:continue
        missing=[u for u in block if not (out/'final_inputs/C_semantic'/f'{u}.json').exists()]
        if missing:
            partial=[u for u in missing if (out/'materialized'/u).exists()]
            if partial:raise ValueError('Partially spent extraction preserved; explicit recovery required: '+','.join(partial))
            return 'normalize',start,end
        return 'qa',start,end
    return None


def load_io(out):
    spec=importlib.util.spec_from_file_location('controlled_frozen_io',out/'control/controlled_memory_io.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def inventory():
    raw=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.free','--format=csv,noheader,nounits'],text=True)
    return {parts[0]:dict(uuid=parts[1],free_mib=int(parts[2]))
            for line in raw.splitlines() if (parts:=[p.strip() for p in line.split(',')])}


def adopt(out,migration,io):
    state=read(out/'queue_status.json');pid=state['controller_pid']
    args=Path('/proc',str(pid),'cmdline').read_bytes().split(b'\0')
    expected=str(out/'control/run_controlled_memory_experiment.py').encode()
    if expected not in args or b'queue' not in args or Path('/proc',str(pid)).stat().st_uid!=os.getuid():
        raise ValueError('Original supervisor identity does not match this experiment')
    stopped=False;terminated=False
    try:
        os.kill(pid,signal.SIGSTOP);stopped=True
        children=Path('/proc',str(pid),'task',str(pid),'children').read_text().split()
        active=[]
        for child in children:
            if not live(int(child)):continue
            cmd=Path('/proc',child,'cmdline').read_bytes().split(b'\0')
            if expected not in cmd:raise ValueError('Unexpected supervisor child')
            tokens=[v.decode() for v in cmd if v]
            if '--out' not in tokens or tokens[tokens.index('--out')+1]!=str(out):raise ValueError('Worker belongs to another experiment')
            mode='normalize' if 'normalize' in tokens else 'qa' if 'qa' in tokens else None
            if mode is None:raise ValueError('Unexpected worker mode')
            stat=Path('/proc',child,'stat').read_text().rsplit(') ',1)[1].split()
            active.append(dict(pid=int(child),start_ticks=stat[19],mode=mode,
                start=int(tokens[tokens.index('--start')+1]),end=int(tokens[tokens.index('--end')+1]),
                phase=tokens[tokens.index('--phase')+1],gpu_index=state.get('gpu_index')))
        if len(active)>1:raise ValueError('More than one in-flight stage; refusing ambiguous adoption')
        for name in ('queue_status.json','progress.json','controller.json','launch.json','report.json'):
            if (out/name).exists():shutil.copy2(out/name,migration/('prior_'+name))
        drain=active[0] if active else None
        io.write(migration/'adoption.json',dict(previous_controller_pid=pid,draining_worker=drain,
            inference_source_unchanged=True,reason='User assigned the new A/B/C experiment to GPU 0.',updated_at=io.now()))
        # Only replace the CPU supervisor. Its in-flight worker/model are left running.
        os.kill(pid,signal.SIGTERM);os.kill(pid,signal.SIGCONT);terminated=True;stopped=False
        for _ in range(30):
            if not live(pid):break
            time.sleep(.1)
        if live(pid):raise ValueError('Original supervisor did not exit')
        return drain
    finally:
        if stopped and not terminated and live(pid):os.kill(pid,signal.SIGCONT)


def queue(out,migration,gpu):
    io=load_io(out)
    for name,h in read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=h:raise ValueError('Frozen input or inference source changed: '+name)
    if not read(out/'validation/baseline_reproduction.json')['passed']:
        raise ValueError('Original-protocol baseline gate has not passed')
    with (migration/'controller.lock').open('a') as local_lock:
        fcntl.flock(local_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stable=0
        while True:
            row=inventory().get(gpu);stable=stable+1 if row and row['free_mib']>=43008 else 0
            io.write(migration/'status.json',dict(state='waiting_for_target_gpu',gpu_index=gpu,
                stable_observations=stable,updated_at=io.now()))
            if stable>=3:
                lease=(Path('/tmp')/f'videoagent-finalqa-{os.getuid()}-{row["uuid"]}.lock').open('a')
                try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:lease.close()
                else:break
            time.sleep(10)
        try:
            drain=adopt(out,migration,io)
            with (out/'controller.lock').open('a') as global_lock:
                fcntl.flock(global_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                io.write(out/'controller.json',dict(pid=os.getpid(),started_at=io.now(),migration=str(migration),
                    command=[sys.executable,'-u',str(Path(__file__).resolve()),'queue','--out',str(out),'--gpu',gpu]))
                io.write(out/'launch.json',dict(gpu_index=gpu,gpu_uuid=row['uuid'],started_at=io.now(),
                    controller_pid=os.getpid(),draining_worker=drain,migration=str(migration)))
                env=dict(os.environ,CUDA_VISIBLE_DEVICES=row['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',HF_HUB_OFFLINE='1',
                    TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
                while True:
                    stage=next_stage(out,drain)
                    if stage is None:
                        if drain and live(drain['pid'],drain['start_ticks']):
                            io.write(out/'queue_status.json',dict(state='waiting_for_inflight_stage',gpu_index=gpu,
                                controller_pid=os.getpid(),draining_worker=drain,updated_at=io.now()))
                            time.sleep(10);continue
                        result=io.report(out)
                        io.write(out/'queue_status.json',dict(state='complete' if result['complete'] else 'failed',
                            controller_pid=os.getpid(),gpu_index=gpu,updated_at=io.now()))
                        return 0 if result['complete'] else 2
                    mode,start,end=stage;phase=f'gpu{gpu}_{mode}_{start}'
                    command=[sys.executable,'-u',str(out/'control/run_controlled_memory_experiment.py'),mode,
                        '--out',str(out),'--start',str(start),'--end',str(end),'--phase',phase]
                    with (out/'logs'/f'{phase}.log').open('a') as log:
                        proc=subprocess.Popen(command,cwd=out,env=env,stdin=subprocess.DEVNULL,stdout=log,
                            stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
                    io.write(out/'workers'/f'{phase}.json',dict(pid=proc.pid,command=command,started_at=io.now(),gpu_index=gpu))
                    while proc.poll() is None:
                        try:io.report(out)
                        except json.JSONDecodeError:pass
                        io.write(out/'queue_status.json',dict(state='running',phase=phase,worker_pid=proc.pid,
                            controller_pid=os.getpid(),gpu_index=gpu,
                            draining_worker=drain if drain and live(drain['pid'],drain['start_ticks']) else None,
                            updated_at=io.now()))
                        time.sleep(10)
                    io.report(out)
                    if proc.returncode:
                        io.write(out/'queue_status.json',dict(state='failed',phase=phase,returncode=proc.returncode,
                            gpu_index=gpu,controller_pid=os.getpid(),updated_at=io.now()))
                        return proc.returncode
        finally:lease.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['start','queue'])
    p.add_argument('--out',type=Path,required=True);p.add_argument('--gpu',default='0')
    args=p.parse_args();out=args.out.resolve();io=load_io(out)
    migration=out/'control/migration_gpu0_r1'
    if args.mode=='start':
        migration.mkdir(exist_ok=False)
        script=migration/Path(__file__).name;shutil.copy2(__file__,script)
        io.write(migration/'request.json',dict(created_at=io.now(),gpu_index=args.gpu,
            policy='Drain existing stage without interruption; process disjoint pending blocks on target GPU; retain all results/costs.',
            frozen_manifest_sha256=io.sha(out/'frozen_hashes.json')))
        command=[sys.executable,'-u',str(script),'queue','--out',str(out),'--gpu',args.gpu]
        with (migration/'controller.log').open('a') as log:
            proc=subprocess.Popen(command,cwd=out,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        io.write(migration/'controller.json',dict(pid=proc.pid,command=command,started_at=io.now()))
        print(json.dumps(dict(migration_controller_pid=proc.pid,target_gpu=args.gpu)),flush=True);return 0
    try:return queue(out,migration,args.gpu)
    except Exception as exc:
        import traceback
        io.write(migration/'failure.json',dict(type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc(),updated_at=io.now()))
        raise


if __name__=='__main__':raise SystemExit(main())
