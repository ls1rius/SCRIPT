"""Frozen experiments: visual scene segmentation, summary-derived entity memory."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script_core.config import load_config, digest
from script_core.temporal_entity import SETTINGS,validate_directory
ARMS=dict(cluster_qa='Contiguous visual scenes + center retrieval + original QA',
          temporal_local='Same entry scene + summary-derived temporal entity network + original QA',
          summary_deep='Progressive summary search + independent scene-local entities + original QA',
          temporal_cross='Same evidence as summary_deep + cross-stage identity and entity history + original QA')

def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.replace(path)

def prepare(out,reference,shards=12,reuse_frozen_inputs=False,sparse_visual=False):
    if out.exists():raise ValueError('Use a new experiment output directory')
    cohort=read(reference/'cohort.json')
    if len(cohort)!=300 or len(set(cohort))!=300:raise ValueError('Require fixed 300 cohort')
    out.mkdir(parents=True)
    cfg=load_config(reference/'config.json')
    cfg['query'].update(temporal_entity_source='summary',temporal_sparse_visual=sparse_visual)
    cfg['budget'].update(online_cap=SETTINGS['online_seconds'],tool_call_cap=150.,final_answer_reserve=45.)
    cfg['compatibility'].update(enabled=False,cache_directory='')
    cfg['scene_memory']['enabled']=False
    cfg['query'].update(answer_order='original',tool_transient_retries=0,json_repair_retries=1)
    write(out/'config.json',cfg);write(out/'settings.json',SETTINGS);write(out/'cohort.json',cohort)
    frames_root=(ROOT/'data/EgoSchema/egoschema_frames').resolve()
    features_root=(ROOT/'data/EgoSchema/egoschema_features').resolve()
    frozen_hashes=read(reference/'input_hashes.json') if reuse_frozen_inputs else {}
    def one(uid):
        source=reference/'inputs'/f'{uid}.json'
        packet=read(source)
        if set(packet['question'])-{'question_id','video_id','question','options','scope'}:
            raise ValueError('Unauthorized question metadata')
        if packet['question']['video_id']!=uid:raise ValueError('Expected unique videos')
        if reuse_frozen_inputs:
            if sha(source)!=frozen_hashes[f'inputs/{uid}.json']:
                raise ValueError('Frozen reference input changed')
            if len(packet['frames'])!=len(packet['captions']) or digest(packet['captions'])!=packet['caption_records_hash']:
                raise ValueError('Frozen reference evidence incomplete')
            (out/'inputs').mkdir(exist_ok=True)
            shutil.copyfile(source,out/'inputs'/f'{uid}.json')
            return uid
        meta=read(features_root/f'{uid}.json')
        if meta['fps']!=1 or meta['frame_indices']!=list(range(len(packet['captions']))) or meta['shape']!=[len(packet['captions']),1280]:
            raise ValueError('Visual feature timestamp grid mismatch')
        if Path(meta['model']).resolve()!=Path(cfg['models']['visual_encoder']).resolve():
            raise ValueError('Source encoder does not match retrieval encoder')
        feature=features_root/f'{uid}.pt'
        frame_rows=[]
        from PIL import Image
        for i in meta['frame_indices']:
            path=frames_root/uid/f'{i}.jpg'
            with Image.open(path) as image:w,h=image.size
            frame_rows.append(dict(frame_id=f'F{i}',pts=float(i),path=str(path),width=w,height=h,sha256=sha(path)))
        packet.update(feature_uri=str(feature),feature_hash=sha(feature),feature_metadata=meta,
            frames=frame_rows,caption_records_hash=digest(packet['captions']))
        write(out/'inputs'/f'{uid}.json',packet)
        return uid
    with ThreadPoolExecutor(max_workers=8) as pool:
        for n,uid in enumerate(pool.map(one,cohort),1):
            if n%50==0:print('PREPARED',n,flush=True)
    # Inference never reads evaluation_only; accuracy is computed only by report().
    shutil.copyfile(reference/'evaluation_only.json',out/'evaluation_only.json')
    smoke=read(reference/'smoke/ids.json')[:2]
    write(out/'smoke/ids.json',smoke)
    for group in range(shards):write(out/f'shards/{group}.json',cohort[group::shards])
    for path in ROOT.rglob('*.py'):
        if any(part in ('data','workdirs','checkpoints','.git','__pycache__') for part in path.relative_to(ROOT).parts):continue
        dest=out/'source'/path.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest)
    for name in ('data','checkpoints'):(out/'source'/name).symlink_to((ROOT/name).resolve(),target_is_directory=True)
    for source in (ROOT/'tests/fixtures').glob('*.json'):
        dest=out/'source/tests/fixtures'/source.name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,dest)
    for name in ('script/temporal_entity.pbs','docs/SCRIPT_Temporal_Entity_Relation_Plan_2026-10-19.md'):
        dest=out/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,dest)
    source_hashes={str(p.relative_to(out/'source')):sha(p) for p in (out/'source').rglob('*') if p.is_file() and
                   not any(t in ('data','checkpoints','__pycache__') for t in p.relative_to(out/'source').parts)}
    write(out/'source_hashes.json',source_hashes)
    write(out/'input_hashes.json',{str(p.relative_to(out)):sha(p) for p in (out/'inputs').glob('*.json')})
    write(out/'control_hashes.json',{n:sha(out/n) for n in ('config.json','settings.json','cohort.json','smoke/ids.json')})
    arms=deepcopy(ARMS)
    if sparse_visual:
        arms['summary_deep']+=' + optional bounded visual check after unresolved summary gap'
        arms['temporal_cross']+='; reuses the exact same visual observations without rereading frames'
    from script_core.sparse_visual import LIMITS
    write(out/'experiment.json',dict(method='contiguous_summary_temporal_entities',entity_source='summary',arms=arms,
        sparse_visual=dict(enabled=sparse_visual,arm='summary_deep',replayed_by='temporal_cross',limits=LIMITS),
        paired_evidence=dict(source='summary_deep',target='temporal_cross',worker='summary_pair',
            same_summaries=True,same_sampled_visual_observations=True,source_answer_visible=False,
            original_shared_preparation_cost_charged=True),
        questions_per_arm=300,shards=shards,reference=str(reference),smoke_ids=smoke,
        settings=SETTINGS,qa_generation=cfg['models']['generation'],qa_option_order='original A-E',
        frozen_inputs_reused=reuse_frozen_inputs,
        notes=['New implementation; old scene/key-memory results are not results of this method.',
        'Same 300 development videos/questions/captions, same EVA/Qwen weights; no held-out claim.',
        'Directory uses contiguous exact-DP visual segmentation, normalized mean center and whole-scene caption summary.',
        'Frames rank by cosine to center; chronological order remains independent. Similar-scene pointers are bidirectional.',
        'Directory sees no questions. Entity extraction uses original timestamped summaries/captions, no images/questions/answers.',
        'Attributes, states, relations and actions retain caption citations; text assertions are never pixel verification.',
        'Whole-scene text inventories use sparse state-change records rather than per-frame visual observations.',
        'Cross-scene identity uses text evidence from each entity own source captions; ambiguous or competing matches stay unresolved.',
        'State fragments across gaps are retained without assuming persistence or inventing causes.',
        'Bounded entity retrieval exposes omission counts; complete networks stay in network.json.',
        '600-second directory + 1500-second online caps, unlike historical 600-second old method; report cost with accuracy.',
        'Cluster/local shards use independent jobs; deep/cross use a paired GPU worker with identical source evidence.',
        'Cross-stage QA cannot see the deep control answer; original shared preparation cost still consumes the cross-stage budget.',
        'Formal jobs depend only on successful execution smoke. Accuracy never controls gate or configuration.',
        'Interrupted queries retain graph/call traces; error results count wrong. Budget-truncated extraction is marked incomplete.',
        'Initial trial uses at most four scenes; exhaustive coverage is not guaranteed.']))
    print(json.dumps(dict(prepared=300,arms=arms,output=str(out))),flush=True)


def checked_packet(out,uid,check_images=True):
    p=out/'inputs'/f'{uid}.json'
    if sha(p)!=read(out/'input_hashes.json')[str(p.relative_to(out))]:raise ValueError('Frozen packet changed')
    packet=read(p)
    if sha(packet['feature_uri'])!=packet['feature_hash']:raise ValueError('Visual features changed')
    for f in packet['frames'] if check_images else []:
        if sha(f['path'])!=f['sha256']:raise ValueError('Source image changed')
    if digest(packet['captions'])!=packet['caption_records_hash']:raise ValueError('Caption source changed')
    return packet


def execute(out,arm,uid,backend,cfg,smoke=False):
    from script_core.budget import BudgetLedger,Meter
    from script_core.temporal_pipeline import directory,run,run_cross_replay
    import torch
    target=(out/'smoke' if smoke else out)/'arms'/arm/'runs'/uid
    if (target/'result.json').exists():return read(target/'result.json')
    target.mkdir(parents=True,exist_ok=True)
    started=time.monotonic()
    try:
        caption_mode=cfg['query'].get('temporal_entity_source','summary')=='summary'
        packet=checked_packet(out,uid,check_images=not caption_mode)
        if arm=='temporal_cross' and caption_mode:
            source=target.parent.parent.parent/'summary_deep/runs'/uid/'evidence_bundle.json'
            if not source.exists():raise ValueError('Paired summary_deep evidence is required; run --arm summary_pair')
            bundle=read(source);d=bundle['directory']
            write(target/'directory.json',d)
            r=run_cross_replay(packet,bundle,backend,cfg,target)
            r['directory_cost_summary']=d['cost_summary']
            r['total_measured_seconds']=r['cost_summary']['total']+d['cost_summary']['total']
            r['wall_seconds']=time.monotonic()-started
            r.update(source_image_hashes_verified=False,source_caption_hash_verified=True,
                     shared_evidence_bundle=str(source))
            write(target/'result.json',r);return r
        ledger=BudgetLedger(SETTINGS['directory_seconds'],answer_reserve=0,calls_path=target/'directory_calls/calls.jsonl')
        meter=Meter(backend,ledger,cfg,target/'directory_calls/raw_calls')
        try:
            feature=torch.load(packet['feature_uri'],map_location='cpu',weights_only=True)
            d=directory(packet,feature.tolist(),meter)
            d.update(feature_hash=packet['feature_hash'],cost_summary=ledger.summary())
            validate_directory(d);write(target/'directory.json',d)
        finally:meter.close()
        allow_sparse=caption_mode and arm=='summary_deep' and cfg['query'].get('temporal_sparse_visual',False)
        r=run(packet,d,packet['frames'] if not caption_mode or allow_sparse else [],backend,cfg,target,arm)
        r['directory_cost_summary']=d['cost_summary']
        r['total_measured_seconds']=r['cost_summary']['total']+d['cost_summary']['total']
        r['wall_seconds']=time.monotonic()-started
        r['source_image_hashes_verified']=not caption_mode
        r['source_caption_hash_verified']=True
        write(target/'result.json',r)
        return r
    except Exception as exc:
        r=dict(arm=arm,question_id=uid,answer=None,error=dict(type=type(exc).__name__,message=str(exc)),wall_seconds=time.monotonic()-started)
        write(target/'result.json',r);return r


def gate(out):
    failures=[];config=read(out/'config.json')
    caption_mode=config['query'].get('temporal_entity_source')=='summary'
    sparse_visual=config['query'].get('temporal_sparse_visual',False)
    for uid in read(out/'smoke/ids.json'):
        entries=[]
        for arm in ARMS:
            path=out/'smoke/arms'/arm/'runs'/uid/'result.json'
            if not path.exists():failures.append(dict(id=uid,arm=arm,reason='missing result'));continue
            r=read(path)
            if r.get('error') or not r.get('answer'):
                failures.append(dict(id=uid,arm=arm,reason=r.get('error') or 'missing QA answer'));continue
            if r['cost_summary']['budget_violation'] or r['directory_cost_summary']['budget_violation']:
                failures.append(dict(id=uid,arm=arm,reason='budget violation'))
            if r['incomplete'] or r['inventory_incomplete']:
                failures.append(dict(id=uid,arm=arm,reason='incomplete entity construction'))
            if caption_mode:
                calls=[json.loads(line) for line in (path.parent/'calls.jsonl').read_text().splitlines()]
                allow_visual=sparse_visual and arm=='summary_deep'
                for c in calls:
                    image_input=bool(c.get('model_frame_views') or c.get('input_pixels'))
                    if c['operation'] in ('temporal_observe','temporal_align') or (image_input and
                            (not allow_visual or c['operation'] not in ('sparse_visual_observe','sparse_visual_verify'))):
                        failures.append(dict(id=uid,arm=arm,reason='image input outside permitted supplemental check'))
                supplemental=r.get('supplemental_visual',{})
                if (not allow_visual and r['pixel_frame_count']) or len(supplemental.get('selected_frame_ids',[]))>3 or len(supplemental.get('rounds',[]))>1 or supplemental.get('seconds',0)>60.05:
                    failures.append(dict(id=uid,arm=arm,reason='supplemental visual limit violation'))
                from script_core.caption_temporal import validate_network
                validate_network(read(path.parent/'network.json'),read(path.parent/'directory.json')['captions'])
                if arm!='cluster_qa' and (not r.get('caption_source_count') or not r['entity_count'] or not r['state_count']):
                    failures.append(dict(id=uid,arm=arm,reason='no source-grounded summary entity/state extraction'))
                if arm=='temporal_cross' and len(r['opened_scenes'])>1 and not any(c['operation']=='scene_correspondence_lineage' for c in calls):
                    failures.append(dict(id=uid,arm=arm,reason='missing text correspondence check'))
            elif arm!='cluster_qa' and (r['pixel_frame_count']==0 or r['entity_count']==0 or r['state_count']==0):
                failures.append(dict(id=uid,arm=arm,reason='no real visual entity/state extraction'))
            if arm in ('summary_deep','temporal_cross') and not r['reviews']:
                failures.append(dict(id=uid,arm=arm,reason='missing entity query/navigation'))
            if not caption_mode and arm=='temporal_cross' and len(r['opened_scenes'])>1 and not r['identity_comparisons']:
                failures.append(dict(id=uid,arm=arm,reason='missing identity comparison before transfer'))
            entries.append(r['retrieval']['entry_scene'])
            validate_directory(read(path.parent/'directory.json'))
        if len(set(entries))>1:failures.append(dict(id=uid,reason='local arms did not share entry evidence'))
        deep=out/'smoke/arms/summary_deep/runs'/uid
        cross=out/'smoke/arms/temporal_cross/runs'/uid
        if (deep/'result.json').exists() and (cross/'result.json').exists():
            a,b=read(deep/'result.json'),read(cross/'result.json')
            if not a.get('error') and not b.get('error') and (a.get('opened_scenes')!=b.get('opened_scenes') or a.get('selected_seconds')!=b.get('selected_seconds') or
                a.get('supplemental_visual',{}).get('observations')!=b.get('supplemental_visual',{}).get('observations')):
                failures.append(dict(id=uid,reason='deep and cross-stage arms did not use identical evidence'))
            if (deep/'network.json').exists() and (read(deep/'network.json')['identities'] or read(deep/'network.json')['aliases']):
                failures.append(dict(id=uid,reason='deep control contains cross-stage identity links'))
            if not a.get('error') and not b.get('error'):
                bundle=read(deep/'evidence_bundle.json')
                if b.get('paired_evidence_hash')!=digest(bundle) or b['cost_summary']['initialization']!=bundle['preparation_cost_summary']['total']:
                    failures.append(dict(id=uid,reason='cross-stage evidence hash or shared cost changed'))
                replay_calls=[json.loads(line) for line in (cross/'calls.jsonl').read_text().splitlines()]
                if any(c['operation'] in ('caption_temporal_observe','sparse_visual_plan','sparse_visual_observe','sparse_visual_verify','temporal_review','clip_text','scene_scope') for c in replay_calls):
                    failures.append(dict(id=uid,reason='cross-stage replay acquired new source evidence'))
    if sparse_visual:
        probe=out/'verification/sparse-visual-probe/status.json'
        if (not probe.exists() or not read(probe).get('passed') or
            read(probe).get('cost_summary',{}).get('budget_violation') or
            len(read(probe).get('selected_frame_ids',[]))!=3):
            failures.append(dict(reason='targeted GPU visual branch probe has not passed'))
    value=dict(passed=not failures,failures=failures,accuracy_used_for_gate=False)
    write(out/'smoke/status.json',value);print(json.dumps(value),flush=True)
    return not failures


def work(out,arm,group,smoke=False):
    from script_core.backend import preflight
    from script_core.budget import ModelWorker
    for name,expected in read(out/'source_hashes.json').items():
        if sha(ROOT/name)!=expected:raise ValueError('Frozen source changed: '+name)
    if (out/'control_hashes.json').exists():
        for name,expected in read(out/'control_hashes.json').items():
            if sha(out/name)!=expected:raise ValueError('Frozen control changed: '+name)
    cfg,artifacts=preflight(load_config(out/'config.json'))
    target=out/'smoke' if smoke else out
    write(target/'setup'/f'{arm}-{group}-artifacts.json',artifacts)
    ids=read(out/'smoke/ids.json' if smoke else out/f'shards/{group}.json')
    backend=ModelWorker(cfg)
    try:
        for i,uid in enumerate(ids):
            profiles=list(ARMS) if smoke else (['summary_deep','temporal_cross'] if arm=='summary_pair' else [arm])
            for profile in profiles:
                r=execute(out,profile,uid,backend,cfg,smoke)
                print('TEMPORAL',profile,i+1,uid,'answer=',(r.get('answer') or {}).get('selected_option'),
                      'entities=',r.get('entity_count'),'error=',r.get('error'),flush=True)
                write(target/'progress'/f'{profile}-{group}.json',dict(completed=i+1,planned=len(ids),last_id=uid,error=r.get('error')))
        if smoke and cfg['query'].get('temporal_sparse_visual',False):
            from script_core.sparse_visual import probe
            from script_core.budget import BudgetLedger,Meter
            probe_target=out/'verification/sparse-visual-probe'
            ledger=BudgetLedger(60.,answer_reserve=0,calls_path=probe_target/'calls.jsonl')
            meter=Meter(backend,ledger,cfg,probe_target/'raw_calls')
            try:
                packet=checked_packet(out,ids[0],check_images=False)
                value=probe(packet,meter,probe_target)
                value['cost_summary']=ledger.summary()
                if ledger.budget_violation:raise RuntimeError('GPU visual branch probe exceeded its budget')
                write(probe_target/'status.json',value)
            finally:meter.close()
    finally:backend.close()
    if smoke and not gate(out):raise SystemExit(1)


def report(out):
    evaluation=read(out/'evaluation_only.json');summary={}
    for arm in ARMS:
        paths=list((out/'arms'/arm/'runs').glob('*/result.json'));rows=[read(p) for p in paths]
        correct=0
        for r in rows:
            truth=str(evaluation[r['question_id']]['truth'])
            correct+=str((r.get('answer') or {}).get('selected_option'))==truth
        summary[arm]=dict(completed=len(rows),planned=300,correct=correct,accuracy_fixed_denominator=correct/300,
            errors=sum(bool(r.get('error')) for r in rows),
            incomplete=sum(bool(r.get('incomplete')) or bool(r.get('inventory_incomplete')) for r in rows),
            unresolved_identity_comparisons=sum(r.get('unresolved_identity_comparisons',0) for r in rows),
            observation_page_recoveries=sum(r.get('observation_page_recoveries',0) for r in rows),
            alignment_repairs=sum(r.get('alignment_repairs',0) for r in rows),
            visual_check_questions=sum(bool(r.get('supplemental_visual',{}).get('triggered')) and not r.get('supplemental_visual',{}).get('reused_from_deep_search',False) for r in rows),
            shared_visual_check_questions=sum(bool(r.get('supplemental_visual',{}).get('triggered')) and r.get('supplemental_visual',{}).get('reused_from_deep_search',False) for r in rows),
            visual_frames_read=sum(r.get('pixel_frame_count',0) for r in rows),
            shared_visual_frames=sum(r.get('shared_pixel_frame_count',0) for r in rows),
            visual_processing_seconds=sum(r.get('supplemental_visual',{}).get('seconds',0) for r in rows if not r.get('supplemental_visual',{}).get('reused_from_deep_search',False)),
            shared_visual_processing_seconds=sum(r.get('supplemental_visual',{}).get('seconds',0) for r in rows if r.get('supplemental_visual',{}).get('reused_from_deep_search',False)),
            accepted_visual_observations=sum(len(r.get('supplemental_visual',{}).get('observations',[])) for r in rows),
            measured_processing_seconds=sum(r.get('total_measured_seconds',0) for r in rows))
    value=dict(complete=all(v['completed']==300 for v in summary.values()),arms=summary)
    write(out/'comparison.json',value);print(json.dumps(value,indent=2));return value


def submit(out):
    path=out/'jobs.json'
    jobs=read(path) if path.exists() else dict(smoke=None,formal=[],report=None)
    def save():write(path,jobs)
    def qsub(name,env,log,dependency=None,priority=0,report=False):
        cmd=['qsub','-N',name,'-p',str(priority),'-o',str(log),'-v',env]
        if dependency:cmd+=['-W','depend='+dependency]
        if report:cmd+=['-l','select=1:ncpus=1:mem=4GB','-l','walltime=00:10:00']
        elif name=='te-smoke':cmd+=['-l','walltime=04:00:00']
        cmd+=[str(out/'source/script/temporal_entity.pbs')]
        p=subprocess.run(cmd,capture_output=True,text=True)
        if p.returncode:raise RuntimeError(p.stderr)
        return p.stdout.strip()
    if not jobs['smoke']:
        jobs['smoke']=qsub('te-smoke',f'TEMPORAL_DIR={out},TEMPORAL_STAGE=smoke',out/'smoke.pbs.log',priority=1000)
        save()
    existing={(r['arm'],r['group']) for r in jobs['formal']}
    (out/'logs').mkdir(exist_ok=True)
    for group in range(read(out/'experiment.json')['shards']):
        for index,arm in enumerate(('summary_pair','temporal_local','cluster_qa')):
            if (arm,group) in existing:continue
            job=qsub(f'te-{arm[:5]}-{group:02d}',
                f'TEMPORAL_DIR={out},TEMPORAL_STAGE=formal,TEMPORAL_ARM={arm},TEMPORAL_GROUP={group}',
                out/'logs'/f'{arm}-{group}.pbs.log','afterok:'+jobs['smoke'],950-group*3-index)
            jobs['formal'].append(dict(arm=arm,group=group,job_id=job,dependency='afterok:'+jobs['smoke']))
            save();print('SUBMITTED',job,arm,group,flush=True)
    if not jobs['report']:
        jobs['report']=qsub('te-report',f'TEMPORAL_DIR={out},TEMPORAL_STAGE=report',out/'report.pbs.log',
            'afterany:'+':'.join(r['job_id'] for r in jobs['formal']),report=True)
        save()
    print(json.dumps(jobs),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','run','gate','report','submit'])
    parser.add_argument('--out',required=True);parser.add_argument('--reference')
    parser.add_argument('--arm',choices=list(ARMS)+['summary_pair'],default='summary_pair');parser.add_argument('--group',type=int,default=0)
    parser.add_argument('--smoke',action='store_true');parser.add_argument('--shards',type=int,default=12)
    parser.add_argument('--reuse-frozen-inputs',action='store_true')
    parser.add_argument('--sparse-visual',action='store_true',help='Allow one optional three-frame check only in the third arm')
    args=parser.parse_args();out=Path(args.out).resolve()
    if args.command=='prepare':prepare(out,Path(args.reference).resolve(),args.shards,args.reuse_frozen_inputs,args.sparse_visual)
    elif args.command=='run':work(out,args.arm,args.group,args.smoke)
    elif args.command=='gate':raise SystemExit(0 if gate(out) else 1)
    elif args.command=='submit':submit(out)
    else:report(out)
