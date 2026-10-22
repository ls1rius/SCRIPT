"""Temporal entity memory extracted from original timestamped descriptions.

The legacy state assembler uses frame-named reference fields internally. This
adapter supplies caption timestamps, then converts every stored reference and
assertion status back to caption provenance before committing the network.
No image path, pixel, question or answer is supplied to the extractor.
"""
from copy import deepcopy

from .accounting import metered_cpu
from .temporal_entity import ingest, canonical, compact_profiles
from .summary_graph import grounded_label
from .scene_memory import validate_matches
from .state import write_json


REFERENCE_KEYS={'frame_id':'caption_id','source_frame_ids':'source_caption_ids',
                'frame_ids':'caption_ids'}
CAPTION_STATUSES={'visual_model_assertion':'caption_asserted',
                  'sampled_visual_assertion':'caption_asserted',
                  'visual_model_assertion_not_independently_verified':'caption_asserted',
                  'sampled_inventory_not_continuous_visibility':'caption_inventory_not_continuous_visibility'}


def map_evidence(value,to_caption=True):
    keys=REFERENCE_KEYS if to_caption else {v:k for k,v in REFERENCE_KEYS.items()}
    if isinstance(value,dict):
        return {keys.get(k,k):([keys.get(item,item) for item in v] if k=='required' else CAPTION_STATUSES.get(v,v) if to_caption and
                k in ('status','source_status') and isinstance(v,str) else map_evidence(v,to_caption))
                for k,v in value.items()}
    if isinstance(value,list):return [map_evidence(v,to_caption) for v in value]
    return value


def output_schema(inputs):
    from .temporal_entity import output_schema as legacy_schema
    adapted=map_evidence(inputs,False)
    adapted['frames']=[dict(frame_id=c['id'],pts=c['interval'][0]) for c in inputs['captions']]
    adapted['delta_observations']=True
    schema=map_evidence(legacy_schema('temporal_observe',adapted))
    # A summary batch is a whole scene rather than a six-image batch. Keep
    # sparse state changes, not one repeated full state for each source second.
    for variant in schema['properties']['entities']['items']['anyOf']:
        variant['properties']['observations']['maxItems']=min(12,len(inputs['captions']))
        for obs in variant['properties']['observations']['items']['anyOf']:
            if 'cause' in obs['properties']:
                obs['properties']['cause']['properties']['basis']['enum']=['inferred_action','unknown']
    return schema


def commit_page(net,report,captions,scene_id,batch_id,known_ids):
    """Commit atomically with caption citations and exact original text retained."""
    sources={c['id']:c for c in captions}
    for row in report['entities']:
        cited={ob['caption_id'] for ob in row['observations']}
        if not cited<=sources.keys():raise ValueError('Foreign caption observation')
        text='\n'.join(sources[c]['text'] for c in cited)
        if not grounded_label(row['label'],text):
            raise ValueError('Entity label is not grounded in its cited summary text')
        for ob in row['observations']:
            if 'state' in ob and not grounded_label(ob['state'],sources[ob['caption_id']]['text']):
                raise ValueError('State text is not stated in its cited summary')
            cause=ob.get('cause',{})
            if cause.get('basis','unknown')!='unknown':
                refs=cause.get('source_caption_ids',[])
                if not refs or not set(refs)<=sources.keys():raise ValueError('Foreign caption action')
                evidence='\n'.join(sources[c]['text'] for c in refs)
                for key in ('action','actor','tool'):
                    if cause.get(key) and not grounded_label(cause[key],evidence):
                        raise ValueError('Action/actor/tool is not stated in cited summaries')
        for attr in row['attributes']:
            refs=attr['source_caption_ids']
            if not refs or not set(refs)<=sources.keys():raise ValueError('Foreign caption attribute')
            if not grounded_label(attr['value'],'\n'.join(sources[c]['text'] for c in refs)):
                raise ValueError('Attribute value is not stated in cited summary text')
    for relation in report['relations']:
        refs=relation['source_caption_ids']
        if not refs or not set(refs)<=sources.keys():raise ValueError('Foreign caption relation')
        if not grounded_label(relation['predicate'],'\n'.join(sources[c]['text'] for c in refs)):
            raise ValueError('Relation predicate is not stated in cited summary text')
    staged=map_evidence(net,False)
    support=[dict(frame_id=c['id'],pts=c['interval'][0]) for c in captions]
    ids=ingest(staged,map_evidence(report,False),support,scene_id,batch_id,known_ids)
    staged=map_evidence(staged)
    staged.update(source_type='caption',evidence_status='caption_asserted',physical_identity_confirmed=False)
    for c in captions:staged.setdefault('source_captions',{})[c['id']]=deepcopy(c)
    all_sources=staged['source_captions']
    for state in staged['states'].values():
        refs=state['source_caption_ids']
        state['source_support_interval']=[min(all_sources[c]['interval'][0] for c in refs),
                                          max(all_sources[c]['interval'][1] for c in refs)]
        # Caption support is an interval, not an exact visual change instant.
        for key in ('valid_from_bounds','valid_to_bounds'):
            bounds=state.get(key)
            if net['states'].get(state['id'],{}).get(key)==bounds:continue
            if bounds and bounds[1] in [c['interval'][0] for c in captions]:
                upper=max(c['interval'][1] for c in captions if c['interval'][0]==bounds[1])
                state[key]=[bounds[0],upper]
    for relation in staged['relations']:
        if relation['id'] not in {r['id'] for r in net['relations']}:
            refs=relation['source_caption_ids']
            relation['valid_from_bounds']=[None,min(all_sources[c]['interval'][1] for c in refs)]
    for transition in staged['transitions']:
        if transition['id'] not in {t['id'] for t in net['transitions']}:
            transition['change_time_bounds']=deepcopy(staged['states'][transition['after']]['valid_from_bounds'])
        refs=transition['cause']['source_caption_ids']
        if refs and transition['cause']['basis']!='unknown':
            transition['operation_interval']=[min(all_sources[c]['interval'][0] for c in refs),
                                              max(all_sources[c]['interval'][1] for c in refs)]
    net.clear();net.update(staged)
    return ids


