"""Shared VideoTree evidence and budget; paired flat/script memory QA."""
from copy import deepcopy
from pathlib import Path
import json
import time
from .accounting import metered_cpu
from .budget import BudgetLedger,Meter,ResourceExhausted,ToolFailure,exploration_exhausted
from .compatibility import qa_inputs
from .state import write_json
from .temporal_entity import new_network,observe_page,align_page,canonical,compact_profiles
from .videotree_aligned import directory,visit_order
from .config import digest


def profiles(net,ids):
    result=[]
    for eid in dict.fromkeys(canonical(net,e) for e in ids):
        entity=net['entities'][eid];attributes={}
        for a in entity['attributes']:
            key=(a['field'],a['value'])
            entry=attributes.setdefault(key,dict(field=a['field'],value=a['value'],source_frame_ids=[],observed_at=[]))
            entry['source_frame_ids']=list(dict.fromkeys(entry['source_frame_ids']+a['source_frame_ids']))
            entry['observed_at']=sorted(set(entry['observed_at']+a['observed_at']))
        result.append(dict(id=eid,label=entity['label'],scene_ids=entity['scene_ids'],
            attributes=list(attributes.values()),state_ids=entity['state_ids'],
            last_state=net['states'][entity['state_ids'][-1]] if entity['state_ids'] else None))
    return compact_profiles(result)


def frontend(packet,meter,continuous):
    query=dict(packet['question'],id=packet['question']['question_id'])
    inp=dict(query_id=query['id'],query={k:query[k] for k in ('id','question','options')},
             captions=[c['text'] for c in packet['captions']],duration=len(packet['captions']),feature_uri=packet['feature_uri'])
    raw,_=meter.call('videotree_contiguous_width' if continuous else 'videotree_width',inp,build=True)
    width=raw['data']
    raw,_=meter.call('videotree_depth',dict(query_id=query['id'],feature_uri=packet['feature_uri'],
        width=width['width'],relevance={'cluster_relevance':width['relevance']['cluster_relevance']}),build=True)
    selected=sorted(set(raw['data']['selected_seconds']))
    if not selected or not set(selected)<=set(range(len(packet['captions']))):raise ValueError('Invalid original selected seconds')
    return query,width,selected


def state_frames(net,eid,sid,lookup):
    state=net['states'][sid]
    refs=list(dict.fromkeys(state['source_frame_ids'][:1]+state['source_frame_ids'][-1:]))
    return [lookup[r] for r in refs]


@metered_cpu('aligned_identity_transfer')
def align_all(net,new_ids,prior_ids,lookup,meter,target,page_size):
    """Every compatible prior entity is eligible; no lexical top-two cutoff."""
    for original in new_ids:
        uid=canonical(net,original)
        candidates=list(dict.fromkeys(canonical(net,x) for x in prior_ids if canonical(net,x)!=uid))
        candidates=[x for x in candidates if not set(net['entities'][x]['scene_ids']) & set(net['entities'][uid]['scene_ids'])]
        for start in range(0,len(candidates),page_size):
            uid=canonical(net,uid);current=[]
            for candidate in candidates[start:start+page_size]:
                cid=canonical(net,candidate)
                if cid==uid or set(net['entities'][cid]['scene_ids']) & set(net['entities'][uid]['scene_ids']):continue
                current.append(cid)
            if not current:continue
            pairs=[];frame_rows=[];state_rows=[]
            for cid in current:
                ls,rs=min(((s,t) for s in net['entities'][uid]['state_ids'] for t in net['entities'][cid]['state_ids']),
                    key=lambda p:abs(net['states'][p[0]]['first_observed_at']-net['states'][p[1]]['last_observed_at']))
                pairs.append(dict(left=uid,right=cid,left_state=ls,right_state=rs))
                state_rows.extend([net['states'][ls],net['states'][rs]])
                frame_rows.extend(state_frames(net,uid,ls,lookup)+state_frames(net,cid,rs,lookup))
            frame_rows=list({f['frame_id']:f for f in frame_rows}.values())
            inp=dict(pairs=[[p['left'],p['right']] for p in pairs],compare_state_pairs=pairs,
                frames=frame_rows,left_entities=profiles(net,[uid]),right_entities=profiles(net,current),
                states=list({s['id']:s for s in state_rows}.values()),
                relations=[r for r in net['relations'] if r['subject'] in [uid]+current or r['object'] in [uid]+current])
            report,_=align_page(net,inp,meter,build=True)
            net.setdefault('identity_batches',[]).append(dict(candidate_pairs=inp['pairs'],
                source_frame_ids=[f['frame_id'] for f in frame_rows],matches=len(report['matches'])))
            write_json(Path(target)/'network.json',net)
            if any(r['decision']=='same' for r in report['matches']):break


