"""Source-grounded, sampled temporal entity networks; no query-dependent extraction.

Observation spans describe sampled support, not proof that nothing happened between
samples. Change times and persistence outside that support remain explicitly bounded.
"""
from copy import deepcopy
import math
import re
from .config import digest

SETTINGS = dict(penalty=1.0, min_samples=4, max_scene_seconds=45,
                frame_batch=6, entities_per_page=4, max_pages=8,
                max_scene_visits=4, semantic_links=2, online_seconds=1500,
                directory_seconds=600)
ATTRIBUTE_FIELDS = ['structure', 'components', 'shape', 'texture', 'appearance',
                    'color', 'material', 'size', 'location', 'pose', 'other']


def partition(video_id, features, captions, settings=SETTINGS):
    import numpy as np
    from .segmentation import contiguous_partition
    x = np.asarray(features, dtype=float)
    if len(x) != len(captions) or [c['interval'] for c in captions] != [[i, i+1] for i in range(len(x))]:
        raise ValueError('This runner requires the frozen one-second source grid')
    norm = np.linalg.norm(x, axis=1, keepdims=True)
    if not np.isfinite(x).all() or (norm <= 0).any():
        raise ValueError('Invalid visual source features')
    x = x/norm
    ranges = contiguous_partition(x, settings['penalty'], settings['min_samples'])
    scenes = []
    for a, b in ranges:
        for start in range(a, b, settings['max_scene_seconds']):
            end = min(b, start+settings['max_scene_seconds'])
            center = x[start:end].mean(axis=0)
            length = np.linalg.norm(center)
            if length <= 1e-12:
                raise ValueError('Degenerate scene center')
            center /= length
            order = sorted(range(start, end), key=lambda i: (-float(x[i]@center), i))
            scenes.append(dict(id=f'S{len(scenes)}', interval=[start, end],
                center=center.tolist(), representative_order=order,
                chronological_order=list(range(start, end)),
                caption_ids=[c['id'] for c in captions[start:end]], description='',
                status='directory_only'))
    pointers = []
    for i, s in enumerate(scenes):
        targets = sorted((j for j in range(len(scenes)) if abs(i-j) > 1),
                         key=lambda j: (-sum(a*b for a,b in zip(s['center'], scenes[j]['center'])), j))
        for j, kind in [(i+1, 'temporal_next')]+[(j, 'similar_scene') for j in targets[:settings['semantic_links']]]:
            if j >= len(scenes):
                continue
            a,b = sorted((i,j))
            if any(p['source']==scenes[a]['id'] and p['target']==scenes[b]['id'] and p['kind']==kind for p in pointers):
                continue
            pointers.append(dict(id=f'P{len(pointers)}', source=scenes[a]['id'], target=scenes[b]['id'],
                kind=kind, bidirectional=True, status='navigation_hint_not_identity_or_causality',
                similarity=float(sum(u*v for u,v in zip(s['center'], scenes[j]['center'])))))
    result = dict(schema_version=1, method='contiguous_visual_temporal_entities', video_id=video_id,
        captions=deepcopy(captions), source_records_hash=digest(captions), scenes=scenes, pointers=pointers,
        feature_space='EVA-CLIP-8B projected image/text, L2 normalized', settings=deepcopy(settings))
    validate_directory(result)
    return result


def validate_directory(d):
    cursor=0
    for s in d['scenes']:
        a,b=s['interval']
        if a!=cursor or b<=a or s['chronological_order']!=list(range(a,b)):
            raise ValueError('Scenes must form a contiguous partition')
        if sorted(s['representative_order'])!=list(range(a,b)):
            raise ValueError('Representative order omitted frames')
        if s['caption_ids']!=[c['id'] for c in d['captions'][a:b]]:
            raise ValueError('Scene caption sources changed')
        if not s['center'] or not all(math.isfinite(v) for v in s['center']):
            raise ValueError('Invalid center')
        cursor=b
    if cursor!=len(d['captions']) or digest(d['captions'])!=d['source_records_hash']:
        raise ValueError('Directory omitted source information')


def new_network():
    return dict(schema_version=1, entities={}, states={}, transitions=[], relations=[],
                identities=[], lineage=[], aliases={}, observations=[], coverage=[], diagnostics=[], next_entity=0)


