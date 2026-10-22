"""Executable visual scene -> temporal entities -> original VideoTree QA pipeline."""
from copy import deepcopy
from pathlib import Path
import json
import re
import time
from .temporal_entity import (SETTINGS, partition, validate_directory, new_network, observe_page,compact_profiles,
                             align_page, entity_context, canonical)
from .budget import BudgetLedger, Meter, ResourceExhausted, ToolFailure, exploration_exhausted
from .config import digest
from .state import write_json
from .accounting import metered_cpu


@metered_cpu('temporal_directory')
def directory(packet, features, meter, settings=SETTINGS):
    d=partition(packet['question']['video_id'],features,packet['captions'],settings)
    for scene in d['scenes']:
        a,b=scene['interval']
        report,_=meter.json('temporal_summary',dict(captions=packet['captions'][a:b]),('description',),build=True)
        scene.update(description=report['description'],description_status='caption_derived_directory_hint',
                     summary_source_caption_ids=scene['caption_ids'])
    return d


def profiles(net, ids):
    result=[]
    for eid in dict.fromkeys(canonical(net,eid) for eid in ids):
        e=net['entities'][eid]
        states=[net['states'][s] for s in e['state_ids']]
        result.append(dict(id=eid,label=e['label'],scene_ids=e['scene_ids'],
            attributes=e['attributes'][-12:],state_ids=e['state_ids'],
            last_state=states[-1] if states else None,
            context_selection=dict(attributes_total=len(e['attributes']),attributes_shown=min(12,len(e['attributes'])))))
    return compact_profiles(result)


@metered_cpu('temporal_scene_expansion')
def open_scene(net, scene, all_frames, meter, target, settings=SETTINGS):
    a,b=scene['interval']; scene_id=scene['id']; known=[]
    sources=all_frames[a:b]
    for start in range(0,len(sources),settings['frame_batch']):
        batch=sources[max(0,start-1):start+settings['frame_batch']]
        # Boundary context is shown, but its already-ingested observations are not reset.
        # Fresh images plus one previous image allow visible operations across batches.
        reported=[]; exhausted=False
        for page in range(settings['max_pages']):
            visible=profiles(net,known)
            # One previous state and compact attributes per entity; never silently truncate inventory.
            inp=dict(frames=batch,entities=visible,already_reported=reported,
                     page_size=settings['entities_per_page'],scene_interval=scene['interval'],
                     new_frame_ids=[f['frame_id'] for f in sources[start:start+settings['frame_batch']]],
                     page=page,attribute_policy='all observable attributes; unknown omitted')
            ids,report,call=observe_page(net,inp,meter,scene_id,f'{scene_id}:{start}:{page}',known)
            known=list(dict.fromkeys(known+ids))
            reported.extend(profiles(net,ids))
            net['coverage'].append(dict(scene_id=scene_id,batch_start=start,page=page,
                frame_ids=[f['frame_id'] for f in batch],more_entities=report['more_entities'],
                unresolved=report['unresolved'],call_id=call['call_id'],
                status='sampled_inventory_not_continuous_visibility'))
            write_json(Path(target)/'network.json',net)
            if not report['more_entities']:
                exhausted=True
                break
            if not report['entities']:
                raise ValueError('Inventory pagination made no progress')
        if not exhausted:
            net['diagnostics'].append(dict(scene_id=scene_id,batch_start=start,code='inventory_page_limit',complete=False))
    return list(dict.fromkeys(canonical(net,k) for k in known))


