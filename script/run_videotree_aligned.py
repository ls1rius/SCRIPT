"""Run original VideoTree and matched continuous flat/script memory controls."""
import argparse
from copy import deepcopy
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'script'))
from script_core.config import load_config,digest
from script_core.videotree_aligned import defaults
from run_temporal_entity import read,write,sha,checked_packet
ARMS=dict(videotree='Fresh original VideoTree width/depth/QA',
          continuous_flat='Contiguous VideoTree + shared visual evidence rendered as flat observations',
          continuous_script='Same contiguous VideoTree and visual evidence rendered as temporal entity network')


def prepare(out,reference,shards):
    if out.exists():raise ValueError('Use a new experiment directory')
    out.mkdir(parents=True)
    cohort=read(reference/'cohort.json')
    if len(cohort)!=300 or len(set(cohort))!=300:raise ValueError('Require frozen matched 300 questions')
    cfg=load_config(reference/'config.json')
    # Restore the common protocol; no separate 600-second directory allowance.
    cfg['budget'].update(online_cap=600.,tool_call_cap=60.,final_answer_reserve=30.,build_call_cap=600.)
    cfg['compatibility'].update(enabled=False,cache_directory='')
    cfg['scene_memory']['enabled']=False
    write(out/'config.json',cfg);write(out/'cohort.json',cohort)
    source_inputs=read(reference/'input_hashes.json')
    for uid in cohort:
        name=f'inputs/{uid}.json';source=reference/name
        if sha(source)!=source_inputs[name]:raise ValueError('Frozen reference input changed')
        dest=out/name;dest.parent.mkdir(exist_ok=True);shutil.copyfile(source,dest)
    write(out/'input_hashes.json',{n:sha(out/n) for n in source_inputs})
    shutil.copyfile(reference/'evaluation_only.json',out/'evaluation_only.json')
    write(out/'smoke/ids.json',read(reference/'smoke/ids.json'))
    for group in range(shards):write(out/f'shards/{group}.json',cohort[group::shards])
    for source in ROOT.rglob('*.py'):
        rel=source.relative_to(ROOT)
        if any(t in ('data','workdirs','checkpoints','.git','__pycache__') for t in rel.parts):continue
        dest=out/'source'/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,dest)
    for name in ('data','checkpoints'):(out/'source'/name).symlink_to((ROOT/name).resolve(),target_is_directory=True)
    for source in (ROOT/'tests/fixtures').glob('*.json'):
        dest=out/'source/tests/fixtures'/source.name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,dest)
    for name in ('script/videotree_aligned.pbs','docs/SCRIPT_Temporal_Entity_Relation_Plan_2026-10-19.md'):
        dest=out/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,dest)
    write(out/'source_hashes.json',{str(p.relative_to(out/'source')):sha(p) for p in (out/'source').rglob('*')
        if p.is_file() and not any(t in ('data','checkpoints','__pycache__') for t in p.relative_to(out/'source').parts)})
    write(out/'control_hashes.json',{n:sha(out/n) for n in ('config.json','cohort.json','smoke/ids.json')})
    write(out/'experiment.json',dict(method='videotree_aligned_contiguous_script',arms=ARMS,questions_per_arm=300,
        shards=shards,reference=str(reference),videotree_defaults=defaults(),online_cap_seconds=600.,
        qa_generation=cfg['models']['generation'],common_model_input_tokens=cfg['models']['max_input_tokens'],
        workers=dict(videotree='independent baseline job',paired='one evidence build, two independent QA calls'),
        notes=['Earlier jobs and frozen experiment directories remain unchanged.',
        'Original width launch is reused by swapping only kmeans for an exact fixed-K contiguous visual partition.',
        'Width starts at 8, doubles to 16/32, stops at four relevance-3 representatives, exactly as current VideoTree defaults.',
        'Original raw-mean/Euclidean primary representative selector, cap_score prompt, retries and cluster-ID alignment are reused.',
        'Original depth Ward 4/4 and cosine representative selection, qa_standard, Qwen.forward, decoding, A-E order are reused.',
        'No CLIP text/options weighted ranking, custom sufficiency review, four-scene cutoff or 1500-second online budget.',
        'All selected scenes are eligible for memory; pointers order visits by original relevance, with reverse traversal.',
        'No unselected images are read. Flat/script arms share one extraction, identity resolution, frame set and retained records.',
        'Flat/script comparison isolates memory rendering on this single-question cohort, not the full benefit of multi-query caching.',
        'Original baseline uses captions only; original-vs-new comparison bundles contiguous clustering and visual-memory effects.',
        'No arbitrary top-two lexical identity candidates or fixed 12-entity/32-state QA cutoff.',
        'Complete entity units rank by original scene relevance and time; both QA renderings must fit the same context cap.',
        'New entity API uses batched images and constrained JSON (up to 4096 generation tokens); no VideoTree counterpart exists.',
        'New summaries/entity/identity extraction consume the same 600-second cap; no free preprocessing phase.',
        'Paired build reserves two final QA allowances; each result is charged full shared construction plus its own QA.',
        'Budget-interrupted graphs are retained, paired on the exact same partial evidence, and reported as incomplete.',
        'Smoke gates execution invariants and exact baseline prompt/selection reproduction, never accuracy improvement.',
        'Same 300 development questions; no held-out or equal-information claim against the caption-only original baseline.']))
    print(json.dumps(dict(prepared=300,out=str(out),arms=ARMS,shards=shards)),flush=True)