def canonical(net, ref):
    seen=set()
    while ref in net['aliases']:
        if ref in seen:
            raise ValueError('Identity cycle')
        seen.add(ref)
        ref=net['aliases'][ref]
    return ref


def grounded_frames(refs, frames):
    allowed={f['frame_id']: f for f in frames}
    if not refs or not set(refs)<=allowed.keys():
        raise ValueError('Missing or foreign visual evidence')
    return [allowed[r] for r in dict.fromkeys(refs)]


def ingest(net, report, frames, scene_id, batch_id, known_ids):
    """Each entity row may contain several true changes within a six-frame batch."""
    allowed={f['frame_id']:f for f in frames}
    mapping={}
    seen_known=set()
    for i,row in enumerate(report['entities']):
        ref=row['entity_ref']
        if ref!='new':
            if ref not in known_ids or ref in seen_known:
                raise ValueError(f'Invalid or duplicate within-scene identity {ref} at B{i} '
                                 f'({row["label"]}); distinct instances must use new, not a shared ID')
            seen_known.add(ref)
            eid=canonical(net,ref)
        else:
            eid=f'E{net["next_entity"]}'
            net['next_entity']+=1
            net['entities'][eid]=dict(id=eid,label=row['label'],attributes=[],scene_ids=[],state_ids=[])
        mapping[f'B{i}']=eid
        mapping[ref]=eid if ref!='new' else mapping.get(ref,eid)
    for i,row in enumerate(report['entities']):
        eid=mapping[f'B{i}']; entity=net['entities'][eid]
        if scene_id not in entity['scene_ids']:
            entity['scene_ids'].append(scene_id)
        for attr in row['attributes']:
            support=grounded_frames(attr['source_frame_ids'],frames)
            entity['attributes'].append(dict(attr, observed_at=[f['pts'] for f in support],
                source_status='visual_model_assertion', scene_id=scene_id, batch_id=batch_id))
        observations=sorted(row['observations'],key=lambda r:allowed[r['frame_id']]['pts'])
        if not observations:
            if row['entity_ref']=='new':
                raise ValueError('A new entity needs at least one state observation')
            continue
        for ob in observations:
            f=allowed[ob['frame_id']]
            existing=[net['states'][sid] for sid in entity['state_ids'] if net['states'][sid]['scene_id']==scene_id]
            previous=max(existing,key=lambda s:s['last_observed_at']) if existing else None
            if previous and f['pts']<previous['last_observed_at']:
                raise ValueError('Within-scene observations must arrive in time order')
            if previous and ob['continuity']=='same':
                state=previous
                state['last_observed_at']=max(state['last_observed_at'],f['pts'])
                if f['frame_id'] not in state['source_frame_ids']:
                    state['source_frame_ids'].append(f['frame_id'])
            else:
                if 'state' not in ob or 'cause' not in ob:
                    raise ValueError('A first, changed or uncertain observation needs a full state and cause; short same requires an existing scene state')
                if previous and ob['continuity']=='initial':
                    raise ValueError('A scene boundary or fresh description cannot reset an existing entity')
                sid=f'T{len(net["states"])}'
                state=dict(id=sid,entity_id=eid,scene_id=scene_id,description=ob['state'],
                    logical_state_id=sid,fragment_kind='initial_observation' if previous is None else 'actual_change',
                    first_observed_at=f['pts'],last_observed_at=f['pts'],valid_from=None,valid_to=None,
                    valid_from_bounds=[None,f['pts']],valid_to_bounds=None,
                    status='sampled_visual_assertion', boundary='initial_observation' if previous is None else ob['continuity'],
                    source_frame_ids=[f['frame_id']], relation_ids=[])
                # Uncertain observations are retained separately, not promoted to new physical states.
                if ob['continuity']=='uncertain' and previous:
                    net['observations'].append(dict(entity_id=eid,scene_id=scene_id,observation=deepcopy(ob),
                        pts=f['pts'],status='unresolved_state_observation'))
                    continue
                net['states'][sid]=state; entity['state_ids'].append(sid)
                if previous:
                    bounds=[previous['last_observed_at'],f['pts']]
                    previous['valid_to_bounds']=bounds
                    state['valid_from_bounds']=bounds
                    cause=deepcopy(ob['cause'])
                    refs=cause['source_frame_ids']
                    if cause['basis']!='unknown':
                        support=grounded_frames(refs,frames)
                        if cause['basis']=='observed_action' and len({f['pts'] for f in support})<3:
                            cause['basis']='inferred_action'
                    if cause['basis']=='unknown':
                        cause.update(action=None, actor=None, tool=None,source_frame_ids=[])
                    net['transitions'].append(dict(id=f'X{len(net["transitions"])}',entity_id=eid,
                        before=previous['id'],after=sid,change_time_bounds=bounds,cause=cause,
                        operation_interval=([min(allowed[r]['pts'] for r in refs),max(allowed[r]['pts'] for r in refs)]
                                            if refs and cause['basis']!='unknown' else None),
                        status='visual_model_assertion_not_independently_verified'))
            state['observed_support_interval']=[state['first_observed_at'],state['last_observed_at']]
    refs={**{k:canonical(net,k) for k in known_ids},**mapping}
    for row in report['relations']:
        if row['subject'] not in refs or row['object'] not in refs:
            raise ValueError('Foreign relation endpoint')
        a,b=refs[row['subject']],refs[row['object']]
        if a==b:
            raise ValueError(f'A relation collapsed distinct endpoints: {row["subject"]} and '
                             f'{row["object"]} both resolve to {a}; use distinct grounded entities or omit the relation')
        support=grounded_frames(row['source_frame_ids'],frames)
        t=allowed[row['frame_id']]['pts']
        matches=[r for r in net['relations'] if (r['subject'],r['object'],r['predicate'],r['scene_id'])==
                 (a,b,row['predicate'],scene_id) and r['valid_to_bounds'] is None]
        if row['update']=='end':
            for r in matches:
                r['valid_to_bounds']=[r['last_observed_at'],t]
        elif matches:
            r=matches[-1];r['last_observed_at']=max(r['last_observed_at'],t)
            r['source_frame_ids']=list(dict.fromkeys(r['source_frame_ids']+row['source_frame_ids']))
        else:
            net['relations'].append(dict(id=f'R{len(net["relations"])}',subject=a,object=b,
                predicate=row['predicate'],scene_id=scene_id,first_observed_at=t,last_observed_at=t,
                valid_from=None,valid_to=None,valid_from_bounds=[t,t],valid_to_bounds=None,
                source_frame_ids=row['source_frame_ids'],status='sampled_visual_assertion'))
    refresh(net)
    return list(mapping[f'B{i}'] for i in range(len(report['entities'])))


