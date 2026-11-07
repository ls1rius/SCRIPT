"""Freeze J now; after its demo format gate and queued I, evaluate all I tasks."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
import controlled_memory_io as io


def verify(root):
    for name,sha in io.read(root/'frozen_hashes.json').items():
        if io.sha(root/name)!=sha:raise ValueError('Frozen queue artifact changed: '+str(root/name))


def prepare(folder,predecessor,parent,demo,out,gpu_index='3'):
    if folder.exists() or out.exists():raise ValueError('Use fresh J queue and output directories')
    verify(predecessor);verify(demo)
    if Path(io.read(predecessor/'plan.json')['output_I'])!=parent:
        raise ValueError('J parent must match the queued I output')
    if io.read(demo/'experiment.json').get('arm')!='J_grounded_reader':raise ValueError('J demo prerequisite required')
    control=folder/'control';control.mkdir(parents=True)
    for name in ['controlled_memory_io.py','grounded_reader.py','demo_evidence_revision.py','qa_evidence_view.py',
                 'g_rl.py']:
        shutil.copy2(demo/'control'/name,control/name)
    shutil.copy2(Path(__file__),control/Path(__file__).name)
    # Reader method modules remain sealed to the validated demo; only the
    # execution entry and GPU waiter use the updated scheduling implementation.
    for name in ['run_grounded_reader_experiment.py','gpu_queue.py']:
        shutil.copy2(Path(__file__).parent/name,control/name)
    io.write(folder/'plan.json',dict(created_at=io.now(),predecessor_I_queue=str(predecessor),parent_I=str(parent),
        demo=str(demo),output_J=str(out),gpu_index=str(gpu_index),planned=io.read(predecessor/'plan.json')['planned'],
        predecessor_manifest_sha256=io.sha(predecessor/'frozen_hashes.json'),demo_manifest_sha256=io.sha(demo/'frozen_hashes.json'),
        version=io.read(demo/'experiment.json')['version'],gate='Synthetic positive/negative source probe passes; demo complete, zero format errors, two reader calls per case; accuracy is never a gate.',
        failures='Stop on dependency or child failure; no retries or parent changes.'))
    io.write(folder/'frozen_hashes.json',{str(p.relative_to(folder)):io.sha(p) for p in folder.rglob('*') if p.is_file()})
    io.write(folder/'queue_status.json',dict(state='queued',phase='waiting_for_J_demo_and_I',updated_at=io.now()))


def completed(root):
    state=io.read(root/'queue_status.json')
    if state['state']=='failed' or (root/'failures/queue.json').exists():raise RuntimeError('Dependency failed: '+str(root))
    return state['state']=='complete'


def validate_sealed_gate(folder, plan):
    gate=io.read(folder/'prerequisite_gate.json')
    if (gate.get('version')!=plan['version'] or gate.get('passed') is not True
            or gate.get('synthetic_functional_probe_passed') is not True
            or gate.get('benchmark_labels_used') is not False
            or gate.get('completed_cases')!=7 or gate.get('format_errors')!=0
            or gate.get('reader_calls_per_case')!=[2]*7 or gate.get('accuracy_used') is not False):
        raise ValueError('Sealed J prerequisite gate failed; accuracy is not used')


def run(folder):
    verify(folder);plan=io.read(folder/'plan.json')
    predecessor,parent,out=(Path(plan[k]) for k in ['predecessor_I_queue','parent_I','output_J'])
    demo=Path(plan['demo']) if plan.get('demo') else None
    dependencies=[(predecessor,'predecessor_manifest_sha256')]
    if demo is not None:dependencies.append((demo,'demo_manifest_sha256'))
    for root,key in dependencies:
        if io.sha(root/'frozen_hashes.json')!=plan[key]:raise ValueError('Dependency manifest changed')

    def status(state,phase,**extra):
        io.write(folder/'queue_status.json',dict(state=state,phase=phase,controller_pid=os.getpid(),updated_at=io.now(),**extra))

    def child(phase,command):
        launch=folder/'launches'/f'{phase}.json'
        if launch.exists():raise ValueError('Stage already attempted: '+phase)
        (folder/'logs').mkdir(exist_ok=True)
        with (folder/'logs'/f'{phase}.log').open('a') as log:
            proc=subprocess.Popen(command,cwd=folder,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
        io.write(launch,dict(pid=proc.pid,command=command,started_at=io.now()))
        while proc.poll() is None:
            status('running',phase,child_pid=proc.pid);time.sleep(10)
        if proc.returncode:raise RuntimeError(f'{phase} failed with exit code {proc.returncode}')

    with (folder/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            if demo is None:
                validate_sealed_gate(folder,plan)
                io.write(folder/'functional_gate.json',dict(passed=True,sealed_prerequisite='prerequisite_gate.json',accuracy_used=False,updated_at=io.now()))
            else:
                while not completed(demo):
                    status('waiting_for_demo','J_demo',dependency=str(demo));time.sleep(30)
                result=io.read(demo/'report.json')
                gate_path=demo/'validation/functional.json'
                if (not gate_path.exists() or not io.read(gate_path)['passed'] or result['completed']!=7
                        or result['format_errors'] or any(c['J'].get('reader_calls')!=2 for c in result['cases'])):
                    raise ValueError('J demo functional gate failed; accuracy is not used')
                io.write(folder/'functional_gate.json',dict(passed=True,demo=str(demo),accuracy_used=False,updated_at=io.now()))
            while not completed(predecessor):
                status('waiting_for_I','I',dependency=str(predecessor));time.sleep(30)
            if not completed(parent):raise ValueError('Queued I did not produce complete parent results')
            entry=folder/'control/run_grounded_reader_experiment.py'
            child('J_prepare',[sys.executable,'-u',str(entry),'prepare','--parent',str(parent),'--out',str(out),'--gpu-index',plan['gpu_index']])
            child('J_queue',[sys.executable,'-u',str(out/'control/run_grounded_reader_experiment.py'),'queue','--out',str(out)])
            if not completed(out):raise RuntimeError('J cohort did not complete')
            status('complete','complete',output_J=str(out));return 0
        except Exception as exc:
            status('failed','stopped',error=str(exc));raise


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=['prepare','run'])
    parser.add_argument('--queue-root',type=Path,required=True)
    for name in ['predecessor','parent','demo','out']:parser.add_argument('--'+name,type=Path)
    parser.add_argument('--gpu-index',default='3');args=parser.parse_args();folder=args.queue_root.resolve()
    if args.mode=='prepare':
        if any(getattr(args,n) is None for n in ['predecessor','parent','demo','out']):parser.error('prepare needs predecessor, parent, demo and out')
        prepare(folder,args.predecessor.resolve(),args.parent.resolve(),args.demo.resolve(),args.out.resolve(),args.gpu_index);return 0
    return run(folder)


if __name__=='__main__':raise SystemExit(main())
