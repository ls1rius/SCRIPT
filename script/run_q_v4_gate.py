"""Single frozen synthetic pilot; failures retained, no automatic retry."""
import os,sys,time,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script.scale_live_parallel_pool import read,write,now,verify_manifest
from script.run_evidence_planner_experiment import verify_model
from script_core.budget import ModelWorker
from script.gpu_queue import acquire
from script.q_v4_validation import validate_live

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);args=parser.parse_args();out=args.out.resolve()
    verify_manifest(out);verify_model(out)
    if (out/'status.json').exists():raise ValueError('Pilot already attempted')
    cfg=read(out/'qa_config.json');policy=read(out/'experiment.json');backend=ModelWorker(cfg)
    def waiting(d):write(out/'status.json',dict(stage='waiting',details=d,at=now()))
    with acquire(policy['resource_policy'],waiting) as (gpu,_):
        os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid'];os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
        try:
            write(out/'status.json',dict(stage='loading',pid=os.getpid(),gpu=gpu['index'],at=now()))
            started=time.monotonic();details=backend.call('prepare_vlm',{},600)
            write(out/'setup.json',dict(seconds=time.monotonic()-started,details=details,gpu=gpu))
            write(out/'status.json',dict(stage='validation',pid=os.getpid(),gpu=gpu['index'],at=now()))
            validate_live(backend,cfg,out/'validation')
            write(out/'status.json',dict(stage='complete',pid=os.getpid(),gpu=gpu['index'],at=now()))
        except BaseException as exc:
            write(out/'status.json',dict(stage='failed',error=str(exc),at=now()));raise
        finally:backend.close(graceful=True)