def compact_profiles(rows):
    """Factor repeated attribute observations without dropping values or sources.

    Storage batch IDs, duplicated state bounds and relation indexes stay in the
    complete network. The observation/identity prompt needs attributes and their
    observed support, plus the latest physical state, rather than storage rows.
    """
    result=[]
    for row in rows:
        attributes={}
        for attr in row.get('attributes',[]):
            key=(attr['field'],attr['value'])
            entry=attributes.setdefault(key,dict(field=attr['field'],value=attr['value'],
                source_frame_ids=[],observed_at=[]))
            entry['source_frame_ids']=list(dict.fromkeys(entry['source_frame_ids']+attr.get('source_frame_ids',[])))
            entry['observed_at']=sorted(set(entry['observed_at']+attr.get('observed_at',[])))
        profile={k:deepcopy(row[k]) for k in ('id','label','scene_ids','state_ids','context_selection') if k in row}
        if 'attributes' in row:profile['attributes']=list(attributes.values())
        if 'last_state' in row:
            state=row['last_state']
            profile['last_state']=({k:deepcopy(state[k]) for k in ('id','description','first_observed_at',
                'last_observed_at','source_frame_ids','logical_state_id','fragment_kind') if k in state}
                if state else None)
        result.append(profile)
    return result


def collapse_exact_known_rows(report, known_ids):
    """Repeated identical updates to an explicitly known ID are serialization repeats.

    Never coalesce new entities or differing claims about a known ID. B indexes
    are remapped before validation, so a duplicate cannot hide a self-relation or
    move an edge onto an unrelated entity.
    """
    value=deepcopy(report);rows=[];seen={};positions={};duplicates=[]
    for index,row in enumerate(value['entities']):
        ref=row['entity_ref'];signature=digest(row)
        key=(ref,signature)
        if ref in known_ids and key in seen:
            target=seen[key]
            duplicates.append(dict(position=index,retained_position=target,entity_ref=ref))
        else:
            target=len(rows);rows.append(row)
            if ref in known_ids:seen[key]=target
        positions[f'B{index}']=f'B{target}'
    for row in rows:
        for observation in row['observations']:
            if 'cause' not in observation:
                continue
            for field in ('actor','tool'):
                ref=observation['cause'][field]
                observation['cause'][field]=positions.get(ref,ref)
    for relation in value['relations']:
        for field in ('subject','object'):
            relation[field]=positions.get(relation[field],relation[field])
    value['entities']=rows
    return value,duplicates