@metered_cpu('aligned_selected_entity_extraction')
def extract_scene(net,scene,selected_frames,meter,target,page_size,frame_batch):
    known=[];lookup={f['frame_id']:f for f in selected_frames}
    for start in range(0,len(selected_frames),frame_batch):
        batch=selected_frames[max(0,start-1):start+frame_batch]
        new_frame_ids=[f['frame_id'] for f in selected_frames[start:start+frame_batch]]
        reported=[];page=0
        while True:
            inp=dict(frames=batch,entities=profiles(net,known),already_reported=reported,
                page_size=page_size,scene_interval=scene['interval'],new_frame_ids=new_frame_ids,page=page,
                observation_scope='Only VideoTree-selected representative frames; unseen intervals remain unknown.')
            ids,report,call=observe_page(net,inp,meter,scene['id'],f'{scene["id"]}:{start}:{page}',known,build=True)
            known=list(dict.fromkeys(known+ids));reported.extend(profiles(net,ids))
            net['coverage'].append(dict(scene_id=scene['id'],frame_ids=[f['frame_id'] for f in batch],
                new_frame_ids=new_frame_ids,page=page,more_entities=report['more_entities'],
                unresolved=report['unresolved'],call_id=call['call_id'],status='VideoTree_selected_sampled_visual_assertions'))
            write_json(Path(target)/'network.json',net)
            if not report['more_entities']:break
            page+=1
    return list(dict.fromkeys(canonical(net,k) for k in known))


@metered_cpu('aligned_memory_construction')
def construct(packet,meter,target,cfg):
    import torch
    query,width,selected=frontend(packet,meter,True)
    features=torch.load(packet['feature_uri'],map_location='cpu',weights_only=True)
    d=directory(packet,features,width);write_json(Path(target)/'directory.json',d)
    net=new_network();order=visit_order(d,selected);opened=[];complete=[];interrupted=None
    lookup={f['frame_id']:f for f in packet['frames']}
    # These batch sizes concern the new extraction API only; VideoTree has no entity extractor.
    frame_batch=cfg['initialization']['caption_frames_per_window']
    page_size=frame_batch
    try:
        for s in d['scenes']:
            a,b=s['interval']
            summary,_=meter.json('temporal_summary',dict(captions=packet['captions'][a:b]),('description',),build=True)
            s.update(description=summary['description'],description_status='whole_scene_caption_hint')
        write_json(Path(target)/'directory.json',d)
        for sid in order:
            opened.append(sid);scene=next(s for s in d['scenes'] if s['id']==sid)
            previous=list(net['entities'])
            frames=[packet['frames'][i] for i in selected if i in scene['chronological_order']]
            ids=extract_scene(net,scene,frames,meter,target,page_size,frame_batch)
            if previous:align_all(net,ids,previous,lookup,meter,target,page_size)
            complete.append(sid)
    except (ResourceExhausted,ToolFailure) as exc:
        if not exploration_exhausted(exc):
            raise
        interrupted=dict(code='shared_600_second_budget',stage='entity_construction',reason=str(exc))
    finally:
        write_json(Path(target)/'directory.json',d);write_json(Path(target)/'network.json',net)
    allowed={packet['frames'][i]['frame_id'] for i in selected}
    observed={r for c in net['coverage'] for r in c['frame_ids']}
    if not observed<=allowed:raise ValueError('Memory read an unselected image')
    return dict(query=query,width=width,selected_seconds=selected,directory=d,network=net,
        planned_scene_order=order,opened_scenes=opened,completed_scenes=complete,
        incomplete=interrupted,selected_frame_ids=sorted(allowed),observed_frame_ids=sorted(observed),
        extraction_parameters=dict(frame_batch=frame_batch,entity_page=page_size,pagination='until complete or shared budget'))