@metered_cpu('caption_temporal_scene_expansion')
def open_scene(net,scene,captions,meter,target,settings):
    from pathlib import Path
    # Input source records are the complete original scene summaries, never
    # the generated scene-directory description or a question-dependent digest.
    allowed=set(scene['caption_ids'])
    sources=[c for c in captions if c['id'] in allowed]
    known=[];reported=[]
    for page in range(settings.get('caption_max_pages',4)):
        entities=[dict(net['entities'][eid],last_state=net['states'][net['entities'][eid]['state_ids'][-1]])
                  for eid in known]
        profiles=map_evidence(compact_profiles(map_evidence(entities,False)))
        inputs=dict(captions=deepcopy(sources),entities=profiles,
                    already_reported=[dict(id=eid) for eid in reported],page_size=12,
                    scene_interval=scene['interval'],page=page,delta_observations=True)
        for attempt in range(2):
            report,call=meter.json('caption_temporal_observe',inputs,
                                  ('entities','relations','more_entities','unresolved'))
            try:
                # A repeated already-reported entity would hide a failed inventory loop.
                if any(row['entity_ref'] in reported for row in report['entities']):
                    raise ValueError('Caption inventory repeated an already reported entity')
                ids=commit_page(net,report,sources,scene['id'],f'{scene["id"]}:captions:{page}',known)
                break
            except ValueError as exc:
                if attempt:raise
                inputs['validation_feedback']=str(exc)+' Copy only phrases stated in the cited summaries; omit unsupported attributes.'
        known=list(dict.fromkeys(known+ids));reported.extend(ids)
        net['coverage'].append(dict(scene_id=scene['id'],page=page,caption_ids=[c['id'] for c in sources],
            more_entities=report['more_entities'],unresolved=report['unresolved'],call_id=call['call_id'],
            status='caption_inventory_not_continuous_visibility'))
        write_json(Path(target)/'network.json',net)
        if not report['more_entities']:return known
        if not ids:raise ValueError('Caption inventory pagination made no progress')
    net['diagnostics'].append(dict(scene_id=scene['id'],code='caption_inventory_page_limit',complete=False))
    return known