def observe_page(net, inputs, meter, scene_id, batch_id, known_ids, build=False, max_attempts=3):
    """Validate a whole page before committing; every failed attempt stays in call logs.

    Re-observing a known entity is progress on its first page in this image batch.
    Repeating an entity already covered in this batch is a contract error, even
    when the model claims the inventory is complete. Never infer a new identity
    from a label or silently discard an invalid relation to make a page pass.
    """
    request=deepcopy(inputs)
    if getattr(meter,'cfg',{}).get('query',{}).get('temporal_delta_observations',False):
        request['delta_observations']=True
    request['entities']=compact_profiles(inputs['entities'])
    # Already-reported IDs refer to the profiles above; repeating every profile
    # here doubled a growing prompt on each page of the failed GPU smoke.
    request['already_reported']=[dict(id=e['id']) for e in inputs['already_reported']]
    reported={canonical(net,e['id']) for e in inputs['already_reported']}
    failures=[]
    for attempt in range(max_attempts):
        report,call=meter.json('temporal_observe',request,
            ('entities','relations','more_entities','unresolved'),build=build)
        report,duplicates=collapse_exact_known_rows(report,known_ids)
        try:
            for row in report['entities']:
                ref=row['entity_ref']
                if ref!='new' and canonical(net,ref) in reported:
                    raise ValueError(f'Entity {ref} was already covered in this image batch; '
                                     'return remaining entities only, using new for new instances')
                if ref!='new' and ref in known_ids:
                    old=[net['states'][s]['last_observed_at'] for s in net['entities'][canonical(net,ref)]['state_ids']
                         if net['states'][s]['scene_id']==scene_id]
                    if old:
                        times={f['frame_id']:f['pts'] for f in inputs['frames']}
                        row['observations']=[o for o in row['observations'] if times[o['frame_id']]>max(old) or
                                             (times[o['frame_id']]==max(old) and o['continuity']=='same')]
            if report['more_entities'] and not report['entities']:
                raise ValueError('Inventory pagination made no progress: empty entities with more_entities=true')
            staged=deepcopy(net)
            ids=ingest(staged,report,inputs['frames'],scene_id,batch_id,known_ids)
        except (ValueError,KeyError) as exc:
            failures.append(dict(attempt=attempt,call_id=call['call_id'],error=str(exc)))
            if attempt+1==max_attempts:
                raise ValueError(f'Temporal observation page failed after {max_attempts} attempts: {exc}') from exc
            request['validation_feedback']=dict(error=str(exc),
                rejected_entities=[dict(entity_ref=r['entity_ref'],label=r['label']) for r in report['entities']],
                rejected_relations=report['relations'],
                instruction='Regenerate the entire page from the same images. Rejected output was NOT committed. '
                            'Each known ID occurs at most once; use new for every distinct new instance. '
                            'Relations must connect distinct valid endpoints. Do not repeat already_reported entities.')
            # The decoder can enforce one row, even though it cannot enforce
            # uniqueness of entity_ref across several otherwise different rows.
            # Continue pagination after this recovery page to inventory remaining
            # objects; never convert a conflicting ID into a guessed new entity.
            if 'duplicate within-scene identity' in str(exc) or 'already covered' in str(exc):
                request['page_size']=1
                request['validation_feedback']['instruction']+=' Return one entity on this recovery page; remaining inventory is requested on the next page.'
            continue
        if failures:
            staged.setdefault('observation_repairs',[]).append(dict(scene_id=scene_id,batch_id=batch_id,
                rejected_attempts=failures,accepted_call_id=call['call_id']))
        if duplicates:
            staged.setdefault('observation_normalizations',[]).append(dict(scene_id=scene_id,batch_id=batch_id,
                call_id=call['call_id'],exact_known_row_duplicates=duplicates))
        if request['page_size']<inputs['page_size']:
            staged.setdefault('observation_page_recoveries',[]).append(dict(scene_id=scene_id,batch_id=batch_id,
                call_id=call['call_id'],original_page_size=inputs['page_size'],recovery_page_size=request['page_size'],
                model_more_entities=report['more_entities'],continuation_required=True))
            report['more_entities']=True
        net.clear();net.update(staged)
        return ids,report,call


