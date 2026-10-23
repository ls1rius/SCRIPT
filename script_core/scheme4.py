"""Ordinary summary queries: scheme 3 by default, optional scheme-4 memory."""
from copy import deepcopy
import json
import math
from pathlib import Path
import time

from .budget import BudgetLedger,Meter
from .config import digest,base_config
from .state import write_json
from .temporal_entity import SETTINGS


def read(path):return json.loads(Path(path).read_text())


def query_fields(row,video_id,duration):
    allowed={'id','question_id','schema_version','revision','video_id','question','options','scope',
             'answer_mode','options_visible_to_retrieval','total_cap'}
    if set(row)-allowed:raise ValueError('Question input contains unsupported fields or evaluation annotations')
    uid=row.get('question_id',row.get('id'))
    if not isinstance(uid,str) or not uid or not isinstance(row.get('question'),str) or not row['question'].strip():
        raise ValueError('question_id and a nonempty question are required')
    if row['video_id']!=video_id:raise ValueError('Question video does not match the source')
    options=row.get('options')
    if not isinstance(options,list) or len(options)!=5 or any(not isinstance(x,str) for x in options):
        raise ValueError('Summary queries currently require five original answer choices')
    if row.get('answer_mode','choice')!='choice':raise ValueError('Summary queries currently support multiple-choice QA')
    if row.get('scope',[0,duration])!=[0,duration]:
        raise ValueError('Summary queries currently require the complete original one-second summary timeline')
    return dict(question_id=uid,video_id=video_id,question=row['question'],options=deepcopy(options))


def cached_frames(base_dir):
    """Recover cached-frame metadata without opening any image or video."""
    found={};calls=Path(base_dir)/'calls.jsonl'
    if not calls.exists():return []
    for line in calls.read_text().splitlines():
        call=json.loads(line)
        if call['operation']!='decode' or not call.get('output_ref'):continue
        path=Path(call['output_ref'])
        if not path.exists():continue
        for f in read(path).get('output',{}).get('data',[]):
            if f.get('crop') or not f.get('content_hash') or not Path(f['path']).is_file():continue
            found.setdefault(f['pts'],dict(frame_id=f['frame_id'],pts=f['pts'],path=f['path'],
                width=f['width'],height=f['height'],sha256=f['content_hash']))
    return [found[t] for t in sorted(found)]


def load_source(base_dir,row,cfg):
    """Accept frozen experiment packets or existing summary/feature M0 caches."""
    import torch
    from .video import file_hash,load_captions
    base_dir=Path(base_dir)
    packet_path=base_dir/'inputs'/f'{row["video_id"]}.json'
    if packet_path.exists():
        hashes=read(base_dir/'input_hashes.json')
        if file_hash(packet_path)!=hashes.get(str(packet_path.relative_to(base_dir))):raise ValueError('Frozen source packet changed')
        packet=read(packet_path)
        if file_hash(packet['feature_uri'])!=packet['feature_hash']:raise ValueError('Cached visual features changed')
        if digest(packet['captions'])!=packet['caption_records_hash']:raise ValueError('Original summaries changed')
        meta=packet.get('feature_metadata',{})
        if meta.get('model') and Path(meta['model']).resolve()!=Path(cfg['models']['visual_encoder']).resolve():
            raise ValueError('Cached feature encoder does not match query text encoder')
        features=torch.load(packet['feature_uri'],map_location='cpu',weights_only=True).tolist()
        # Only immutable source data is reused. Historical question/predictions
        # never select this query's answer or affect its entity extraction.
        packet['question']=query_fields(row,packet['question']['video_id'],len(packet['captions']))
    else:
        manifest=read(base_dir/'manifest.json')
        spans=load_captions(manifest)['data']
        captions=[dict(id=f'C{i}',interval=[s['start'],s['end']],text=s['text']) for i,s in enumerate(spans)]
        if [c['interval'] for c in captions]!=[[i,i+1] for i in range(len(captions))]:
            raise ValueError('Summary queries need complete original one-second summaries; overlapping M0 window digests cannot replace them')
        if len(captions)!=manifest['cutoff']:
            raise ValueError('Original summaries do not cover the complete legal video timeline')
        directory=Path(cfg['compatibility']['feature_directory'])
        if not directory.is_absolute():directory=Path(__file__).resolve().parents[1]/directory
        feature=directory/f'{manifest["video_id"]}.pt';sidecar=feature.with_suffix('.json')
        if feature.exists() and sidecar.exists():
            meta=read(sidecar)
            if (meta.get('fps')!=1 or meta.get('frame_indices')!=list(range(len(captions))) or
                Path(meta['model']).resolve()!=Path(cfg['models']['visual_encoder']).resolve()):
                raise ValueError('Cached visual feature model or timestamp grid mismatch')
            features=torch.load(feature,map_location='cpu',weights_only=True).tolist()
            feature_hash=file_hash(feature)
        else:
            meta=read(base_dir/'base_hash.json')
            if meta['base_config_hash']!=digest(base_config(cfg)):
                raise ValueError('M0 feature cache lacks matching encoder provenance; use matching cached features or frozen input packets')
            samples=read(base_dir/'feature_arrays/samples.json')
            if samples['pts']!=list(range(len(captions))):raise ValueError('M0 features do not cover the original summary timeline')
            features=samples['vectors'];feature_hash=digest(samples)
        packet=dict(question=query_fields(row,manifest['video_id'],len(captions)),captions=captions,
            caption_records_hash=digest(captions),feature_hash=feature_hash,frames=cached_frames(base_dir),
            source_caption_hash=manifest['caption_hash'],source_media_hash=manifest['media_hash'])
    if len(features)!=len(packet['captions']):raise ValueError('Features and original summaries are not on the same timestamp grid')
    return packet,features