def execute(out,packet,worker,backend,cfg,target):
    from script_core.aligned_pipeline import baseline,pair
    uid=packet['question']['question_id']
    root=target/worker/'runs'/uid
    if (root/'result.json').exists():return read(root/'result.json')
    try:
        if worker=='videotree':return baseline(packet,backend,cfg,root)
        return dict(results=pair(packet,backend,cfg,root))
    except Exception as exc:
        err=dict(type=type(exc).__name__,message=str(exc))
        value=dict(error=err,question_id=uid,answer=None)
        write(root/'result.json',value)
        if worker=='paired':
            for arm in ('continuous_flat','continuous_script'):write(root/arm/'result.json',dict(value,arm=arm))
        return value


def result_path(out,arm,uid,smoke=False):
    target=out/'smoke' if smoke else out
    if arm=='videotree':return target/'videotree/runs'/uid/'result.json'
    return target/'paired/runs'/uid/arm/'result.json'


def gate(out):
    failures=[];baseline_checks={}
    evaluation=read(out/'evaluation_only.json')
    for uid in read(out/'smoke/ids.json'):
        results={}
        for arm in ARMS:
            path=result_path(out,arm,uid,True)
            if not path.exists():failures.append(dict(id=uid,arm=arm,reason='missing result'));continue
            r=read(path);results[arm]=r
            if r.get('error') or not r.get('answer'):
                failures.append(dict(id=uid,arm=arm,reason=r.get('error') or 'missing answer'));continue
            cost=r['cost_summary']
            if cost['budget_violation'] or cost['budgeted_total']>600.05:
                failures.append(dict(id=uid,arm=arm,reason='common budget violated'))
            if arm=='videotree':
                expected=evaluation[uid].get('videotree')
                if expected:
                    checks=dict(selected_equal=r['selected_seconds']==expected['selected_seconds'],
                        head_equal=r['qa_inputs']['head']==expected['head'],prompts_equal=r['qa_inputs']['prompts']==expected['prompts'])
                    baseline_checks[uid]=checks
                    if not all(checks.values()):failures.append(dict(id=uid,arm=arm,reason=checks))
        if all(a in results and not results[a].get('error') for a in ('continuous_flat','continuous_script')):
            a,b=results['continuous_flat'],results['continuous_script']
            for field in ('selected_seconds','selected_frame_ids','observed_frame_ids','context_hash','shared_construction_hash','opened_scenes','incomplete'):
                if a[field]!=b[field]:failures.append(dict(id=uid,reason='paired evidence differs: '+field))
            if not set(a['observed_frame_ids'])<=set(a['selected_frame_ids']):failures.append(dict(id=uid,reason='unselected image read'))
            if not a['observed_frame_ids'] or a['entity_count']==0:failures.append(dict(id=uid,reason='no visual entity extraction'))
            d=read(out/'smoke/paired/runs'/uid/'shared/directory.json')
            cursor=0
            for scene in d['scenes']:
                if scene['interval'][0]!=cursor or scene['chronological_order']!=list(range(*scene['interval'])):
                    failures.append(dict(id=uid,reason='noncontiguous scene'))
                cursor=scene['interval'][1]
            if cursor!=len(d['captions']):failures.append(dict(id=uid,reason='directory coverage lost'))
    report=dict(passed=not failures,failures=failures,baseline_reproduction=baseline_checks,accuracy_used_for_gate=False,
        budget_incompleteness_policy='report incomplete; execution gate does not select based on accuracy or graph size')
    write(out/'smoke/status.json',report);print(json.dumps(report),flush=True)
    return report['passed']


