"""Persistent four-GPU EgoSchema evaluation with streaming shared asset preparation.

Runs the existing, validated experiment execute() unchanged. Each arm owns one
physical GPU. The fourth arm consumes only the third arm's evidence bundle.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import shutil
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.run_temporal_entity import ARMS, execute, read, report, sha, write
from script_core.config import digest, load_config
from script_core.temporal_entity import SETTINGS

TZ = ZoneInfo('Asia/Singapore')


def now():
    return datetime.now(TZ).isoformat()


def cohort_ids(annotations, seed=0):
    """Reconstruct the checked-in original100 + shuffled200 sampling rule.

    The old cohort.json is unavailable here, so provenance explicitly records
    reconstruction rather than claiming equality with that missing artifact.
    """
    excluded = {'0074f737-11cb-497d-8d07-77c3a8127391',
                '0a01d7d0-11d6-4af6-abd9-2025656d3c63'}
    original = random.Random(seed).sample(sorted(set(annotations)-excluded), 100)
    remaining = sorted(set(annotations)-set(original))
    random.Random(seed).shuffle(remaining)
    return original + remaining[:200]


def gpu_inventory():
    value = subprocess.check_output(['nvidia-smi',
        '--query-gpu=index,uuid,memory.used,utilization.gpu',
        '--format=csv,noheader,nounits'], text=True)
    return {p[0].strip(): dict(uuid=p[1].strip(), used_mib=int(p[2]), utilization=int(p[3]))
            for line in value.splitlines() if len(p := line.split(',')) == 4}


def require_idle(indices):
    if len(indices) != len(set(indices)):
        raise ValueError('Assign distinct physical GPUs')
    available = gpu_inventory()
    for index in indices:
        item = available.get(index)
        if not item or item['used_mib'] > 500 or item['utilization'] > 5:
            raise ValueError(f'GPU {index} is occupied; no other process will be interrupted')
    return available


def worker_env(uuid):
    return dict(os.environ, CUDA_VISIBLE_DEVICES=uuid, CUDA_DEVICE_ORDER='PCI_BUS_ID',
        HF_HOME=str(ROOT/'checkpoints/.cache/huggingface'),
        HF_MODULES_CACHE=str(ROOT/'checkpoints/.cache/modules'),
        HF_XET_CACHE=str(ROOT/'checkpoints/.cache/xet'), HF_HUB_OFFLINE='1',
        TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='4', MKL_NUM_THREADS='4',
        PYTHONUNBUFFERED='1')


def verify_snapshot(out):
    for name, expected in read(out/'source_hashes.json').items():
        if sha(ROOT/name) != expected:
            raise ValueError('Frozen source changed: '+name)
    for name, expected in read(out/'control_hashes.json').items():
        if sha(out/name) != expected:
            raise ValueError('Frozen control changed: '+name)


def prepare(out, gpus, asset_gpu, batch_size, frame_workers, verification=None):
    if out.exists():
        raise ValueError('Use a new experiment directory; prior runs retain their spent budgets')
    if len(gpus) != 4:
        raise ValueError('Exactly four inference GPUs are required')
    inventory = require_idle(gpus+[asset_gpu])
    smoke = Path(verification) if verification else ROOT/'workdirs/dev-four-arm-smoke-r5'
    checked = read(smoke/'verification/final_report.json')
    if not checked['passed'] or checked['unit_tests']['passed'] < 423:
        raise ValueError('Require the successful four-arm functional verification')
    for name, expected in read(smoke/'source_hashes.json').items():
        if sha(ROOT/name) != expected:
            raise ValueError('Source differs from verified smoke: '+name)
    data = ROOT/'data/EgoSchema/egoschema'
    annotations, captions = read(data/'subset_anno.json'), read(data/'lavila_subset.json')
    ids = cohort_ids(annotations)
    if len(annotations) != 500 or len(ids) != 300 or len(set(ids)) != 300:
        raise ValueError('Require 300 distinct examples from the public 500')
    for uid in ids:
        if (len(captions[uid]) != 180 or not (data/'videos'/f'{uid}.mp4').is_file()
                or annotations[uid]['truth'] not in range(5)):
            raise ValueError('Missing source or invalid one-second timeline: '+uid)
    out.mkdir(parents=True)
    cfg = load_config(ROOT/'configs/script.yaml')
    write(out/'config.json', cfg)
    write(out/'settings.json', SETTINGS)
    write(out/'cohort.json', ids)
    write(out/'input_hashes.json', {})
    write(out/'evaluation_only.json', {u:dict(truth=annotations[u]['truth']) for u in ids})
    questions = [dict(question_id=u,video_id=u,question=annotations[u]['question'],
                     options=[annotations[u][f'option {i}'] for i in range(5)]) for u in ids]
    (out/'questions.jsonl').write_text(''.join(json.dumps(q,ensure_ascii=False)+'\n' for q in questions))
    allocation = {arm:dict(index=gpu,uuid=inventory[gpu]['uuid']) for arm,gpu in zip(ARMS,gpus)}
    allocation['assets'] = dict(index=asset_gpu,uuid=inventory[asset_gpu]['uuid'])
    plan = dict(created_at=now(),questions_per_arm=300,arms=ARMS,gpus=allocation,
        feature_batch_size=batch_size,frame_workers=frame_workers,
        cohort_provenance=dict(seed=0,rule='original100 sampled excluding two debug IDs, then 200 from seeded shuffle of remaining400',
            historical_cohort_artifact_available=False,historical_identity_verified=False,
            annotation_sha256=sha(data/'subset_anno.json'),caption_sha256=sha(data/'lavila_subset.json')),
        verified_smoke=str(smoke),budget=cfg['budget'],
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_has_local_fixes=True,paired_evidence=dict(source='summary_deep',target='temporal_cross',
            same_summaries=True,same_visual_observations=True,source_answer_visible=False,
            original_shared_preparation_cost_charged=True),
        assets_are_shared=True,model_weights_persist_between_questions=True,
        errors_are_retained_and_count_wrong=True)
    write(out/'experiment.json',plan)
    source = out/'source'
    names = subprocess.check_output(['git','ls-files','*.py','configs/*.yaml'],cwd=ROOT,text=True).splitlines()
    names += [str(Path(__file__).relative_to(ROOT)),'tests/test_four_arm_local.py',
              'requirements.txt','script/run_local_smoke.sh']
    names += [str(p.relative_to(ROOT)) for p in (ROOT/'tests/fixtures').glob('*.json')]
    for name in sorted(set(names)):
        dest=source/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,dest)
    for name in ('data','checkpoints'):
        (source/name).symlink_to((ROOT/name).resolve(),target_is_directory=True)
    write(out/'source_hashes.json',{n:sha(source/n) for n in sorted(set(names))})
    write(out/'control_hashes.json',{n:sha(out/n) for n in
        ('config.json','settings.json','cohort.json','experiment.json','questions.jsonl')})
    write(out/'verification/smoke_report.json',checked)
    shutil.copy2(ROOT/'README.md',source/'README.md')
    (out/'local_changes.patch').write_bytes(subprocess.check_output(['git','diff','--binary'],cwd=ROOT))
    for name in ('inputs','input_ready','workers','logs','arms'):
        (out/name).mkdir(exist_ok=True)
    return plan


def launch(out, gpus, asset_gpu, batch_size, frame_workers, verification=None):
    if batch_size < 1 or not 1 <= frame_workers <= 8:
        raise ValueError('Invalid preprocessing batch/worker count')
    plan = prepare(out,gpus,asset_gpu,batch_size,frame_workers,verification)
    session = 'codex-four-arm-300-'+datetime.now(TZ).strftime('%Y%m%d-%H%M%S')
    script = out/'source/script'/Path(__file__).name
    command = [sys.executable,'-u',str(script),'controller','--out',str(out)]
    with (out/'start.sh').open('w') as f:
        f.write('#!/usr/bin/env bash\nset -euo pipefail\n')
        f.write('cd '+shlex.quote(str(out/'source'))+'\n')
        f.write('exec '+shlex.join(command)+' >> '+shlex.quote(str(out/'logs/controller.log'))+' 2>&1\n')
    subprocess.run(['tmux','new-session','-d','-s',session,'bash '+shlex.quote(str(out/'start.sh'))],check=True)
    write(out/'launch.json',dict(session=session,launched_at=now(),python=sys.executable,command=command))
    print(json.dumps(dict(launched=True,session=session,out=str(out),allocation=plan['gpus']),indent=2),flush=True)


def progress(out, name, **fields):
    value = dict(worker=name,updated_at=now(),pid=os.getpid(),**fields)
    write(out/'workers'/f'{name}.json',value)


def make_packet(out, uid, question, captions, cfg):
    from PIL import Image
    feature=(ROOT/'data/EgoSchema/egoschema_features'/f'{uid}.pt').resolve()
    meta=read(feature.with_suffix('.json'))
    if (meta['fps'] != 1 or meta['shape'] != [len(captions),1280]
            or meta['frame_indices'] != list(range(len(captions)))
            or Path(meta['model']).resolve() != Path(cfg['models']['visual_encoder']).resolve()):
        raise ValueError('Feature provenance/grid mismatch: '+uid)
    frames=[]
    for i in range(len(captions)):
        path=(ROOT/'data/EgoSchema/egoschema_frames'/uid/f'{i}.jpg').resolve()
        with Image.open(path) as image:w,h=image.size
        frames.append(dict(frame_id=f'F{i}',pts=float(i),path=str(path),width=w,height=h,sha256=sha(path)))
    packet=dict(question=question,captions=captions,frames=frames,
        caption_records_hash=digest(captions),feature_uri=str(feature),feature_hash=sha(feature),feature_metadata=meta)
    write(out/'inputs'/f'{uid}.json',packet)
    hashes=read(out/'input_hashes.json');hashes[f'inputs/{uid}.json']=sha(out/'inputs'/f'{uid}.json')
    write(out/'input_hashes.json',hashes)
    # Publication is last, after both atomic packet and hash-index writes.
    write(out/'input_ready'/f'{uid}.json',dict(ready=True,packet_hash=hashes[f'inputs/{uid}.json'],published_at=now()))


def assets(out):
    import cv2
    import torch
    from PIL import Image
    from transformers import AutoModel, CLIPImageProcessor
    from data_extraction.extract_images import extract_video
    verify_snapshot(out)
    plan=read(out/'experiment.json');cfg=load_config(out/'config.json')
    ids=read(out/'cohort.json');questions={q['video_id']:q for q in
        map(json.loads,(out/'questions.jsonl').read_text().splitlines())}
    source=ROOT/'data/EgoSchema/egoschema';texts=read(source/'lavila_subset.json')
    cv2.setNumThreads(1)
    processor=CLIPImageProcessor(size={'shortest_edge':224},crop_size={'height':224,'width':224})
    model=None;batch_size=plan['feature_batch_size'];errors=0;began=time.monotonic()
    progress(out,'assets',state='preparing',completed=0,planned=len(ids),errors=0)
    with ThreadPoolExecutor(max_workers=plan['frame_workers']) as pool:
        futures={uid:pool.submit(extract_video,source/'videos'/f'{uid}.mp4',
            ROOT/'data/EgoSchema/egoschema_frames'/uid,len(texts[uid])) for uid in ids}
        for n,uid in enumerate(ids,1):
            progress(out,'assets',state='preparing',completed=n-1,planned=len(ids),video_id=uid,errors=errors)
            try:
                futures[uid].result()
                folder=ROOT/'data/EgoSchema/egoschema_frames'/uid
                target=ROOT/'data/EgoSchema/egoschema_features'/f'{uid}.pt'
                target.parent.mkdir(parents=True,exist_ok=True)
                valid=False
                if target.exists() and target.with_suffix('.json').exists():
                    meta=read(target.with_suffix('.json'))
                    features=torch.load(target,map_location='cpu',weights_only=True)
                    valid=(meta.get('model')==str(Path(cfg['models']['visual_encoder']).resolve())
                        and list(features.shape)==[len(texts[uid]),1280] and torch.isfinite(features).all().item()
                        and meta.get('frame_indices')==list(range(len(texts[uid]))))
                if not valid:
                    if model is None:
                        model=AutoModel.from_pretrained(cfg['models']['visual_encoder'],torch_dtype=torch.float16,
                            trust_remote_code=True,local_files_only=True,low_cpu_mem_usage=True).to('cuda').eval()
                    batches=[];start=0
                    while start<len(texts[uid]):
                        images=[]
                        for i in range(start,min(start+batch_size,len(texts[uid]))):
                            with Image.open(folder/f'{i}.jpg') as im:images.append(im.convert('RGB'))
                        pixels=processor(images=images,return_tensors='pt').pixel_values.pin_memory().to('cuda',dtype=torch.float16,non_blocking=True)
                        try:
                            with torch.inference_mode():encoded=model.encode_image(pixels)
                            batches.append(encoded.float().cpu());start+=len(images)
                        except torch.cuda.OutOfMemoryError:
                            if batch_size==1:raise
                            batch_size=max(1,batch_size//2);torch.cuda.empty_cache()
                            print('FEATURE_BATCH_REDUCED',batch_size,flush=True)
                    features=torch.cat(batches)
                    if not torch.isfinite(features).all():raise ValueError('Non-finite visual features')
                    temp=target.with_suffix('.pt.tmp');torch.save(features,temp);temp.replace(target)
                    write(target.with_suffix('.json'),dict(model=str(Path(cfg['models']['visual_encoder']).resolve()),
                        shape=list(features.shape),fps=1,dtype=str(features.dtype),
                        frame_indices=list(range(len(features))),extraction_batch_size=batch_size))
                records=[dict(id=f'C{i}',interval=[i,i+1],text=text) for i,text in enumerate(texts[uid])]
                make_packet(out,uid,questions[uid],records,cfg)
                print('INPUT_READY',n,uid,'elapsed',round(time.monotonic()-began,1),'batch',batch_size,flush=True)
            except Exception as exc:
                errors+=1
                write(out/'input_ready'/f'{uid}.json',dict(ready=False,error=dict(type=type(exc).__name__,message=str(exc))))
                print('ASSET_ERROR',uid,traceback.format_exc(),flush=True)
    progress(out,'assets',state='finished' if not errors else 'finished_with_errors',completed=len(ids),planned=len(ids),errors=errors)
    return int(bool(errors))


def wait_ready(out, uid, arm):
    marker=out/'input_ready'/f'{uid}.json'
    while not marker.exists():
        status=out/'workers/assets.json'
        if status.exists() and read(status).get('state')=='failed':
            raise RuntimeError('Asset preparation worker failed; inspect logs/assets.log')
        time.sleep(1)
    return read(marker)


def paired_ready(out, uid):
    base=out/'arms/summary_deep/runs'/uid
    while not (base/'evidence_bundle.json').exists():
        result=base/'result.json'
        if result.exists():return False
        status=out/'workers/summary_deep.json'
        if status.exists() and read(status).get('state')=='failed':
            raise RuntimeError('Summary-deep worker failed; inspect logs/summary_deep.log')
        time.sleep(1)
    return True


def error_result(out, arm, uid, error):
    value=dict(arm=arm,question_id=uid,answer=None,error=error)
    write(out/'arms'/arm/'runs'/uid/'result.json',value)
    return value


def run_arm(out, arm):
    from script_core.backend import preflight
    from script_core.budget import ModelWorker
    verify_snapshot(out)
    progress(out,arm,state='preflight',completed=0,planned=300,errors=0)
    cfg,artifacts=preflight(load_config(out/'config.json'))
    write(out/'verification'/f'{arm}-artifacts.json',artifacts)
    backend=ModelWorker(cfg);ids=read(out/'cohort.json');errors=0
    try:
        for n,uid in enumerate(ids,1):
            progress(out,arm,state='waiting_input',completed=n-1,planned=len(ids),video_id=uid,errors=errors)
            ready=wait_ready(out,uid,arm)
            if not ready['ready']:
                value=error_result(out,arm,uid,dict(code='shared_asset_failed',**ready['error']))
            else:
                if arm=='temporal_cross':
                    progress(out,arm,state='waiting_summary_evidence',completed=n-1,planned=len(ids),video_id=uid,errors=errors)
                    if not paired_ready(out,uid):
                        value=error_result(out,arm,uid,dict(code='paired_preparation_failed',message='Summary-deep failed before exporting source evidence'))
                    else:value=None
                else:value=None
                if value is None:
                    target=out/'arms'/arm/'runs'/uid
                    if not (target/'result.json').exists() and any(target.rglob('calls.jsonl')):
                        raise RuntimeError('Interrupted query retains spent budget; use a new output directory: '+str(target))
                    progress(out,arm,state='running',completed=n-1,planned=len(ids),video_id=uid,errors=errors)
                    value=execute(out,arm,uid,backend,cfg,smoke=False)
            errors+=bool(value.get('error'))
            progress(out,arm,state='running',completed=n,planned=len(ids),video_id=uid,errors=errors)
            print('RESULT',arm,n,'/',len(ids),uid,'answer',(value.get('answer')or{}).get('selected_option'),'error',value.get('error'),flush=True)
    finally:
        backend.close(graceful=True)
    progress(out,arm,state='finished' if not errors else 'finished_with_errors',completed=len(ids),planned=len(ids),errors=errors)
    return int(bool(errors))


def controller(out):
    verify_snapshot(out)
    plan=read(out/'experiment.json')
    require_idle([v['index'] for v in plan['gpus'].values()])
    jobs={};files={}
    for name in ['assets',*ARMS]:
        log=(out/'logs'/f'{name}.log').open('a');files[name]=log
        command=[sys.executable,'-u',str(Path(__file__).resolve()),'assets' if name=='assets' else 'run',
                 '--out',str(out)]
        if name!='assets':command+=['--arm',name]
        p=subprocess.Popen(command,cwd=ROOT,env=worker_env(plan['gpus'][name]['uuid']),stdout=log,stderr=subprocess.STDOUT)
        jobs[name]=p
        write(out/'processes.json',{n:dict(pid=v.pid,**plan['gpus'][n]) for n,v in jobs.items()})
        print('START',name,'GPU',plan['gpus'][name]['index'],'PID',p.pid,flush=True)
    statuses={};last_report=0.
    while len(statuses)<len(jobs):
        for name,p in jobs.items():
            if name not in statuses and p.poll() is not None:
                statuses[name]=dict(exit_code=p.returncode,finished_at=now())
                state_path=out/'workers'/f'{name}.json'
                current=read(state_path) if state_path.exists() else {}
                if current.get('state') not in ('finished','finished_with_errors'):
                    write(state_path,dict(current,state='failed',exit_code=p.returncode,updated_at=now()))
                print('EXIT',name,p.returncode,flush=True)
        if time.monotonic()-last_report>=10:
            workers={n:read(out/'workers'/f'{n}.json') for n in jobs if (out/'workers'/f'{n}.json').exists()}
            write(out/'progress.json',dict(updated_at=now(),planned_per_arm=300,workers=workers,exited=statuses))
            last_report=time.monotonic()
        time.sleep(1)
    for f in files.values():f.close()
    comparison=report(out)
    write(out/'finished.json',dict(finished_at=now(),workers=statuses,complete=comparison['complete']))
    return int(any(v['exit_code'] for v in statuses.values()))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['launch','controller','assets','run'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--gpus',default='2,3,4,5')
    parser.add_argument('--asset-gpu',default='0')
    parser.add_argument('--feature-batch-size',type=int,default=16)
    parser.add_argument('--frame-workers',type=int,default=4)
    parser.add_argument('--verification',type=Path,help='Passed verification root with matching source hashes')
    parser.add_argument('--arm',choices=list(ARMS))
    args=parser.parse_args();out=args.out.resolve()
    if args.command=='launch':
        launch(out,args.gpus.split(','),args.asset_gpu,args.feature_batch_size,args.frame_workers,args.verification)
        return 0
    if args.command=='controller':return controller(out)
    name='assets' if args.command=='assets' else args.arm
    if name is None:parser.error('--arm is required for run')
    try:return assets(out) if name=='assets' else run_arm(out,name)
    except BaseException as exc:
        progress(out,name,state='failed',error=dict(type=type(exc).__name__,message=str(exc)))
        raise


if __name__=='__main__':
    raise SystemExit(main())