def answer(packet,features,cfg,backend,output,resume=False,total_cap=None,source_access_seconds=0.):
    from .temporal_pipeline import directory,run,run_cross_replay
    pipeline=cfg['query']['pipeline']
    if pipeline not in ('scheme3','scheme4'):
        raise ValueError('Summary query entry requires query.pipeline=scheme3 or scheme4')
    scheme=4 if pipeline=='scheme4' else 3
    if cfg['query']['temporal_entity_source']!='summary':
        raise ValueError('Summary queries require summary-derived entities')
    if total_cap is not None and (type(total_cap) not in (int,float) or not math.isfinite(total_cap)
            or total_cap<=cfg['budget']['final_answer_reserve']):
        raise ValueError('Total cap must leave the final-answer reserve available')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    request=dict(scheme=scheme,packet_hash=digest(packet),config_hash=digest(cfg),total_cap=total_cap)
    marker=output/f'{pipeline}_request.json'
    other_marker=output/f'scheme{3 if scheme==4 else 4}_request.json'
    if other_marker.exists():
        raise ValueError('Existing output belongs to a different pipeline; choose a new output directory')
    if marker.exists():
        if not resume or read(marker)!=request:raise ValueError(f'Existing scheme-{scheme} output requires --resume with identical source, question and configuration')
        if (output/'result.json').exists() and not read(output/'result.json').get('error'):
            completed=read(output/'result.json')
            from .video import file_hash
            used=set(completed.get('supplemental_visual',{}).get('frame_ids',[]))
            sources={f['frame_id']:f for f in packet.get('frames',[])}
            if not used<=sources.keys():raise ValueError('Cached answer lost its sampled visual sources')
            for fid in used:
                if file_hash(sources[fid]['path'])!=sources[fid]['sha256']:raise ValueError('Cached sampled frame changed')
            return completed
    else:
        if (output/'result.json').exists():raise ValueError('Existing output belongs to a different pipeline; choose a new output directory')
        write_json(marker,request)
    write_json(output/'query.json',dict(id=packet['question']['question_id'],video_id=packet['question']['video_id'],
        question=packet['question']['question'],options=packet['question']['options'],scope=[0,len(packet['captions'])]))
    settings=dict(SETTINGS,online_seconds=cfg['budget']['online_cap'])
    directory_path=output/'directory.json'
    if directory_path.exists():d=read(directory_path)
    else:
        if (output/'directory_calls/calls.jsonl').exists():
            raise ValueError('Interrupted directory build needs a new output directory; do not reset a partially consumed budget')
        cap=min(SETTINGS['directory_seconds'],total_cap-cfg['budget']['final_answer_reserve']) if total_cap is not None else SETTINGS['directory_seconds']
        ledger=BudgetLedger(cap,answer_reserve=0,calls_path=output/'directory_calls/calls.jsonl')
        meter=Meter(backend,ledger,cfg,output/'directory_calls/raw_calls')
        try:
            if source_access_seconds:
                ledger.record('source_cache_access',{},ledger.allowance(cap),time.monotonic()-source_access_seconds)
            d=directory(packet,features,meter,settings)
            d.update(feature_hash=packet['feature_hash'],cost_summary=ledger.summary())
            write_json(directory_path,d)
        finally:meter.close()
    if total_cap is not None:settings['online_seconds']=min(settings['online_seconds'],total_cap-d['cost_summary']['total'])
    preparation=output/'summary_search';bundle_path=preparation/'evidence_bundle.json'
    if scheme==3:
        if (preparation/'result.json').exists():
            result=read(preparation/'result.json')
        else:
            if (preparation/'calls.jsonl').exists():
                raise ValueError('Interrupted summary search needs a new output directory; its existing budget is retained')
            result=run(packet,d,packet.get('frames',[]),backend,cfg,preparation,'summary_deep',settings)
        if result.get('error'):raise RuntimeError('Scheme-3 summary query failed: '+str(result['error']))
        if not result.get('answer'):raise ValueError('Scheme-3 summary result lacks its final answer; choose a new output directory')
        bundle=read(bundle_path)
    else:
        if not bundle_path.exists():
            if (preparation/'calls.jsonl').exists():
                raise ValueError('Interrupted summary search needs a new output directory; its existing budget is retained')
            prepared=run(packet,d,packet.get('frames',[]),backend,cfg,preparation,'summary_deep',settings,evidence_only=True)
            if prepared['error']:raise RuntimeError('Scheme-4 summary preparation failed: '+str(prepared['error']))
        bundle=read(bundle_path)
        final=output/'cross_stage'
        if (final/'result.json').exists():result=read(final/'result.json')
        else:
            if (final/'calls.jsonl').exists():raise ValueError('Interrupted cross-stage answering needs a new output directory')
            result=run_cross_replay(packet,bundle,backend,cfg,final,settings)
    result=deepcopy(result)
    result.update(scheme=scheme,pipeline=pipeline,config_hash=digest(cfg),base_hash=digest(packet),
        query=read(output/'query.json'),run_config=deepcopy(cfg),directory_cost_summary=d['cost_summary'],
        source_preprocessing_reused=True,experiment_mode='live',source_evidence_only=scheme==4)
    # Charge the directory once; each scheme generates exactly one final QA.
    cost=result['cost_summary'];directory_cost=d['cost_summary']['total']
    for key in ('initialization','total','budgeted_total','total_cap'):cost[key]+=directory_cost
    cost['budget_violation']=cost['budget_violation'] or d['cost_summary']['budget_violation']
    enough=bool(bundle['reviews'] and bundle['reviews'][-1]['sufficient'] and
                bundle['reviews'][-1]['coverage_check']['met'] and not result['incomplete'])
    reason='scheme4_bounded_summary_search_and_cross_stage_memory' if scheme==4 else 'scheme3_bounded_summary_search'
    result['stop_decision']=dict(reason=reason,
        evidence_status='sufficient' if enough else 'insufficient')
    write_json(output/'result.json',result)
    return result