def work(out,worker,group,smoke):
    from script_core.backend import preflight
    from script_core.budget import ModelWorker
    for n,h in read(out/'source_hashes.json').items():
        if sha(ROOT/n)!=h:raise ValueError('Frozen source changed: '+n)
    for n,h in read(out/'control_hashes.json').items():
        if sha(out/n)!=h:raise ValueError('Frozen control changed: '+n)
    cfg,artifacts=preflight(load_config(out/'config.json'))
    target=out/'smoke' if smoke else out
    write(target/'setup'/f'{worker}-{group}.json',artifacts)
    ids=read(out/'smoke/ids.json' if smoke else out/f'shards/{group}.json')
    backend=ModelWorker(cfg)
    try:
        for i,uid in enumerate(ids):
            packet=checked_packet(out,uid)
            for current in (('videotree','paired') if smoke else [worker]):
                value=execute(out,packet,current,backend,cfg,target)
                print('ALIGNED',current,i+1,uid,json.dumps(value.get('error')),flush=True)
                write(target/'progress'/f'{current}-{group}.json',dict(completed=i+1,planned=len(ids),last_id=uid,error=value.get('error')))
    finally:backend.close()
    if smoke and not gate(out):raise SystemExit(1)


def report(out):
    evaluation=read(out/'evaluation_only.json');summary={}
    cohort=read(out/'cohort.json')
    for arm in ARMS:
        rows=[read(result_path(out,arm,u)) for u in cohort if result_path(out,arm,u).exists()]
        correct=sum(str((r.get('answer') or {}).get('selected_option'))==str(evaluation[r['question_id']]['truth']) for r in rows)
        summary[arm]=dict(completed=len(rows),planned=300,correct=correct,accuracy_fixed_denominator=correct/300,
            errors=sum(bool(r.get('error')) for r in rows),incomplete=sum(bool(r.get('incomplete')) for r in rows),
            unresolved_identity_comparisons=sum(r.get('unresolved_identity_comparisons',0) for r in rows),
            observation_page_recoveries=sum(r.get('observation_page_recoveries',0) for r in rows),
            alignment_repairs=sum(r.get('alignment_repairs',0) for r in rows),
            standalone_processing_seconds=sum(r.get('cost_summary',r.get('shared_cost_summary',{})).get('budgeted_total',0) for r in rows))
    value=dict(complete=all(s['completed']==300 for s in summary.values()),arms=summary)
    write(out/'comparison.json',value);print(json.dumps(value,indent=2));return value


def submit(out):
    path=out/'jobs.json';jobs=read(path) if path.exists() else dict(smoke=None,formal=[],report=None)
    def save():write(path,jobs)
    def qsub(name,env,log,dependency=None,priority=0,walltime=None):
        cmd=['qsub','-N',name,'-p',str(priority),'-o',str(log),'-v',env]
        if dependency:cmd+=['-W','depend='+dependency]
        if walltime:cmd+=['-l','walltime='+walltime]
        if name=='vta-report':cmd+=['-l','select=1:ncpus=1:mem=4GB']
        cmd+=[str(out/'source/script/videotree_aligned.pbs')]
        p=subprocess.run(cmd,capture_output=True,text=True)
        if p.returncode:raise RuntimeError(p.stderr)
        return p.stdout.strip()
    if not jobs['smoke']:
        jobs['smoke']=qsub('vta-smoke',f'ALIGNED_DIR={out},ALIGNED_STAGE=smoke',out/'smoke.pbs.log',priority=1000,walltime='04:00:00');save()
    existing={(r['worker'],r['group']) for r in jobs['formal']}
    (out/'logs').mkdir(exist_ok=True)
    for group in range(read(out/'experiment.json')['shards']):
        for idx,worker in enumerate(('paired','videotree')):
            if (worker,group) in existing:continue
            job=qsub(f'vta-{worker[:4]}-{group:02d}',f'ALIGNED_DIR={out},ALIGNED_STAGE=formal,ALIGNED_WORKER={worker},ALIGNED_GROUP={group}',
                out/'logs'/f'{worker}-{group}.pbs.log','afterok:'+jobs['smoke'],priority=950-group*2-idx)
            jobs['formal'].append(dict(worker=worker,group=group,job_id=job,dependency='afterok:'+jobs['smoke']));save();print('SUBMITTED',job,worker,group,flush=True)
    if not jobs['report']:
        jobs['report']=qsub('vta-report',f'ALIGNED_DIR={out},ALIGNED_STAGE=report',out/'report.pbs.log',
            'afterany:'+':'.join(r['job_id'] for r in jobs['formal']),walltime='00:10:00');save()
    print(json.dumps(jobs),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['prepare','run','gate','report','submit']);p.add_argument('--out',required=True)
    p.add_argument('--reference');p.add_argument('--shards',type=int,default=6)
    p.add_argument('--worker',choices=['videotree','paired'],default='paired');p.add_argument('--group',type=int,default=0)
    p.add_argument('--smoke',action='store_true');a=p.parse_args();out=Path(a.out).resolve()
    if a.command=='prepare':prepare(out,Path(a.reference).resolve(),a.shards)
    elif a.command=='run':work(out,a.worker,a.group,a.smoke)
    elif a.command=='gate':raise SystemExit(0 if gate(out) else 1)
    elif a.command=='submit':submit(out)
    else:report(out)