def _merge_caption_identity(net,left,right):
    """A text-supported identity remains explicitly inferred, never pixel verified."""
    left,right=canonical(net,left),canonical(net,right)
    if left==right:return
    source=net['entities'][left];dest=net['entities'][right]
    dest['attributes'].extend(source['attributes'])
    dest['scene_ids']=list(dict.fromkeys(dest['scene_ids']+source['scene_ids']))
    dest['state_ids'].extend(source['state_ids'])
    for sid in source['state_ids']:net['states'][sid]['entity_id']=right
    for row in net['transitions']:
        if row['entity_id']==left:row['entity_id']=right
    for row in net['relations']:
        for k in ('subject','object'):
            if row[k]==left:row[k]=right
    # A text merge cannot justify a self-relation; keep it as a diagnostic.
    self_edges=[r for r in net['relations'] if r['subject']==r['object']]
    if self_edges:
        net.setdefault('identity_diagnostics',[]).extend(self_edges)
        net['relations']=[r for r in net['relations'] if r['subject']!=r['object']]
        valid={r['id'] for r in net['relations']}
        for s in net['states'].values():s['relation_ids']=[r for r in s['relation_ids'] if r in valid]
    net['aliases'][left]=right;del net['entities'][left]


@metered_cpu('caption_temporal_identity_transfer')
def cross_align(net,new_ids,previous_ids,captions,meter,target):
    from pathlib import Path
    def graph(ids):
        entities=[];refs=set()
        for eid in dict.fromkeys(canonical(net,eid) for eid in ids):
            e=net['entities'][eid]
            cited=list(dict.fromkeys(c for sid in e['state_ids'] for c in net['states'][sid]['source_caption_ids']))
            entities.append(dict(id=eid,label=e['label'],caption_ids=cited));refs.update(cited)
        return dict(entities=entities,captions=[deepcopy(c) for c in captions if c['id'] in refs])
    left,right=graph(new_ids),graph(previous_ids)
    if not left['entities'] or not right['entities']:return
    report,_=meter.json('scene_correspondence_lineage',dict(left=left,right=right),('matches',))
    matches=validate_matches(report,left,right)
    # Competing one-to-many identities stay unresolved; no state transfer occurs.
    same=[m for m in matches if m['decision']=='same']
    for match in matches:
        match=deepcopy(match);a,b=match['left_entity'],match['right_entity']
        if match['decision']=='same' and (sum(m['left_entity']==a for m in same)>1 or
                                         sum(m['right_entity']==b for m in same)>1):
            match.update(decision='unknown',relation='unknown',status='unresolved',reason='competing caption identity candidates')
        net['identities'].append(dict(match,source_type='caption'))
        if match['decision']=='same':_merge_caption_identity(net,a,b)
        elif match.get('relation') in ('derived_from','part_of','assembled_into'):
            net['lineage'].append(dict(child=canonical(net,a),parent=canonical(net,b),
                relation=match['relation'],source_caption_ids=match['caption_ids'],status='text_inferred'))
    write_json(Path(target)/'network.json',net)


def compact_context(context,net):
    """Keep original caption references beside the bounded QA retrieval view."""
    view=deepcopy(context);view['source_type']='caption'
    view['evidence_policy']='Assertions extracted from original timestamped summaries; identities are text inferences, not pixel-verified facts. Unreported changes and persistence remain unknown.'
    chosen={e['id'] for e in view['entities']}
    view['identities']=[deepcopy(r) for r in net['identities']
                        if canonical(net,r['left_entity']) in chosen and canonical(net,r['right_entity']) in chosen]
    for r in view['identities']:
        r['left_entity']=canonical(net,r['left_entity']);r['right_entity']=canonical(net,r['right_entity'])
    for e in view['entities']:
        e['state_ids']=[sid for sid in e['state_ids'] if any(s['id']==sid for s in view['states'])]
    return view


def validate_network(net,captions):
    """Audit source type and citations independently of model success/accuracy."""
    allowed={c['id']:c for c in captions}
    if net.get('source_type')!='caption':raise ValueError('Expected a caption-derived network')
    def check(value):
        if isinstance(value,dict):
            if set(value)&{'frame_id','frame_ids','source_frame_ids','frames'}:
                raise ValueError('Visual evidence reference in summary-derived network')
            for k,v in value.items():
                if k in ('source_status','status') and isinstance(v,str) and 'visual' in v:
                    raise ValueError('Visual assertion status in summary-derived network')
                if k in ('source_caption_ids','caption_ids') and not set(v)<=allowed.keys():
                    raise ValueError('Unknown source caption citation')
                check(v)
        elif isinstance(value,list):
            for item in value:check(item)
    check(net)
    if any(allowed.get(cid)!=c for cid,c in net.get('source_captions',{}).items()):
        raise ValueError('Original summary text or timestamps changed')
    return True