def run_queries(args,questions,cfg,backend):
    from script_qa import safe_directory_id
    pipeline=cfg['query']['pipeline'];scheme=4 if pipeline=='scheme4' else 3
    root=Path(args.base);index=read(root/'videos.json') if (root/'videos.json').exists() else None
    rows=[]
    for row in questions:
        uid=row.get('question_id',row.get('id'));output=Path(args.out)/safe_directory_id(uid)
        path=output/'result.json'
        try:
            source=Path(index[row['video_id']]) if index else root
            started=time.monotonic();packet,features=load_source(source,row,cfg)
            access=time.monotonic()-started
            write_json(output/'run_config.json',cfg)
            result=answer(packet,features,cfg,backend,output,args.resume,row.get('total_cap'),access)
            failure=Path(args.out)/'query_failures'/f'{safe_directory_id(uid)}.json'
            failure.unlink(missing_ok=True)
        except Exception as exc:
            # All actual leaf calls are retained across the three stages. The
            # shared source preparation appears in these logs only once.
            calls=[]
            for name in ('directory_calls','summary_search','cross_stage'):
                p=output/name/'calls.jsonl'
                if p.exists():calls.extend(json.loads(line) for line in p.read_text().splitlines() if line.strip())
            consumed=sum(c['actual_cost'] for c in calls)
            result=dict(scheme=scheme,pipeline=pipeline,question_id=uid,answer=None,stop_decision=None,
                error=dict(code=f'{pipeline}_query_failed',message=f'{type(exc).__name__}: {exc}'),
                cost_summary=dict(initialization=0.,online=consumed,total=consumed),run_config=cfg,
                query=dict(id=uid,video_id=row.get('video_id'),scope=row.get('scope')))
            # Preserve every partial stage/call log; never overwrite existing results.
            path=Path(args.out)/'query_failures'/f'{safe_directory_id(uid)}.json'
            write_json(path,result)
        rows.append(dict(question_id=uid,result_path=str(path),error=result['error'],cost=result['cost_summary']))
        print('RESULT',path,flush=True)
    write_json(Path(args.out)/'sequence_cost.json',dict(protocol='query',pipeline=pipeline,
        cumulative_total=sum(r['cost']['total'] for r in rows),questions=rows))
    return int(any(r['error'] for r in rows))