def refresh(net):
    for e in net['entities'].values():
        e['state_ids']=sorted(set(e['state_ids']),key=lambda sid:(net['states'][sid]['first_observed_at'],sid))
    for s in net['states'].values():
        s['relation_ids']=[r['id'] for r in net['relations'] if s['entity_id'] in (r['subject'],r['object'])
            and s['scene_id']==r['scene_id'] and max(s['first_observed_at'],r['first_observed_at'])<=
                min(s['last_observed_at'],r['last_observed_at'])]


def align_page(net, inputs, meter, build=False, max_attempts=3):
    """Exact pair/state allowlists and separate source citations before transfer."""
    request=deepcopy(inputs)
    states={s['id']:s for s in inputs['states']}
    available={f['frame_id'] for f in inputs['frames']}
    state_pairs=inputs.get('compare_state_pairs')
    if state_pairs is None:
        state_pairs=[dict(left=a,right=b,**inputs['compare_state_pair']) for a,b in inputs['pairs']]
    request['identity_evidence_pairs']=[dict(pair,
        left_source_frame_ids=[f for f in states[pair['left_state']]['source_frame_ids'] if f in available],
        right_source_frame_ids=[f for f in states[pair['right_state']]['source_frame_ids'] if f in available])
        for pair in state_pairs]
    allowed={(p['left'],p['right'],p['left_state'],p['right_state']) for p in state_pairs}
    failures=[]
    for attempt in range(max_attempts):
        report,call=meter.json('temporal_align',request,('matches',),build=build)
        try:
            seen=set()
            for row in report['matches']:
                key=tuple(row[k] for k in ('left','right','left_state','right_state'))
                if key not in allowed or key in seen:
                    raise ValueError('Alignment changed or repeated a supplied boundary pair')
                seen.add(key)
            if seen!=allowed:
                raise ValueError('Alignment omitted a supplied boundary pair; return an explicit unknown decision')
            # Multiple candidates for ONE entity are ambiguous. Independent
            # pairs in the same page may legitimately each be a match.
            same=[r for r in report['matches'] if r['decision']=='same']
            ambiguous={r['left'] for r in same if sum(s['left']==r['left'] for s in same)>1}
            ambiguous_right={r['right'] for r in same if sum(s['right']==r['right'] for s in same)>1}
            rejected=[]
            for row in report['matches']:
                if row['decision']=='same' and (row['left'] in ambiguous or row['right'] in ambiguous_right):
                    rejected.append(deepcopy(row))
                    row.update(decision='unknown',state_continuity='unknown',ended_relation_ids=[],
                        reason='Multiple competing same-instance candidates; identity remains unresolved')
                    row.pop('left_source_frame_id',None);row.pop('right_source_frame_id',None)
            staged=deepcopy(net)
            align(staged,report,inputs['frames'],inputs['pairs'])
            if rejected:
                staged.setdefault('alignment_ambiguities',[]).append(dict(call_id=call['call_id'],
                    candidate_matches=rejected,status='unresolved_no_identity_or_history_transfer'))
        except (ValueError,KeyError) as exc:
            failures.append(dict(call_id=call['call_id'],error=str(exc)))
            if attempt+1==max_attempts:raise
            request['validation_feedback']=dict(error=str(exc),instruction=
                'Regenerate exact supplied comparisons. For a supported decision cite one allowed frame from EACH side. '
                'If identity cannot be established, return unknown with unknown state continuity; do not transfer history.')
            continue
        if failures:
            staged.setdefault('alignment_repairs',[]).append(dict(rejected_attempts=failures,accepted_call_id=call['call_id']))
        net.clear();net.update(staged)
        return report,call