@metered_cpu('temporal_identity_transfer')
def cross_align(net,new_ids,previous_ids,all_frames,meter,target):
    lookup={f['frame_id']:f for f in all_frames}
    for uid in new_ids:
        uid=canonical(net,uid)
        if uid not in net['entities']:
            continue
        left=net['entities'][uid]
        def words(e):
            return set(re.findall(r'\w+',' '.join([e['label']]+[a['value'] for a in e['attributes']]).lower()))
        candidates=list(dict.fromkeys(canonical(net,x) for x in previous_ids if canonical(net,x)!=uid))
        candidates=[cid for cid in candidates if not set(net['entities'][cid]['scene_ids']) & set(left['scene_ids'])]
        candidates.sort(key=lambda cid:(-len(words(left)&words(net['entities'][cid])),cid))
        for cid in candidates[:2]:
            if canonical(net,cid)==canonical(net,uid):
                continue
            cid=canonical(net,cid);uid=canonical(net,uid)
            l,r=net['entities'][uid],net['entities'][cid]
            sid,tid=min(((s,t) for s in l['state_ids'] for t in r['state_ids']),
                        key=lambda p:abs(net['states'][p[0]]['first_observed_at']-net['states'][p[1]]['last_observed_at']))
            refs=list(dict.fromkeys(net['states'][sid]['source_frame_ids'][:1]+net['states'][sid]['source_frame_ids'][-1:]+
                                    net['states'][tid]['source_frame_ids'][:1]+net['states'][tid]['source_frame_ids'][-1:]))
            frames=[lookup[f] for f in refs]
            inp=dict(pairs=[[uid,cid]],frames=frames,left_entities=profiles(net,[uid]),
                right_entities=profiles(net,[cid]),states=[net['states'][sid],net['states'][tid]],
                relations=[r for r in net['relations'] if r['subject'] in (uid,cid) or r['object'] in (uid,cid)],
                compare_state_pair=dict(left_state=sid,right_state=tid))
            report,_=align_page(net,inp,meter)
            write_json(Path(target)/'network.json',net)
            if any(x['decision']=='same' for x in report['matches']):
                break


def compact_context(context):
    # Storage IDs are already short; raw evidence stays in network.json.
    return dict(entities=[dict(id=e['id'],label=e['label'],attributes=[dict(field=a['field'],value=a['value'],
                    observed_at=a['observed_at']) for a in e['attributes']]) for e in compact_profiles(context['entities'])],
        states=[{k:s[k] for k in ('id','entity_id','description','scene_id','first_observed_at','last_observed_at',
                                  'valid_from_bounds','valid_to_bounds','relation_ids','logical_state_id','fragment_kind')} for s in context['states']],
        transitions=context['transitions'],relations=context['relations'],lineage=context['lineage'],
        context_selection=context['context_selection'],
        evidence_policy='Sampled visual-model assertions. Unknown time/causes stay unknown. Unobserved gaps are not continuity.')


@metered_cpu('temporal_qa_context_selection')
def fit_qa_context(query,captions,selected,context,meter,use_memory=True,final=False,visual_observations=()):
    """Check the actual QA template; omit lowest-ranked whole entities explicitly.

    Captions, question and option order are never truncated. The complete source
    network stays on disk; this changes only the bounded final retrieval view.
    """
    from .compatibility import qa_inputs
    view=deepcopy(context);omitted=[]
    while True:
        inp=qa_inputs(query,captions,selected)
        if use_memory:
            provenance=('original summary/caption assertions; identities are text inferences; not pixel verified'
                        if view.get('source_type')=='caption' else 'sampled visual observations; uncertain bounds/causes explicit')
            inp['prompts'][-1]+='\n\nTemporal entity memory ('+provenance+'):\n'+json.dumps(view,ensure_ascii=False,separators=(',',':'))
        if visual_observations:
            inp['prompts'][-1]+='\n\nSupplemental sampled visual observations (independently checked by the visual model; valid only for the cited sample, not unseen events or whole-video coverage):\n'+json.dumps(visual_observations,ensure_ascii=False,separators=(',',':'))
        try:
            # Context checking is part of final preparation. Exploration may
            # already have spent its allowance, while the QA reserve is intact.
            checked,_=meter.call('temporal_qa_check',inp,final=True)
            return inp,view,checked['input_tokens']
        except ToolFailure as exc:
            if not use_memory or 'Original QA plus retrieved entity context exceeds input allowance' not in str(exc):
                raise
            if not view['entities']:
                raise
        omitted.append(view['entities'].pop()['id'])
        ids={e['id'] for e in view['entities']}
        view['states']=[s for s in view['states'] if s['entity_id'] in ids]
        sids={s['id'] for s in view['states']}
        view['transitions']=[t for t in view['transitions'] if t['before'] in sids and t['after'] in sids]
        view['relations']=[r for r in view['relations'] if r['subject'] in ids and r['object'] in ids]
        rids={r['id'] for r in view['relations']}
        for state in view['states']:
            state['relation_ids']=[r for r in state['relation_ids'] if r in rids]
        view['lineage']=[r for r in view['lineage'] if r['child'] in ids and r['parent'] in ids]
        if 'identities' in view:
            view['identities']=[r for r in view['identities'] if r['left_entity'] in ids and r['right_entity'] in ids]
        view['context_selection']=dict(view['context_selection'],selected_entities=len(ids),selected_states=len(sids),
            token_budget_omitted_entity_ids=list(omitted),
            token_budget_policy='Remove lowest-ranked complete entity records and dependent edges; full source network retained.')


