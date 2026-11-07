"""Adopt resident Q6 workers while retiring selected actor GPUs at a batch boundary.

No model runtime, optimizer, worker code or in-flight call is changed. The controller
follows process identity and durable worker state instead of Popen ownership.
"""
import argparse
import fcntl
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'script').exists():
    import json
    sys.path.insert(0,json.loads(Path(__file__).with_name('config.json').read_text())['runtime'])
else:sys.path.insert(0,str(ROOT))
from script.scale_live_parallel_pool import read,write,now,identity


def alive(row):
    try:
        tail=Path('/proc',str(row['pid']),'stat').read_text().rsplit(')',1)[1].split()
        return tail[0]!='Z' and identity(row['pid'])==row['identity']
    except (OSError,ValueError):return False


def watch(control):
    cfg=read(control/'config.json');out=Path(cfg['out'])
    sys.path.insert(0,cfg['runtime'])
    from script.train_q6_rl import evaluation_report
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        write(control/'launch.json',dict(pid=os.getpid(),identity=identity(os.getpid()),at=now()))
        while True:
            cfg=read(control/'config.json');rows=cfg['active_workers'];states={}
            failure=read(out/'failure.json') if (out/'failure.json').exists() else None
            for row in rows:
                status=read(out/'workers'/str(row['id'])/'status.json')
                states[str(row['id'])]=status
                if not alive(row) and status['stage']!='complete' and not failure:
                    failure=dict(worker_id=row['id'],message='Adopted Q6 worker exited before completion',at=now())
                    write(out/'failure.json',failure)
            trained=(out/'training/result.json').exists()
            evaluation=evaluation_report(out) if trained else None
            live=any(alive(r) for r in rows)
            complete=bool(evaluation and evaluation['complete'] and not live and not failure)
            if not live and not complete and not failure:
                failure=dict(message='Adopted Q6 queue exited before completion',at=now());write(out/'failure.json',failure)
            stage='failed' if failure else 'complete' if complete else 'validation300' if trained else 'parallel_RL'
            state='failed' if failure else 'complete' if complete else 'running'
            write(out/'queue_status.json',dict(state=state,stage=stage,controller_pid=os.getpid(),workers=states,
                allowed_gpu_indices=cfg['allowed_gpu_indices'],user_reserved_gpu_indices=cfg['reserved_gpu_indices'],
                maximum_model_workers=len(rows),primary_arm='Qplus',resource_controller=str(control),
                retired_workers=cfg['retired_workers'],updated_at=now()))
            write(out/'report.json',dict(stage=stage,complete=complete,primary_arm='Qplus',epochs=6,
                training_progress=read(out/'training/status.json') if (out/'training/status.json').exists() else None,
                training=read(out/'training/result.json') if trained else None,evaluation=evaluation,failure=failure,
                user_reserved_gpu_indices=cfg['reserved_gpu_indices'],updated_at=now()))
            if not live:return
            time.sleep(5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--control',type=Path,required=True)
    args=parser.parse_args();watch(args.control.resolve())