def align(net, report, frames, pairs):
    """Identity is resolved before time-sorted state/relation transfer; gaps stay gaps."""
    allowed_pairs=set(map(tuple,pairs))
    for raw in report['matches']:
        row=deepcopy(raw)
        if (row['left'],row['right']) not in allowed_pairs:
            raise ValueError('Identity outside candidate pairs')
        left,right=canonical(net,row['left']),canonical(net,row['right'])
        if left==right:
            continue
        l,r=net['entities'][left],net['entities'][right]
        left_state,right_state=row['left_state'],row['right_state']
        if left_state not in l['state_ids'] or right_state not in r['state_ids']:
            raise ValueError('Identity comparison used foreign state references')
        decision=row['decision']
        # Unknown is an unresolved comparison, not an identity/lineage claim.
        # It may have zero or one-sided evidence, but cannot alter any history.
        if decision=='unknown':
            if row['state_continuity']!='unknown' or row.get('ended_relation_ids'):
                raise ValueError('Unknown identity cannot assert state continuity or end relations')
            refs=row['source_frame_ids']
            evidence=grounded_frames(refs,frames) if refs else []
            net['identities'].append(dict(row,status='unresolved_identity_comparison',
                                         source_times=[f['pts'] for f in evidence]))
            continue
        refs=list(dict.fromkeys(row['source_frame_ids']+
            [row[k] for k in ('left_source_frame_id','right_source_frame_id') if k in row]))
        row['source_frame_ids']=refs
        evidence=grounded_frames(refs,frames)
        left_frames={f for sid in l['state_ids'] for f in net['states'][sid]['source_frame_ids']}
        right_frames={f for sid in r['state_ids'] for f in net['states'][sid]['source_frame_ids']}
        if not (left_frames & set(row['source_frame_ids']) and right_frames & set(row['source_frame_ids'])):
            raise ValueError('Identity needs evidence from both entities')
        if decision=='same' and set(l['scene_ids']) & set(r['scene_ids']):
            raise ValueError('Cannot merge distinct co-scene instances')
        net['identities'].append(dict(row,status='visual_model_identity_assertion',
                                     source_times=[f['pts'] for f in evidence]))
        if decision in ('derived_from','part_of','assembled_into'):
            net['lineage'].append(dict(child=left,parent=right,relation=decision,
                                      source_frame_ids=row['source_frame_ids']))
        if decision!='same':
            continue
        # Keep all historical fragments. Same identity never implies gap persistence.
        l['attributes'].extend(r['attributes']);l['scene_ids']=sorted(set(l['scene_ids']+r['scene_ids']))
        l['state_ids'].extend(r['state_ids'])
        for sid in r['state_ids']:
            net['states'][sid]['entity_id']=left
        for edge in net['relations']:
            for key in ('subject','object'):
                if edge[key]==right:edge[key]=left
        for edge in net['transitions']:
            if edge['entity_id']==right:edge['entity_id']=left
        net['aliases'][right]=left
        del net['entities'][right]
        refresh(net)
        # Boundary comparison describes a state equivalence/change, not an event at a scene cut.
        boundary=sorted((left_state,right_state),key=lambda sid:net['states'][sid]['first_observed_at'])
        for earlier,later in [boundary]:
            a,b=net['states'][earlier],net['states'][later]
            if a['scene_id']==b['scene_id'] or any(x['before']==earlier and x['after']==later for x in net['transitions']):
                continue
            if row['state_continuity']=='changed':
                b['fragment_kind']='actual_change'
                b['valid_from_bounds']=[a['last_observed_at'],b['first_observed_at']]
                a['valid_to_bounds']=b['valid_from_bounds']
                net['transitions'].append(dict(id=f'X{len(net["transitions"])}',entity_id=left,
                    before=earlier,after=later,change_time_bounds=[a['last_observed_at'],b['first_observed_at']],
                    cause=dict(action=None,actor=None,tool=None,basis='unknown',source_frame_ids=[]),
                    operation_interval=None,status='cross_scene_change_cause_unknown'))
            elif row['state_continuity']=='same':
                # Explicit equivalence of supported observations, without filling the unsampled gap.
                b['same_observed_state_as']=earlier
                old_logical=b['logical_state_id']
                for state in net['states'].values():
                    if state['entity_id']==left and state['logical_state_id']==old_logical:
                        state['logical_state_id']=a['logical_state_id']
                b['fragment_kind']='same_state_observation_fragment'
            else:
                b['logical_state_id']=None
                b['fragment_kind']='unresolved_continuation_not_new_physical_state'
        for rid in row.get('ended_relation_ids',[]):
            edge=next((e for e in net['relations'] if e['id']==rid),None)
            if edge is None or left not in (edge['subject'],edge['object']):
                raise ValueError('Invalid cross-scene relation termination')
            edge['valid_to_bounds']=[edge['last_observed_at'],max(f['pts'] for f in evidence)]
            edge['termination_source_frame_ids']=row['source_frame_ids']
    refresh(net)


