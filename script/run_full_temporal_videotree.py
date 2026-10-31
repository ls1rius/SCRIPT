"""Complete the public labeled 500, retaining 300 temporal results, versus VideoTree.

Frozen inputs and implementations; CPU frame preparation starts immediately.
GPU stages wait for memory and run sequentially under UUID-bound leases.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import csv
import fcntl
import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import statistics
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.run_adaptive_comparison import now, read, sha
from script.run_followup_validation import snapshot, resource_error
from script.run_original_videotree_local import aligned_cohorts, question
from script_core.config import digest, load_config
from script_core.state import write_json as write

STAGES = ('features', 'script', 'videotree')


def verify(out):
    for name, expected in read(out/'frozen_hashes.json').items():
        if sha(out/name) != expected:
            raise ValueError('Frozen source/input/result changed: '+name)


def stage_status(out, stage, **fields):
    write(out/'workers'/f'{stage}.json', dict(updated_at=now(), pid=os.getpid(), **fields))


def prediction(row, method):
    if method == 'videotree':
        return row.get('prediction')
    return (row.get('answer') or {}).get('selected_option')


def summary(ids, rows, truth, method):
    available = {u: rows[u] for u in ids if u in rows}
    correct = sum(not r.get('error') and str(prediction(r, method)) == str(truth[u])
                  for u, r in available.items())
    times = [r['inference_seconds'] if method == 'videotree' else r['total_measured_seconds']
             for r in available.values()]
    return dict(planned=len(ids), completed=len(available), pending=len(ids)-len(available),
        correct=correct, errors=sum(bool(r.get('error')) for r in available.values()),
        accuracy=correct/len(ids) if len(available)==len(ids) else None,
        accuracy_among_completed=correct/len(available) if available else None,
        mean_seconds=sum(times)/len(times) if times else None,
        median_seconds=statistics.median(times) if times else None,
        incomplete=sum(bool(r.get('incomplete') or r.get('source_incomplete')) for r in available.values()),
        parser_fallbacks=sum(bool(r.get('qa_parser_fallback')) for r in available.values()),
        complete=len(available)==len(ids), errors_count_wrong=True)


def report(out):
    cohorts, truth = read(out/'cohorts.json'), read(out/'evaluation_only.json')
    ours = {p.parent.name: read(p) for p in (out/'runs/script').glob('*/result.json')}
    baseline = {p.parent.name: read(p) for p in (out/'videotree/runs').glob('*/result.json')}
    reports = {}
    for key, ids in cohorts.items():
        paired = [u for u in ids if u in ours and u in baseline]
        def correct(rows, method, uid):
            return not rows[uid].get('error') and str(prediction(rows[uid],method))==str(truth[uid])
        fixed = [u for u in paired if correct(ours,'script',u) and not correct(baseline,'videotree',u)]
        regressed = [u for u in paired if correct(baseline,'videotree',u) and not correct(ours,'script',u)]
        paired_ours = sum(correct(ours,'script',u) for u in paired)
        paired_vt = sum(correct(baseline,'videotree',u) for u in paired)
        reports[key] = dict(script=summary(ids,ours,truth,'script'),
            videotree=summary(ids,baseline,truth,'videotree'),
            paired=dict(completed=len(paired), planned=len(ids), script_correct=paired_ours,
                videotree_correct=paired_vt, script_only_correct=fixed, videotree_only_correct=regressed,
                accuracy_delta_percentage_points=(paired_ours-paired_vt)/len(ids)*100
                    if len(paired)==len(ids) else None))
        write(out/'reports'/f'comparison_{key}.json',dict(updated_at=now(),**reports[key]))
    workers = {p.stem: read(p) for p in (out/'workers').glob('*.json')}
    result = dict(updated_at=now(),reports=reports,workers=workers,
        scope='All 500 publicly labeled EgoSchema examples, not the unlabeled 5031-question full set',
        prior_300_reused=True,new_script_questions=200,new_videotree_questions=500,
        timing='SCRIPT includes attributed source directory and evidence preparation; VideoTree includes width/depth/QA. Both exclude shared offline frame/feature preparation and cold setup. Dates/GPU sharing differ; not a controlled speed benchmark.',
        source_policy='Old 300 results retained with hashes; new 200 use the frozen default CLI implementation. Report the two subsets separately.',
        development_cohort=True)
    write(out/'report.json',result)
    with (out/'reports/per_question.csv.tmp').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['question_id','truth','subset','script_answer','script_error',
            'script_seconds','videotree_answer','videotree_error','videotree_seconds'])
        writer.writeheader()
        old=set(cohorts['300'])
        for uid in cohorts['500']:
            a,b=ours.get(uid,{}),baseline.get(uid,{})
            writer.writerow(dict(question_id=uid,truth=truth[uid],subset='prior300' if uid in old else 'additional200',
                script_answer=prediction(a,'script'),script_error=json.dumps(a.get('error'),ensure_ascii=False),
                script_seconds=a.get('total_measured_seconds'),videotree_answer=prediction(b,'videotree'),
                videotree_error=json.dumps(b.get('error'),ensure_ascii=False),videotree_seconds=b.get('inference_seconds')))
    (out/'reports/per_question.csv.tmp').replace(out/'reports/per_question.csv')
    return result


def prepare(out, prior, upstream):
    from script_core.backend import preflight
    from script_core.temporal_entity import SETTINGS
    from script.run_partial_temporal_comparison import verify as verify_prior
    if out.exists():
        raise ValueError('Use a fresh output directory; existing spent work must be retained')
    verify_prior(prior)
    if read(prior/'completion.json')['completed'] != 300:
        raise ValueError('Require the complete prior temporal 300')
    data=(ROOT/'data/EgoSchema/egoschema').resolve()
    annotations,captions=read(data/'subset_anno.json'),read(data/'lavila_subset.json')
    cohorts=aligned_cohorts(annotations,prior)
    cohorts['additional200']=cohorts['500'][300:]
    questions={u:question(u,annotations[u]) for u in cohorts['500']}
    cfg=load_config(ROOT/'configs/script.yaml')
    if cfg['query']['pipeline']!='adaptive_partial_temporal':
        raise ValueError('Require the validated temporal default')
    previous=read(prior/'config.json')
    def comparable(c):
        c=deepcopy(c);c['query'].pop('pipeline')
        c['models']['revisions']={}
        for k in ('persistent_directory','persistent_scope'):
            c['cache'].pop(k,None)
        return c
    if comparable(cfg)!=comparable(previous):
        raise ValueError('Method configuration changed beyond default routing and isolated cache paths')
    prior_settings=read(prior/'settings.json')
    if any(prior_settings[k]!=v for k,v in SETTINGS.items()):
        raise ValueError('Evidence preparation settings changed')
    unchanged=('partial_temporal.py','backend.py','prompts.py','schemas.py','adaptive_entity.py','scene_memory.py')
    for name in unchanged:
        if sha(ROOT/'script_core'/name)!=sha(prior/'source/script_core'/name):
            raise ValueError('Temporal inference implementation changed: '+name)
    for uid in cohorts['500']:
        if annotations[uid].get('truth') not in range(5) or len(captions[uid])!=180:
            raise ValueError('Invalid labeled question/captions: '+uid)
        if not (data/'videos'/f'{uid}.mp4').is_file():
            raise ValueError('Missing video: '+uid)
    for uid in cohorts['300']:
        packet=read(prior/'inputs'/f'{uid}.json')
        r=read(prior/'formal/runs'/uid/'result.json')
        if (packet['question']!=questions[uid] or [c['text'] for c in packet['captions']]!=captions[uid]
                or r.get('question_id')!=uid or r.get('error') or not r.get('answer')
                or r.get('method')!='whole-scene-partial-temporal-v2'):
            raise ValueError('Prior result or source mismatch: '+uid)
    if read(prior/'evaluation_only.json')!={u:annotations[u]['truth'] for u in cohorts['300']}:
        raise ValueError('Prior evaluation labels changed')
    print('Verifying local model hashes before freezing the new run',flush=True)
    cfg,artifacts=preflight(cfg)
    old=read(prior/'artifacts.json')
    for kind in ('vlm','visual_encoder','detector','text_embedding'):
        if artifacts[kind]['hash']!=old[kind]['hash']:
            raise ValueError('Model checkpoint changed: '+kind)
    if artifacts['software']!=old['software']:
        raise ValueError('Model runtime software changed')
    out.mkdir(parents=True)
    snapshot(ROOT,out/'source')
    cfg['cache']['persistent_directory']=str(out/'cache/text_tools')
    cfg['cache']['persistent_scope']='adaptive_partial_temporal'
    cfg['compatibility']['cache_directory']=''
    write(out/'config.json',cfg);write(out/'artifacts.json',artifacts)
    write(out/'settings.json',SETTINGS);write(out/'cohort.json',cohorts['500']);write(out/'cohorts.json',cohorts)
    write(out/'questions.json',questions);write(out/'captions.json',{u:captions[u] for u in cohorts['500']})
    write(out/'evaluation_only.json',{u:annotations[u]['truth'] for u in cohorts['500']})
    write(out/'input_hashes.json',{})
    origins={};input_hashes={}
    for uid in cohorts['300']:
        target=out/'runs/script'/uid
        target.parent.mkdir(parents=True,exist_ok=True)
        target.symlink_to(prior/'formal/runs'/uid,target_is_directory=True)
        p=prior/'inputs'/f'{uid}.json';(out/'inputs').mkdir(exist_ok=True)
        shutil.copy2(p,out/'inputs'/p.name)
        input_hashes['inputs/'+p.name]=sha(out/'inputs'/p.name)
        origins[uid]=dict(source=str(target.resolve()),result_sha256=sha(target/'result.json'))
    write(out/'input_hashes.json',input_hashes);write(out/'carried_results.json',origins)
    vt=out/'videotree'
    for name in ('upstream','runtime','runs','ready','workers','reports'):
        (vt/name).mkdir(parents=True,exist_ok=True)
    for path in upstream.rglob('*.py'):
        if '.git' not in path.parts:
            dest=vt/'upstream'/path.relative_to(upstream);dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,dest)
    path=vt/'upstream/kmeans_pytorch/__init__.py'
    text=path.read_text().replace('from .soft_dtw_cuda import SoftDTW\n','')
    text=text.replace("elif distance == 'soft_dtw':\n","elif distance == 'soft_dtw':\n        from .soft_dtw_cuda import SoftDTW\n")
    text=text.replace('torch.nonzero(choice_cluster == index).squeeze()','torch.nonzero(choice_cluster == index).flatten()')
    path.write_text(text)
    shutil.copy2(ROOT/'model.py',vt/'runtime/model.py')
    write(vt/'questions.json',questions);write(vt/'captions.json',{u:captions[u] for u in cohorts['500']})
    write(vt/'durations.json',read(data/'duration.json'));write(vt/'cohorts.json',cohorts)
    write(vt/'experiment.json',dict(model=cfg['models']['vlm'],max_new_tokens=cfg['models']['generation']['max_new_tokens'],
        seed=cfg['seed'],feature_root=str((ROOT/'data/EgoSchema/egoschema_features').resolve()),
        inference_workers=1,per_question_timeout_seconds=1800,
        upstream=str(upstream),upstream_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=upstream,text=True).strip(),
        upstream_original_hashes={str(p.relative_to(upstream)):sha(p) for p in upstream.rglob('*.py') if '.git' not in p.parts},
        adaptation='Persistent local Qwen2.5-VL-7B; lazy optional SoftDTW import and modern Torch singleton-index shape. Original prompts, caption indexing, relevance mapping and QA parser retained.'))
    subprocess.run([sys.executable,'-c',
        'import adaptive_breath_expansion, depth_expansion, main_qa, prompts, util; '
        'from pathlib import Path; '
        'assert all(Path(m.__file__).resolve().parent == Path.cwd() for m in '
        '(adaptive_breath_expansion,depth_expansion,main_qa,prompts,util)); '
        'print("UPSTREAM_IMPORT_CHECK passed",flush=True)'],cwd=vt/'upstream',check=True)
    write(out/'experiment.json',dict(created_at=now(),prior=str(prior),planned=500,carried_script=300,new_script=200,
        new_videotree=500,method='whole-scene-partial-temporal-v2',default_pipeline='adaptive_partial_temporal',
        inference_adapters='Prior 300 paired fixed-evidence treatment; new 200 evidence-only local preparation followed by same temporal method and one final QA.',
        source_integrity='Same questions, original options, original 1FPS captions, feature encoder and local model checkpoints',
        unchanged_inference_modules=list(unchanged),old_cancelled_queues_resumed=False,
        scheduling=dict(allowed_gpus=['0','1','2','3'],preferred_gpu='0',poll_seconds=30,stable_observations=3,
            max_gpu_workers=1,minimum_free_mib=dict(features=40*1024,script=56*1024,videotree=40*1024),automatic_retry=False),
        feature_batch_size=8,frame_workers=3,development_cohort=True))
    frozen=[]
    for folder in (out/'source',vt/'upstream',vt/'runtime'):
        frozen.extend(p for p in folder.rglob('*') if p.is_file()
            and not any(x in ('data','checkpoints','__pycache__') for x in p.relative_to(folder).parts))
    frozen.extend(out/n for n in ('config.json','settings.json','cohort.json','cohorts.json','questions.json',
        'captions.json','evaluation_only.json','carried_results.json','experiment.json','artifacts.json'))
    frozen.extend(out/'inputs'/f'{u}.json' for u in cohorts['300'])
    frozen.extend(out/'runs/script'/u/'result.json' for u in cohorts['300'])
    frozen.extend(vt/n for n in ('questions.json','captions.json','durations.json','cohorts.json','experiment.json'))
    write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in frozen})
    verify(out);report(out)
    write(out/'queue_status.json',dict(state='prepared',updated_at=now()))
    print('PREPARED',str(out),flush=True)


def frames_worker(out):
    import cv2
    from data_extraction.extract_images import extract_video
    cv2.setNumThreads(1)
    ids=read(out/'cohorts.json')['additional200'];plan=read(out/'experiment.json')
    data=ROOT/'data/EgoSchema/egoschema/videos';dest=ROOT/'data/EgoSchema/egoschema_frames'
    stage_status(out,'frames',state='running',completed=0,planned=len(ids))
    with ThreadPoolExecutor(max_workers=plan['frame_workers']) as pool:
        futures={pool.submit(extract_video,data/f'{u}.mp4',dest/u,180):u for u in ids}
        for n,done in enumerate(as_completed(futures),1):
            done.result()
            stage_status(out,'frames',state='running',completed=n,planned=len(ids),question_id=futures[done])
    stage_status(out,'frames',state='complete',completed=len(ids),planned=len(ids))


def runtime_config(out):
    from script_core.backend import preflight
    cfg,artifacts=preflight(load_config(out/'config.json'));expected=read(out/'artifacts.json')
    if any(artifacts[k]['hash']!=expected[k]['hash'] for k in ('vlm','visual_encoder','detector','text_embedding')):
        raise ValueError('Frozen checkpoint content changed')
    if artifacts['software']!=expected['software']:
        raise ValueError('Frozen software changed')
    return cfg


def features_worker(out):
    import torch
    from script.run_four_arm_300_local import make_packet
    cfg=runtime_config(out);plan=read(out/'experiment.json');cohorts=read(out/'cohorts.json')
    ids=cohorts['additional200']
    stage_status(out,'features',state='running',completed=0,planned=len(ids))
    subprocess.run([sys.executable,str(ROOT/'data_extraction/extract_features.py'),'--model',cfg['models']['visual_encoder'],
        '--video-ids',*ids,'--batch-size',str(plan['feature_batch_size'])],cwd=ROOT,check=True)
    questions,captions=read(out/'questions.json'),read(out/'captions.json');old=set(cohorts['300'])
    for n,uid in enumerate(cohorts['500'],1):
        path=ROOT/'data/EgoSchema/egoschema_features'/f'{uid}.pt';meta=read(path.with_suffix('.json'))
        features=torch.load(path,map_location='cpu',weights_only=True)
        if (meta.get('model')!=cfg['models']['visual_encoder'] or meta.get('fps')!=1
                or list(features.shape)!=[180,1280] or not torch.isfinite(features).all()
                or meta.get('frame_indices')!=list(range(180))):
            raise ValueError('Feature model, grid or values changed: '+uid)
        if uid not in old:
            records=[dict(id=f'C{i}',interval=[i,i+1],text=t) for i,t in enumerate(captions[uid])]
            make_packet(out,uid,questions[uid],records,cfg)
        else:
            packet=read(out/'inputs'/f'{uid}.json')
            if packet['feature_hash']!=sha(path):raise ValueError('Prior feature content changed: '+uid)
        write(out/'videotree/ready'/f'{uid}.json',dict(ready=True,feature_uri=str(path.resolve()),feature_sha256=sha(path)))
        stage_status(out,'features',state='publishing',completed=n,planned=500,question_id=uid)
    stage_status(out,'features',state='complete',completed=200,planned=200,all_500_inputs_published=True)


def script_worker(out):
    import torch
    from script_core.budget import ModelWorker, BudgetLedger, Meter
    from script_core.scheme4 import answer
    cfg=runtime_config(out);ids=read(out/'cohorts.json')['additional200']
    backend=ModelWorker(cfg)
    try:
        ledger=BudgetLedger(600,answer_reserve=0,calls_path=out/'setup/script/calls.jsonl')
        meter=Meter(backend,ledger,cfg,out/'setup/script/raw_calls')
        try:meter.call('prepare_vlm',{},build=True)
        finally:write(out/'setup/script/cost_summary.json',ledger.summary());meter.close()
        for n,uid in enumerate(ids,1):
            target=out/'runs/script'/uid
            if target.exists():raise ValueError('Question already has spent work: '+uid)
            stage_status(out,'script',state='running',completed=n-1,planned=len(ids),question_id=uid)
            packet=read(out/'inputs'/f'{uid}.json')
            expected=read(out/'input_hashes.json')[f'inputs/{uid}.json']
            if sha(out/'inputs'/f'{uid}.json')!=expected or sha(packet['feature_uri'])!=packet['feature_hash']:
                raise ValueError('Published input content changed: '+uid)
            features=torch.load(packet['feature_uri'],map_location='cpu',weights_only=True).tolist()
            result=answer(packet,features,cfg,backend,target)
            if resource_error(result):raise RuntimeError('CUDA memory exhausted; failed question retained, no automatic retry')
            stage_status(out,'script',state='running',completed=n,planned=len(ids),question_id=uid)
    finally:backend.close(graceful=True)
    stage_status(out,'script',state='complete',completed=len(ids),planned=len(ids))


def videotree_worker(out):
    from script import run_original_videotree_local as vt
    runtime_config(out)
    root=out/'videotree';sys.path.insert(0,str(root/'upstream'))
    # Import original modules in a dedicated process, keeping their imports and
    # prompts separate from the SCRIPT backend.
    width=importlib.import_module('adaptive_breath_expansion')
    depth=importlib.import_module('depth_expansion')
    qa=importlib.import_module('main_qa')
    plan=read(root/'experiment.json');model=vt.TracedModel(root,plan)
    ids=read(out/'cohorts.json')['500']
    for n,uid in enumerate(ids,1):
        stage_status(out,'videotree',state='running',completed=n-1,planned=len(ids),question_id=uid)
        ready=read(root/'ready'/f'{uid}.json')
        if sha(ready['feature_uri'])!=ready['feature_sha256']:
            raise ValueError('VideoTree feature content changed: '+uid)
        result=vt.run_question(root,uid,model,width,depth,qa,ready)
        if resource_error(result):raise RuntimeError('CUDA memory exhausted; failed question retained, no automatic retry')
        stage_status(out,'videotree',state='running',completed=n,planned=len(ids),question_id=uid)
        print('VIDEOTREE',n,len(ids),uid,'error',result.get('error'),flush=True)
    stage_status(out,'videotree',state='complete',completed=len(ids),planned=len(ids))


def gpu_env(uuid):
    return dict(os.environ,CUDA_VISIBLE_DEVICES=uuid,CUDA_DEVICE_ORDER='PCI_BUS_ID',HF_HUB_OFFLINE='1',
        HF_HOME=str(ROOT/'checkpoints/.cache/huggingface'),HF_MODULES_CACHE=str(ROOT/'checkpoints/.cache/modules'),
        TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')


def queue(out):
    from script.run_optimized_comparison import inventory
    verify(out);policy=read(out/'experiment.json')['scheduling']
    command=[sys.executable,'-u',str(out/'source/script'/Path(__file__).name)]
    (out/'logs').mkdir(exist_ok=True)
    with (out/'controller.lock').open('a') as controller_lock:
        fcntl.flock(controller_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if any((out/'workers').glob('*.json')):
            raise ValueError('Workers were already attempted; inspect retained state instead of resetting budgets')
        with (out/'logs/frames.log').open('a') as log:
            frames=subprocess.Popen([*command,'frames','--out',str(out)],cwd=out/'source',stdout=log,stderr=subprocess.STDOUT)
        while frames.poll() is None:
            write(out/'queue_status.json',dict(state='preparing_frames',pid=os.getpid(),frames_pid=frames.pid,updated_at=now()))
            report(out);time.sleep(10)
        if frames.returncode:
            write(out/'queue_status.json',dict(state='failed',stage='frames',exit_code=frames.returncode,updated_at=now()))
            return frames.returncode
        for stage in STAGES:
            stable={}
            while True:
                observed={i:r for i,r in inventory().items() if i in policy['allowed_gpus']}
                minimum=policy['minimum_free_mib'][stage]
                stable={i:stable.get(i,0)+1 if r['free_mib']>=minimum else 0 for i,r in observed.items()}
                write(out/'queue_status.json',dict(state='waiting_for_gpu_memory',stage=stage,pid=os.getpid(),
                    minimum_free_mib=minimum,gpus=observed,stable_observations=stable,updated_at=now()))
                report(out)
                selected=None
                for index in sorted(observed,key=lambda i:(i!=policy['preferred_gpu'],-observed[i]['free_mib'])):
                    if stable[index]<policy['stable_observations']:continue
                    row=observed[index];lease=(Path('/tmp')/f'videoagent-{os.getuid()}-{row["uuid"]}.lock').open('a')
                    try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:lease.close();continue
                    fresh=inventory().get(index)
                    if not fresh or fresh['uuid']!=row['uuid'] or fresh['free_mib']<minimum:
                        lease.close();continue
                    selected=(index,row,lease);break
                if selected is None:
                    time.sleep(policy['poll_seconds']);continue
                index,row,lease=selected
                with (out/'logs'/f'{stage}.log').open('a') as log:
                    proc=subprocess.Popen([*command,stage,'--out',str(out)],cwd=out/'source',env=gpu_env(row['uuid']),
                        stdout=log,stderr=subprocess.STDOUT,pass_fds=(lease.fileno(),))
                running=dict(state='running',stage=stage,pid=os.getpid(),worker_pid=proc.pid,gpu_index=index,gpu_uuid=row['uuid'])
                write(out/'launches'/f'{stage}.json',dict(running,started_at=now()))
                while proc.poll() is None:
                    write(out/'queue_status.json',dict(running,updated_at=now()));report(out);time.sleep(10)
                lease.close();report(out)
                if proc.returncode:
                    write(out/'queue_status.json',dict(running,state='failed',exit_code=proc.returncode,updated_at=now()))
                    return proc.returncode
                break
        result=report(out)
        complete=all(result['reports']['500'][m]['complete'] for m in ('script','videotree'))
        write(out/'completion.json',dict(completed_at=now(),complete=complete))
        write(out/'queue_status.json',dict(state='complete' if complete else 'incomplete',updated_at=now(),pid=os.getpid()))
        return 0 if complete else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','queue','frames',*STAGES,'report'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--prior',type=Path,default=ROOT/'workdirs/partial-temporal-300-20261027-r2')
    parser.add_argument('--videotree',type=Path,default=ROOT.parent/'VideoTree')
    parser.add_argument('--detach',action='store_true')
    args=parser.parse_args();out=args.out.resolve()
    try:
        if args.mode=='prepare':
            prepare(out,args.prior.resolve(),args.videotree.resolve())
            if args.detach:
                command=[sys.executable,'-u',str(out/'source/script'/Path(__file__).name),'queue','--out',str(out)]
                start=out/'start.sh';start.write_text('#!/bin/bash\nset -euo pipefail\nexec '+shlex.join(command)+' >> '+shlex.quote(str(out/'logs/controller.log'))+' 2>&1\n')
                (out/'logs').mkdir(exist_ok=True)
                session='script-full-videotree-'+str(int(time.time()))
                subprocess.run(['tmux','new-session','-d','-s',session,'bash '+shlex.quote(str(start))],check=True)
                write(out/'launch.json',dict(session=session,command=command,created_at=now()))
            return 0
        verify(out)
        if args.mode=='report':report(out);return 0
        if args.mode=='queue':return queue(out)
        {'frames':frames_worker,'features':features_worker,'script':script_worker,'videotree':videotree_worker}[args.mode](out)
        return 0
    except Exception as exc:
        if out.exists():
            write(out/'failures'/f'{args.mode}.json',dict(updated_at=now(),type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc()))
            if args.mode in ('frames',*STAGES):stage_status(out,args.mode,state='failed',error=str(exc))
        raise


if __name__=='__main__':raise SystemExit(main())