def run(packet,d,frames,backend,cfg,target,arm,settings=SETTINGS,evidence_only=False):
    from .compatibility import qa_inputs
    from .scene_memory import navigation_coverage
    target=Path(target);target.mkdir(parents=True,exist_ok=True)
    ledger=BudgetLedger(settings['online_seconds'],answer_reserve=cfg['budget']['final_answer_reserve'],calls_path=target/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,target/'raw_calls')
    net=new_network();opened=[];reviews=[];incomplete=[];answer=None;scope=None
    caption_mode=cfg['query'].get('temporal_entity_source','summary')=='summary'
    expanding=arm in ('summary_deep','temporal_cross')
    sparse_enabled=caption_mode and expanding and cfg['query'].get('temporal_sparse_visual',False)
    supplemental=dict(enabled=sparse_enabled,triggered=False,observations=[],frame_ids=[],selected_frame_ids=[],seconds=0.,rounds=[])
    if caption_mode:
        from . import caption_temporal
        net.update(source_type='caption',evidence_status='caption_asserted',physical_identity_confirmed=False)
    def memory_context():
        context=entity_context(net,query)
        return caption_temporal.compact_context(context,net) if caption_mode else compact_context(context)
    query=dict(packet['question'],id=packet['question']['question_id'])
    result=dict(arm=arm,question_id=query['id'],method='contiguous_summary_temporal_entities' if caption_mode else 'contiguous_visual_temporal_entities',
                entity_source='summary' if caption_mode else 'images',answer=None,error=None)
    try:
        embedded,_=meter.call('clip_text',dict(texts=[query['question']]+query['options']),build=True)
        import numpy as np
        q=np.asarray(embedded['vectors']);centers=np.asarray([s['center'] for s in d['scenes']])
        if q.shape[1]!=centers.shape[1]:
            raise ValueError('Visual centers and text encoder are not in the same feature space')
        # Question receives half the weight, the five options share the other half; no label enters ranking.
        scores=.5*(centers@q[0])+.5*(centers@q[1:].T).mean(axis=1)
        ranked=sorted(range(len(scores)),key=lambda i:(-scores[i],i))
        result['retrieval']=dict(scores=scores.tolist(),entry_scene=d['scenes'][ranked[0]]['id'],
            chunk_counts=embedded['chunk_counts'],content_token_counts=embedded['content_token_counts'])
        scope,_=meter.json('scene_scope',dict(question=query['question'],options=query['options']),('scope','reason'))
        current=d['scenes'][ranked[0]]['id']
        maximum=settings['max_scene_visits'] if expanding else 1
        for visit in range(maximum):
            scene=next(s for s in d['scenes'] if s['id']==current)
            before=list(net['entities'])
            opened.append(current)
            if arm!='cluster_qa':
                try:
                    ids=(caption_temporal.open_scene(net,scene,d['captions'],meter,target,settings) if caption_mode
                         else open_scene(net,scene,frames,meter,target,settings))
                    if arm=='temporal_cross' and before:
                        if caption_mode:caption_temporal.cross_align(net,ids,before,d['captions'],meter,target)
                        else:cross_align(net,ids,before,frames,meter,target)
                except (ResourceExhausted,ToolFailure) as exc:
                    if not exploration_exhausted(exc):
                        raise
                    incomplete.append(dict(scene_id=current,stage='entity_network',reason=str(exc)))
                    break
            if not expanding:
                break
            context=memory_context()
            frontier=[p for p in d['pointers'] if (p['source'] in opened)!=(p['target'] in opened)]
            allowed=list(dict.fromkeys(p['target'] if p['source'] in opened else p['source'] for p in frontier))
            coverage=navigation_coverage(scope,{s['id']:s for s in d['scenes']},opened,[0,len(d['captions'])])
            try:
                report,_=meter.json('temporal_review',dict(question=query['question'],options=query['options'],
                    **context,frontier=frontier,allowed_scene_ids=allowed,
                    directory=[dict(id=s['id'],interval=s['interval'],description=s['description']) for s in d['scenes']],
                    opened_scenes=opened,temporal_coverage=coverage),('sufficient','reason','gap','next_scene','entity_ids'))
            except (ResourceExhausted,ToolFailure) as exc:
                if not exploration_exhausted(exc):
                    raise
                incomplete.append(dict(scene_id=current,stage='navigation_review',reason=str(exc)))
                break
            report['coverage_check']=coverage;reviews.append(report)
            if report['sufficient'] and coverage['met']:
                break
            if not allowed:
                break
            nxt=report['next_scene']
            if nxt=='stop' or nxt not in allowed:
                # Required broad temporal coverage overrides a local-sufficiency guess.
                missing=[t for t in (len(d['captions'])/6,len(d['captions'])/2,5*len(d['captions'])/6)
                         if not any(next(s for s in d['scenes'] if s['id']==sid)['interval'][0]<=t<
                                    next(s for s in d['scenes'] if s['id']==sid)['interval'][1] for sid in opened)]
                nxt=min(allowed,key=lambda sid:(min((abs(sum(next(s for s in d['scenes'] if s['id']==sid)['interval'])/2-t) for t in missing),default=0),
                                                 -float(scores[next(i for i,s in enumerate(d['scenes']) if s['id']==sid)])))
            current=nxt
        selected=sorted(i for sid in opened for i in next(s for s in d['scenes'] if s['id']==sid)['chronological_order'])
        if sparse_enabled:
            from .sparse_visual import supplement
            supplemental=supplement(query,d,frames,opened,reviews,scope,meter,target)
            write_json(target/'sparse_visual.json',supplemental)
        context=memory_context()
        if arm=='summary_deep':
            # Export before QA. This contains evidence and costs, never the
            # deep arm's prediction, rationale, confidence or evaluation labels.
            write_json(target/'evidence_bundle.json',dict(schema_version=1,packet_hash=digest(packet),
                captions_hash=digest(d['captions']),directory=deepcopy(d),network=deepcopy(net),
                opened_scenes=list(opened),selected_seconds=selected,reviews=deepcopy(reviews),
                retrieval=deepcopy(result['retrieval']),scope_requirement=scope,
                supplemental_visual=deepcopy(supplemental),incomplete=deepcopy(incomplete),
                preparation_cost_summary=ledger.summary()))
            if evidence_only:
                result['evidence_only']=True
                return result
        inp,context,tokens=fit_qa_context(query,d['captions'],selected,context,meter,arm!='cluster_qa',final=True,
                                        visual_observations=supplemental['observations'])
        result['qa_input_tokens']=tokens
        write_json(target/'qa_context.json',dict(context=context,input_tokens=tokens,selected_seconds=selected))
        # Original QA prompt, option order and generation settings are preserved. Supplemental evidence is explicit.
        out,_=meter.call('videotree_qa',inp,final=True)
        answer=out['data'];result['answer']=answer
        result['selected_seconds']=selected
        result['network_retrieval']=context['context_selection']
    except Exception as exc:
        result['error']=dict(code='temporal_pipeline_failed',type=type(exc).__name__,message=str(exc))
    finally:
        result.update(opened_scenes=opened,reviews=reviews,scope_requirement=scope,incomplete=incomplete,
            cost_summary=ledger.summary(),entity_count=len(net['entities']),state_count=len(net['states']),
            logical_state_count=len({s['logical_state_id'] for s in net['states'].values() if s['logical_state_id']}),
            transition_count=len(net['transitions']),relation_count=len(net['relations']),
            identity_comparisons=len(net['identities']),pixel_frame_count=len(supplemental['frame_ids']) if caption_mode else len({r for c in net['coverage'] for r in c['frame_ids']}),
            supplemental_visual=supplemental,
            caption_source_count=len(net.get('source_captions',{})) if caption_mode else 0,
            unresolved_identity_comparisons=sum(r['decision']=='unknown' for r in net['identities']),
            observation_page_recoveries=len(net.get('observation_page_recoveries',[])),
            alignment_repairs=len(net.get('alignment_repairs',[])),
            inventory_incomplete=net['diagnostics'])
        write_json(target/'network.json',net);write_json(target/'result.json',result);meter.close()
    return result


