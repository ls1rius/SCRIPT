"""Coverage-preserving entity/event memory built from every already-read caption.

Raw storage, semantic materialization and final presentation have separate
coverage contracts. An old query-sufficiency decision cannot skip source storage.
"""
from collections import defaultdict
from copy import deepcopy

from .normalized_memory import evidence_units
from .memory_evidence import qa_inputs as rendered_memory_input


def source_bundle(packet,context,visual):
    selected=context['selected_seconds'];captions=packet['captions']
    if not selected:raise ValueError('Complete memory requires already-read captions')
    if len({c['id'] for c in captions})!=len(captions):raise ValueError('Duplicate original caption IDs')
    if len(set(selected))!=len(selected) or any(type(i) is not int or not 0<=i<len(captions) for i in selected):
        raise ValueError('Invalid or duplicate read-caption positions')
    quotes=[dict(caption_id=captions[i]['id'],interval=deepcopy(captions[i]['interval']),text=captions[i]['text'])
        for i in sorted(selected)]
    query=dict(packet['question'],id=packet['question']['question_id'])
    # Rebuild semantic records from original dated assertions, not from the
    # assembler's first-description-plus-many-citations representation. That
    # representation had already conflated different actions and their times.
    empty=dict(source_type='caption',entities=[],states=[],relations=[],transitions=[],identities=[],lineage=[])
    inputs=rendered_memory_input(query,empty,dict(mode='complete_sources',quotes=quotes),visual)
    bundle=evidence_units(inputs)
    expected={captions[i]['id'] for i in selected}
    represented={cid for u in bundle['units'] for cid in u['source_caption_ids']}
    if not expected<=represented:raise ValueError('Read captions disappeared before materialization')
    bundle['read_caption_ids']=[captions[i]['id'] for i in sorted(selected)]
    # The old assembler's action-containing slots are not physical states.
    bundle['structural_memory']=dict(
        prior_entity_mentions=[{k:deepcopy(e[k]) for k in ('id','label','scene_ids') if k in e}
            for e in context['context'].get('entities',[])],
        identity_hypotheses=[{k:deepcopy(v) for k,v in r.items() if k!='reason'}
            for r in context['context'].get('identities',[])],
        lineage_hypotheses=deepcopy(context['context'].get('lineage',[])),
        identity_policy='Labels and old links are hypotheses; equal names do not merge physical instances.')
    return bundle


def coverage_index(packet,bundle,normalized):
    """Every selected source must have atomic facts or a visible residual text."""
    sources={u['id']:u for u in bundle['units']}
    facts=[f for name in ('actions','attributes','states','relations') for f in normalized[name]]
    retained=[r for group in normalized['retained_evidence'].values() for r in group]
    unit_facts=defaultdict(list)
    for fact in facts:
        for uid in fact['evidence_ids']:
            if uid not in sources:raise ValueError('Foreign fact source')
            unit_facts[uid].append(fact['id'])
    retained_ids={r['id'] for r in retained}
    caps={c['id']:c for c in packet['captions']};rows={}
    for cid in bundle['read_caption_ids']:
        units=[u['id'] for u in bundle['units'] if cid in u['source_caption_ids']]
        atom_ids=list(dict.fromkeys(f for uid in units for f in unit_facts[uid]))
        residual_ids=[uid for uid in units if uid in retained_ids]
        if not atom_ids and not residual_ids:raise ValueError('Read source has neither facts nor residual text: '+cid)
        rows[cid]=dict(interval=deepcopy(caps[cid]['interval']),fact_ids=atom_ids,
            retained_unit_ids=residual_ids,status='atomic_and_residual' if atom_ids and residual_ids
                else 'atomic' if atom_ids else 'categorized_original')
    return dict(read_captions=len(rows),covered_captions=len(rows),coverage=1.,captions=rows,
        archived_captions=len(packet['captions']),unread_caption_ids=[c for c in caps if c not in rows],
        policy='Coverage is source accounting, not independent verification of semantic extraction.')