def context(net,entity_ids):
    chosen=set(entity_ids)
    states=[deepcopy(s) for s in net['states'].values() if s['entity_id'] in chosen]
    sids={s['id'] for s in states}
    relations=[r for r in net['relations'] if r['subject'] in chosen and r['object'] in chosen]
    rids={r['id'] for r in relations}
    for state in states:
        state['relation_ids']=[rid for rid in state['relation_ids'] if rid in rids]
    return dict(entities=[net['entities'][e] for e in entity_ids],states=states,
        transitions=[t for t in net['transitions'] if t['before'] in sids and t['after'] in sids],
        relations=relations,
        lineage=[r for r in net['lineage'] if r['child'] in chosen and r['parent'] in chosen])


def render_flat(value):
    """Lossless field/value text from exactly the same records as structured QA."""
    lines=[]
    for table,rows in value.items():
        for row in rows:
            lines.append(table+': '+'; '.join(k+' = '+json.dumps(v,ensure_ascii=False,sort_keys=True) for k,v in row.items()))
    return '\n'.join(lines)


def paired_inputs(query,captions,selected,value):
    base=qa_inputs(query,captions,selected)
    policy='\n\nAdditional sampled visual observations. These are model assertions, not independent verification. Unknown bounds/causes stay unknown; gaps do not establish persistence.\n'
    flat=deepcopy(base);structured=deepcopy(base)
    flat['prompts'][-1]+=policy+render_flat(value)
    structured['prompts'][-1]+=policy+json.dumps(value,ensure_ascii=False,sort_keys=True)
    return flat,structured


@metered_cpu('aligned_common_context_selection')
def fit_context(built,packet,cfg,final=True,tokenizer=None):
    if tokenizer is None:
        from transformers import AutoTokenizer
        tokenizer=AutoTokenizer.from_pretrained(cfg['models']['vlm'],local_files_only=True)
    def count(inp):
        return len(tokenizer.apply_chat_template([dict(role='system',content=inp['head']),
            dict(role='user',content=inp['prompts'][0])],tokenize=True,add_generation_prompt=True))
    net=built['network'];scenes={s['id']:s for s in built['directory']['scenes']}
    ids=sorted(net['entities'],key=lambda eid:(-max(scenes[s]['relevance'] for s in net['entities'][eid]['scene_ids']),
             min(scenes[s]['interval'][0] for s in net['entities'][eid]['scene_ids']),eid))
    full=list(ids)
    while True:
        value=context(net,ids)
        pair=paired_inputs(built['query'],packet['captions'],built['selected_seconds'],value)
        sizes=[count(inp) for inp in pair]
        if max(sizes)<=cfg['models']['max_input_tokens']:break
        if not ids:raise ValueError('Original selected captions alone exceed shared QA allowance')
        ids.pop()
    return dict(flat=pair[0],structured=pair[1],context=value,context_hash=digest(value),token_counts=sizes,
        selection=dict(total_entities=len(full),selected_entities=len(ids),omitted_entity_ids=[x for x in full if x not in ids],
            priority='original VideoTree scene relevance, then original video time',
            rule='same complete entity records retained in both renderings; fit both to the same model input allowance'))