def run_cross_replay(packet,bundle,backend,cfg,target,settings=SETTINGS):
    """Isolate cross-scene memory using the deep arm's identical source evidence."""
    from . import caption_temporal
    if bundle.get('schema_version')!=1 or bundle['packet_hash']!=digest(packet):
        raise ValueError('Paired evidence does not match this frozen input packet')
    d=deepcopy(bundle['directory']);validate_directory(d)
    if bundle['captions_hash']!=digest(packet['captions']) or d['captions']!=packet['captions']:
        raise ValueError('Paired original summaries changed')
    forbidden={'answer','prediction','selected_option','truth','historical_prediction','evaluation'}
    if forbidden&bundle.keys():raise ValueError('Answer/evaluation information is forbidden in paired evidence')
    net=deepcopy(bundle['network']);caption_temporal.validate_network(net,d['captions'])
    if net['identities'] or net['aliases'] or net['lineage']:
        raise ValueError('Deep-search control must have scene-local entities without cross-scene links')
    if any(len(e['scene_ids'])!=1 for e in net['entities'].values()):
        raise ValueError('Deep-search control already merged entities across scenes')
    opened=list(bundle['opened_scenes']);selected=list(bundle['selected_seconds'])
    expected=sorted(i for s in d['scenes'] if s['id'] in opened for i in s['chronological_order'])
    if len(set(opened))!=len(opened) or selected!=expected:raise ValueError('Paired summary selection changed')
    if bundle['incomplete']:raise ValueError('Paired source extraction is incomplete')
    target=Path(target);target.mkdir(parents=True,exist_ok=True)
    # Reused search/extraction/visual work is still charged at its measured cost
    # inside the same online cap. Cache replay does not grant a free budget.
    shared=bundle['preparation_cost_summary']['total']
    ledger=BudgetLedger(settings['online_seconds'],initialization_cost=shared,
        answer_reserve=cfg['budget']['final_answer_reserve'],calls_path=target/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,target/'raw_calls')
    supplemental=deepcopy(bundle['supplemental_visual'])
    supplemental['reused_from_deep_search']=True
    query=dict(packet['question'],id=packet['question']['question_id'])
    result=dict(arm='temporal_cross',question_id=query['id'],method='summary_cross_stage_matched_evidence',
        entity_source='summary',answer=None,error=None,paired_evidence_hash=digest(bundle),
        navigation_policy='replay_summary_deep_sources',retrieval=deepcopy(bundle['retrieval']),
        shared_preparation_seconds=shared,source_arm='summary_deep')
    incomplete=[]
    try:
        processed=set()
        for sid in opened:
            new=[eid for eid,e in net['entities'].items() if sid in e['scene_ids'] and not processed.intersection(e['scene_ids'])]
            previous=[eid for eid,e in net['entities'].items() if processed.intersection(e['scene_ids'])]
            if new and previous:
                try:caption_temporal.cross_align(net,new,previous,d['captions'],meter,target)
                except (ResourceExhausted,ToolFailure) as exc:
                    if not exploration_exhausted(exc):raise
                    incomplete.append(dict(scene_id=sid,stage='cross_stage_identity',reason=str(exc)));break
            processed.add(sid)
        context=caption_temporal.compact_context(entity_context(net,query),net)
        inp,context,tokens=fit_qa_context(query,d['captions'],selected,context,meter,final=True,
            visual_observations=supplemental['observations'])
        result['qa_input_tokens']=tokens
        write_json(target/'qa_context.json',dict(context=context,input_tokens=tokens,selected_seconds=selected,
            supplemental_visual_observations=supplemental['observations']))
        out,_=meter.call('videotree_qa',inp,final=True)
        result.update(answer=out['data'],network_retrieval=context['context_selection'])
    except Exception as exc:
        result['error']=dict(code='cross_stage_replay_failed',type=type(exc).__name__,message=str(exc))
    finally:
        result.update(opened_scenes=opened,selected_seconds=selected,reviews=deepcopy(bundle['reviews']),
            scope_requirement=bundle['scope_requirement'],incomplete=incomplete,cost_summary=ledger.summary(),
            entity_count=len(net['entities']),state_count=len(net['states']),
            logical_state_count=len({s['logical_state_id'] for s in net['states'].values()}),
            transition_count=len(net['transitions']),relation_count=len(net['relations']),
            identity_comparisons=len(net['identities']),unresolved_identity_comparisons=sum(r['decision']=='unknown' for r in net['identities']),
            observation_page_recoveries=0,alignment_repairs=0,inventory_incomplete=net['diagnostics'],
            caption_source_count=len(net.get('source_captions',{})),supplemental_visual=supplemental,
            pixel_frame_count=0,shared_pixel_frame_count=len(supplemental['frame_ids']))
        write_json(target/'network.json',net);write_json(target/'result.json',result);meter.close()
    return result