def compact_view(bundle,normalized,coverage):
    """Factor metadata without dropping values, observations or timestamps."""
    facts=[f for name in ('actions','attributes','states','relations') for f in normalized[name]]
    fact_fields=['id','kind','subject','predicate','object','value','tool','location',
        'attribute_field','evidence_ids','source_qualifiers']
    fact_rows=[[deepcopy(f.get(k)) for k in fact_fields] for f in facts]
    registry=[[u['id'],u['source_type'],u['status'],u['source_caption_ids'],
        u['source_frame_ids'],u['source_intervals']] for u in bundle['units']]
    residual=[]
    for kind,group in normalized['retained_evidence'].items():
        residual.extend([[r['id'],kind,r['text'],r['retention_reason']] for r in group])
    mentions=defaultdict(lambda:dict(roles=set(),fact_ids=[]))
    for f in facts:
        for role in ('subject','object','tool','location'):
            value=f.get(role)
            if value:
                mentions[value]['roles'].add(role);mentions[value]['fact_ids'].append(f['id'])
    entities=[dict(label=label,roles=sorted(v['roles']),fact_ids=list(dict.fromkeys(v['fact_ids'])))
        for label,v in sorted(mentions.items())]
    return dict(schema='complete-semantic-memory-v1',source_type='caption',
        entity_mention_index=entities,
        entity_policy='This is a lexical lookup index, not proof that equal labels are the same physical instance.',
        fact_fields=fact_fields,facts=fact_rows,
        source_fields=['id','source_type','status','caption_ids','frame_ids','sample_intervals'],sources=registry,
        residual_fields=['source_unit_id','information_type','original_text','retention_reason'],
        categorized_originals=residual,
        temporal_caption_order=bundle['read_caption_ids'],
        prior_identity_hypotheses=normalized['structural_memory'].get('identity_hypotheses',[]),
        prior_lineage_hypotheses=normalized['structural_memory'].get('lineage_hypotheses',[]),
        prior_entity_mentions=normalized['structural_memory'].get('prior_entity_mentions',[]),
        coverage=dict(read_captions=coverage['read_captions'],covered_captions=coverage['covered_captions']),
        evidence_policy='Actions are dated event assertions, not persistent states. Repeated observations retain '
            'separate sample intervals, not one continuous event. State facts describe conditions only. '
            'Null fields remain unknown. Source assertions and identity/causal hypotheses are not pixel verification. '
            'All already-read captions are represented by atomic fields or categorized originals; unread captions remain unknown.')


def quote_positions(packet,selected,maximum=12):
    """Deterministic time coverage; quotations are auxiliary, never a storage gate."""
    selected=sorted(selected)
    if type(maximum) is not int or maximum<1:raise ValueError('Quotation limit must be positive')
    if len(selected)<=maximum:return selected
    if maximum==1:return [selected[len(selected)//2]]
    return [selected[round(i*(len(selected)-1)/(maximum-1))] for i in range(maximum)]


def final_input(original,packet,memory,visual,include_quotes=True,maximum=12):
    """Keep the original system prompt, question/options and answer protocol."""
    from .temporal_pipeline import qa_context_inputs
    selected=[]
    if include_quotes:
        positions={c['id']:i for i,c in enumerate(packet['captions'])}
        selected=quote_positions(packet,[positions[c] for c in memory['temporal_caption_order']],maximum)
    result=qa_context_inputs(dict(packet['question'],id=packet['question']['question_id']),
        packet['captions'],selected,memory,visual_observations=visual)
    if result['head']!=original['head']:raise ValueError('Original final system instructions changed')
    # Compare the question/options portion, while allowing only description and
    # memory payload changes. The supplemental visual source is supplied intact.
    def question_block(prompt):
        return prompt.split(' Questions:',1)[1].split('\n\nTemporal entity memory (',1)[0]
    if question_block(result['prompts'][0])!=question_block(original['prompts'][0]):
        raise ValueError('Original question/options or answer instructions changed')
    return result