def entity_context(net, query, limit=12, state_limit=32):
    terms=set(re.findall(r'\w+', (query['question']+' '+' '.join(query['options'])).lower()))
    scored=[]
    for e in net['entities'].values():
        text=' '.join([e['label']]+[a['value'] for a in e['attributes']]+[net['states'][s]['description'] for s in e['state_ids']])
        words=set(re.findall(r'\w+',text.lower()))
        score=len(words&terms)+2*sum(x['entity_id']==e['id'] for x in net['transitions'])
        scored.append((-score,e['id']))
    chosen=[eid for _,eid in sorted(scored)[:limit]]
    states=[deepcopy(s) for s in net['states'].values() if s['entity_id'] in chosen]
    states=sorted(states,key=lambda s:(-sum(x['before']==s['id'] or x['after']==s['id'] for x in net['transitions']),s['first_observed_at']))[:state_limit]
    sids={s['id'] for s in states}
    relations=[r for r in net['relations'] if r['subject'] in chosen and r['object'] in chosen][-32:]
    rids={r['id'] for r in relations}
    for state in states:
        state['relation_ids']=[rid for rid in state['relation_ids'] if rid in rids]
    return dict(entities=[dict(net['entities'][eid],attributes=net['entities'][eid]['attributes'][-16:]) for eid in chosen],
        states=sorted(states,key=lambda s:s['first_observed_at']),
        transitions=[t for t in net['transitions'] if t['before'] in sids and t['after'] in sids],
        relations=relations,
        lineage=[r for r in net['lineage'] if r['child'] in chosen and r['parent'] in chosen][-12:],
        context_selection=dict(total_entities=len(net['entities']),selected_entities=len(chosen),
            total_states=len(net['states']),selected_states=len(states),attributes_per_entity=16,
            note='Full source graph is retained separately; this is a bounded retrieval view.'))