def baseline(packet,backend,cfg,target):
    target=Path(target);target.mkdir(parents=True,exist_ok=True)
    ledger=BudgetLedger(cfg['budget']['online_cap'],answer_reserve=cfg['budget']['final_answer_reserve'],calls_path=target/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,target/'raw_calls');result=dict(arm='videotree',answer=None,error=None)
    try:
        meter.call('prepare_vlm',{},build=True)
        query,width,selected=frontend(packet,meter,False)
        inp=qa_inputs(query,packet['captions'],selected)
        out,_=meter.call('videotree_qa',inp,final=True)
        result.update(answer=out['data'],selected_seconds=selected,qa_inputs=inp,width=width)
    except Exception as exc:result['error']=dict(type=type(exc).__name__,message=str(exc))
    finally:
        result.update(question_id=packet['question']['question_id'],cost_summary=ledger.summary())
        write_json(target/'result.json',result);meter.close()
    return result


def pair(packet,backend,cfg,target):
    target=Path(target);target.mkdir(parents=True,exist_ok=True)
    cap=cfg['budget']['online_cap'];reserve=cfg['budget']['final_answer_reserve']
    shared_ledger=BudgetLedger(cap,answer_reserve=2*reserve,calls_path=target/'shared/calls.jsonl')
    meter=Meter(backend,shared_ledger,cfg,target/'shared/raw_calls');built=fitted=None;error=None
    try:
        meter.call('prepare_vlm',{},build=True)
        built=construct(packet,meter,target/'shared',cfg)
        fitted=fit_context(built,packet,cfg,final=True)
        write_json(target/'shared/qa_context.json',fitted)
    except Exception as exc:error=dict(type=type(exc).__name__,message=str(exc))
    finally:
        shared_cost=shared_ledger.summary();write_json(target/'shared/cost_summary.json',shared_cost);meter.close()
    results={}
    for arm,key in [('continuous_flat','flat'),('continuous_script','structured')]:
        out=target/arm;out.mkdir(parents=True,exist_ok=True)
        result=dict(arm=arm,question_id=packet['question']['question_id'],answer=None,error=deepcopy(error),
            shared_cost_summary=shared_cost,shared_artifact=str(target/'shared'),online_cap=cap)
        if not error:
            ledger=BudgetLedger(cap,initialization_cost=shared_cost['total'],answer_reserve=reserve,calls_path=out/'calls.jsonl')
            qm=Meter(backend,ledger,cfg,out/'raw_calls')
            try:
                raw,_=qm.call('videotree_qa',fitted[key],final=True);result['answer']=raw['data']
            except Exception as exc:result['error']=dict(type=type(exc).__name__,message=str(exc))
            finally:result['cost_summary']=ledger.summary();qm.close()
            result.update(selected_seconds=built['selected_seconds'],selected_frame_ids=built['selected_frame_ids'],
                observed_frame_ids=built['observed_frame_ids'],context_hash=fitted['context_hash'],
                context_selection=fitted['selection'],qa_input_tokens=fitted['token_counts'][0 if key=='flat' else 1],
                opened_scenes=built['opened_scenes'],completed_scenes=built['completed_scenes'],incomplete=built['incomplete'],
                entity_count=len(built['network']['entities']),state_count=len(built['network']['states']),
                transition_count=len(built['network']['transitions']),relation_count=len(built['network']['relations']),
                identity_comparisons=len(built['network']['identities']),
                unresolved_identity_comparisons=sum(r['decision']=='unknown' for r in built['network']['identities']),
                observation_page_recoveries=len(built['network'].get('observation_page_recoveries',[])),
                alignment_repairs=len(built['network'].get('alignment_repairs',[])),
                shared_construction_hash=digest([built['selected_seconds'],built['network']]))
        write_json(out/'result.json',result);results[arm]=result
    write_json(target/'result.json',dict(results=results,shared_cost_summary=shared_cost,error=error))
    return results