def output_schema(op, inputs):
    from .schemas import record,array,enum,short_text,B,NULL_S
    fids=[f['frame_id'] for f in inputs.get('frames',[])]
    evidence=array(enum(fids),6);evidence['minItems']=1
    cause=record(dict(action=NULL_S,actor=NULL_S,tool=NULL_S,
        basis=enum(['observed_action','inferred_action','unknown']),source_frame_ids=array(enum(fids),6)))
    if op=='temporal_summary':
        return record(dict(description=short_text(420)))
    if op=='temporal_observe':
        known=[e['id'] for e in inputs['entities']]
        reported={e['id'] for e in inputs.get('already_reported',[])}
        refs=known+[f'B{i}' for i in range(inputs['page_size'])]
        observation=record(dict(frame_id=enum(fids),state=short_text(140),
            continuity=enum(['initial','same','changed','uncertain']),cause=cause))
        if inputs.get('delta_observations'):
            # Full state/cause is required unless a prior scene state exists.
            # The semantic ingester checks that condition before accepting a
            # short same. Every observed frame ID remains explicit.
            observation={'anyOf':[
                record(dict(continuity=enum(['same']),frame_id=enum(fids))),
                record(dict(continuity=enum(['initial','same','changed','uncertain']),
                    frame_id=enum(fids),state=short_text(140),cause=cause))]}
        # The batch includes new frames plus one boundary frame. A fixed six
        # slots forced a seventh observation into a duplicate known-entity row.
        observations=array(observation,len(fids));observations['minItems']=1
        fields=dict(attributes=array(record(dict(field=enum(ATTRIBUTE_FIELDS),value=short_text(100),source_frame_ids=evidence)),12),
                    observations=observations)
        # A known ID always retains its supplied canonical label. This prevents
        # an unrelated fabric/container row from choosing the needle/person ID.
        variants=[record(dict(entity_ref=enum(['new']),label=short_text(64),**fields))]
        variants += [record(dict(entity_ref=enum([e['id']]),
            label=enum([e['label']]) if 'label' in e else short_text(64),**fields))
            for e in inputs['entities'] if e['id'] not in reported]
        entity={'anyOf':variants}
        # Constrain literal self-relations at decode time. B-index/known-ID aliases
        # are additionally checked against the actual accepted entity rows.
        relation={'anyOf':[record(dict(subject=enum([ref]),object=enum([r for r in refs if r!=ref]),
            predicate=short_text(48),frame_id=enum(fids),update=enum(['present','end']),
            source_frame_ids=evidence)) for ref in refs if len(refs)>1]}
        relations=array(relation,12) if len(refs)>1 else array(enum([]),0)
        return record(dict(entities=array(entity,inputs['page_size']),relations=relations,
            more_entities=B,unresolved=short_text(180)))
    if op=='temporal_align':
        if inputs.get('identity_evidence_pairs'):
            variants=[]
            extra_evidence=array(enum(fids),6)
            for pair in inputs['identity_evidence_pairs']:
                base={k:enum([pair[k]]) for k in ('left','right','left_state','right_state')}
                unknown=record(dict(**base,decision=enum(['unknown']),state_continuity=enum(['unknown']),
                    reason=short_text(160),source_frame_ids=extra_evidence,ended_relation_ids=array(enum([]),0)),
                    optional=('ended_relation_ids',))
                variants.append(unknown)
                for decisions,continuity in [(['same'],['same','changed','unknown']),
                        (['different','derived_from','part_of','assembled_into'],['unknown'])]:
                    if not pair['left_source_frame_ids'] or not pair['right_source_frame_ids']:continue
                    variants.append(record(dict(**base,decision=enum(decisions),state_continuity=enum(continuity),
                        reason=short_text(160),source_frame_ids=extra_evidence,
                        left_source_frame_id=enum(pair['left_source_frame_ids']),
                        right_source_frame_id=enum(pair['right_source_frame_ids']),
                        ended_relation_ids=array(enum([r['id'] for r in inputs.get('relations',[])]),6)),
                        optional=('ended_relation_ids',)))
            matches=array({'anyOf':variants},len(inputs['identity_evidence_pairs']))
            matches['minItems']=len(inputs['identity_evidence_pairs'])
            return record(dict(matches=matches))
        return record(dict(matches=array(record(dict(left=enum([p[0] for p in inputs['pairs']]),
            right=enum([p[1] for p in inputs['pairs']]),decision=enum(['same','different','unknown','derived_from','part_of','assembled_into']),
            left_state=enum([s for e in inputs['left_entities'] for s in e['state_ids']]),
            right_state=enum([s for e in inputs['right_entities'] for s in e['state_ids']]),
            ended_relation_ids=array(enum([r['id'] for r in inputs.get('relations',[])]),6),
            state_continuity=enum(['same','changed','unknown']),reason=short_text(160),source_frame_ids=evidence)),4)))
    if op=='temporal_review':
        return record(dict(sufficient=B,reason=short_text(240),gap=short_text(160),
            next_scene=enum(inputs['allowed_scene_ids']+['stop']),entity_ids=array(enum([e['id'] for e in inputs['entities']]),8)))
    raise ValueError(op)
