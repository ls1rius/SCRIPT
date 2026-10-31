"""Temporal/cross-scene organization of adaptive_partial's fixed evidence.

No retrieval, entity extraction or image access occurs in this paired treatment.
Only existing observations receive conservative continuity judgments; identity
links reuse scheme 4's cited-caption validation and state assembler.
An explicit final-input mode can replace the full caption list with bounded
original quotations supporting the same memory; upstream evidence stays fixed.
"""
from copy import deepcopy
from pathlib import Path

from . import adaptive_entity as temporal, adaptive_transition as partial
from .accounting import metered_cpu
from .budget import BudgetLedger, Meter, ResourceExhausted, ToolFailure, exploration_exhausted
from .config import digest
from .schemas import array, enum, record, short_text, validate_output
from .state import write_json

VERSION = 'whole-scene-partial-temporal-v2'


def output_schema(inputs):
    variants = []
    for pair in inputs['pairs']:
        refs = array(enum([pair['before']['caption_id'], pair['after']['caption_id']]), 2)
        refs['minItems'] = 2
        variants.append(record(dict(pair_id=enum([pair['id']]),
            continuity=enum(['same', 'changed', 'uncertain']), source_caption_ids=refs,
            reason=short_text(80))))
    decisions = array({'anyOf': variants}, len(variants)) if variants else array(record({}), 0)
    decisions['minItems'] = len(variants)
    return record(dict(decisions=decisions))


@metered_cpu('partial_temporal_source_projection')
def project(memory):
    """Copy accepted local facts, retaining IDs and every original source record."""
    staged = deepcopy(memory)
    staged.update(version=VERSION, cross_matches=[], alignment_requests=[], temporal_decisions=[])
    for observation in staged['observations'].values():
        observation['continuity'] = 'uncertain'
    temporal.rebuild_indexes(staged)
    temporal.rebuild_temporal_network(staged)
    return staged


def observation_pairs(memory):
    pairs = []
    sources = memory['source_captions']
    for eid, entity in memory['entities'].items():
        # Multiple queried fields at one source second are one dated assertion.
        unique = {}
        for o in sorted(memory['observations'].values(), key=lambda o:o['id']):
            if o['entity_id'] == eid:
                unique.setdefault(o['caption_id'], o)
        ordered = sorted(unique.values(), key=lambda o:(sources[o['caption_id']]['interval'][0], o['id']))
        for before, after in zip(ordered, ordered[1:]):
            def view(o):
                c = sources[o['caption_id']]
                return dict(observation_id=o['id'], caption_id=c['id'], interval=c['interval'], text=c['text'])
            pairs.append(dict(id=f'P{len(pairs)}', entity_id=eid, label=entity['label'],
                              before=view(before), after=view(after)))
    return pairs


def identity_schema(inputs):
    variants=[]
    for pair in inputs['pairs']:
        def cited(side):
            refs=array(enum(pair[side]['caption_ids']),3);refs['minItems']=1
            return refs
        variants.append(record(dict(pair_id=enum([pair['id']]),
            relation=enum(['same_instance','different_instance','derived_from','part_of','assembled_into','unknown']),
            left_caption_ids=cited('left'),right_caption_ids=cited('right'),reason=short_text(80))))
    decisions=array({'anyOf':variants},len(variants)) if variants else array(record({}),0)
    decisions['minItems']=len(variants)
    return record(dict(decisions=decisions))


def identity_pairs(memory,opened):
    """Compare later scene-local entities to prior scenes, once per pair."""
    result=[];entities=memory['entities'];sources=memory['source_captions']
    def view(eid):
        e=entities[eid];refs=e['mention_caption_ids']
        return dict(entity_id=eid,label=e['label'],scene_id=e['scene_id'],caption_ids=refs,
            captions=[deepcopy(sources[c]) for c in refs])
    for i,sid in enumerate(opened):
        left=[eid for eid,e in entities.items() if e['scene_id']==sid]
        right=[eid for eid,e in entities.items() if e['scene_id'] in opened[:i]]
        candidates=[(a,b) for a in left for b in right]
        candidates.sort(key=lambda pair:(entities[pair[0]]['label'].lower()!=entities[pair[1]]['label'].lower(),pair))
        for a,b in candidates[:6]:result.append(dict(id=f'I{len(result)}',left=view(a),right=view(b)))
    return result


@metered_cpu('partial_temporal_identity_commit')
def commit_identities(memory,inputs,report,call_id):
    from .scene_memory import validate_matches
    from .temporal_entity import canonical
    validate_output(report,identity_schema(inputs));expected={p['id']:p for p in inputs['pairs']}
    if len({d['pair_id'] for d in report['decisions']})!=len(expected):
        raise ValueError('Every identity candidate requires exactly one explicit judgment')
    staged=deepcopy(memory)
    for decision in report['decisions']:
        pair=expected[decision['pair_id']]
        def graph(side):
            entity=pair[side];eid=entity['entity_id'];stored=staged['entities'][eid]
            if entity['label']!=stored['label'] or entity['scene_id']!=stored['scene_id'] or entity['caption_ids']!=stored['mention_caption_ids']:
                raise ValueError('Identity entity provenance changed')
            if entity['captions']!=[staged['source_captions'][c] for c in entity['caption_ids']]:
                raise ValueError('Identity original captions changed')
            return dict(entities=[dict(id=eid,label=stored['label'],caption_ids=entity['caption_ids'])],captions=entity['captions'])
        left,right=graph('left'),graph('right')
        converted=dict(left=dict(entity=pair['left']['entity_id'],caption_ids=decision['left_caption_ids']),
            right=dict(entity=pair['right']['entity_id'],caption_ids=decision['right_caption_ids']),
            relation=decision['relation'],reason=decision['reason'])
        match=validate_matches(dict(matches=[converted]),left,right)[0]
        if match['decision']=='same':
            net=staged['temporal_network'];a=canonical(net,match['left_entity']);b=canonical(net,match['right_entity'])
            def component(root):return {e['scene_id'] for eid,e in staged['entities'].items() if canonical(net,eid)==root}
            if a!=b and component(a)&component(b):
                match.update(decision='unknown',relation='unknown',status='unresolved',reason='Competing same-scene instances')
        match.update(call_id=call_id,candidate_id=pair['id'])
        staged['cross_matches'].append(match)
        temporal.rebuild_indexes(staged);temporal.rebuild_temporal_network(staged)
    memory.clear();memory.update(staged)


@metered_cpu('partial_temporal_decision_commit')
def commit_decisions(memory, inputs, report, call_id):
    validate_output(report, output_schema(inputs))
    expected = {p['id']:p for p in inputs['pairs']}
    if len({d['pair_id'] for d in report['decisions']}) != len(expected):
        raise ValueError('Continuity decisions must cover every supplied pair exactly once')
    staged = deepcopy(memory)
    for decision in report['decisions']:
        pair = expected[decision['pair_id']]
        own = {pair['before']['caption_id'],pair['after']['caption_id']}
        if set(decision['source_caption_ids']) != own:
            raise ValueError('Continuity must cite both original observations')
        for side in ('before','after'):
            row = pair[side];actual = staged['source_captions'][row['caption_id']]
            if row['text'] != actual['text'] or row['interval'] != actual['interval']:
                raise ValueError('Temporal source assertion changed')
        continuity = decision['continuity']
        reason = decision['reason']
        if continuity == 'same' and pair['after']['interval'][0] > pair['before']['interval'][1]:
            continuity = 'uncertain'
            reason = 'Unobserved gap; continuous state is unknown'
        for o in staged['observations'].values():
            if o['entity_id'] == pair['entity_id'] and o['caption_id'] == pair['after']['caption_id']:
                o['continuity'] = continuity
                o['continuity_basis'] = 'text_inferred'
        staged['temporal_decisions'].append(dict(decision, applied_continuity=continuity,
            applied_reason=reason, entity_id=pair['entity_id'], call_id=call_id, status='text_inferred'))
    temporal.rebuild_temporal_network(staged)
    memory.clear();memory.update(staged)


def context(memory, original, query):
    if not memory['entities']:
        # Preserve the exact original QA memory for a genuine no-op treatment.
        return partial.memory_context(original)
    net = memory['temporal_network']
    for transition in net['transitions']:
        transition['status'] = 'text_inferred'
    view = temporal.memory_context(memory, query)
    view['temporal_organization_policy'] = (
        'Original caption assertions and existing local entities only. Continuity and cross-scene '
        'identity are text inferences. Unknown gaps, causes and physical identities remain unknown. '
        'Repeated actions are not automatically a single continuous state.')
    return view


def run(packet, bundle, backend, cfg, target, settings):
    from .temporal_pipeline import fit_qa_context, qa_context_inputs
    from script.run_followup_validation import direct_input
    if bundle['packet_hash'] != digest(packet):
        raise ValueError('Partial treatment source packet changed')
    if set(bundle) & {'answer','selected_option','truth','prediction','evaluation'}:
        raise ValueError('Answers are forbidden in organization inputs')
    original = bundle['memory']
    if original['source_captions'] != {c['id']:c for c in packet['captions']}:
        raise ValueError('Partial original summaries changed')
    partial.validate_memory(deepcopy(original),original['video_id'],packet['captions'],original['extraction_hash'])
    selected = bundle['selected_seconds']
    available = {packet['captions'][i]['id'] for i in selected}
    if any(ref not in available for refs in original['indexes']['entity_to_captions'].values() for ref in refs):
        raise ValueError('Partial facts cite unread evidence')
    target = Path(target);target.mkdir(parents=True,exist_ok=True)
    ledger = BudgetLedger(settings['online_seconds'], initialization_cost=bundle['preparation_seconds'],
        answer_reserve=cfg['budget']['final_answer_reserve'], calls_path=target/'calls.jsonl')
    meter = Meter(backend,ledger,cfg,target/'raw_calls')
    query = dict(packet['question'],id=packet['question']['question_id'])
    evidence_mode=cfg['query'].get('temporal_qa_evidence','full')
    method=VERSION+'-memory-quotes-v1' if evidence_mode=='memory_quotes' else VERSION
    if evidence_mode=='typed_memory_quotes':method=VERSION+'-typed-memory-quotes-v1'
    if evidence_mode in ('complete_with_quotes','complete_structured'):method='source-complete-semantic-v1-'+evidence_mode
    if evidence_mode=='state_segments':method='compact-semantic-state-segments-D-v1'
    if evidence_mode=='joint_entities':method='joint-entity-evidence-E-v1'
    if evidence_mode=='lazy_script':method='lazy-script-evidence-F-v1'
    result = dict(arm='adaptive_partial_temporal',method=method,question_id=query['id'],answer=None,error=None,
        paired_evidence_hash=digest(bundle),source_arm='adaptive_partial',
        opened_scenes=bundle['opened_scenes'],selected_seconds=selected,
        supplemental_visual=deepcopy(bundle['supplemental_visual']),new_pixel_frame_count=0,
        shared_preparation_seconds=bundle['preparation_seconds'])
    memory = None;incomplete = []
    try:
        memory = project(original)
        pairs = observation_pairs(memory)
        for start in range(0,len(pairs),6):
            inp = dict(pairs=pairs[start:start+6],source_type='caption')
            try:
                for attempt in range(2):
                    report,call = meter.json('partial_temporal_organize',inp,('decisions',))
                    try:
                        commit_decisions(memory,inp,report,call['call_id']);break
                    except ValueError as exc:
                        if attempt:raise
                        inp['validation_feedback'] = str(exc)
            except (ResourceExhausted,ToolFailure) as exc:
                if not exploration_exhausted(exc):raise
                incomplete.append(dict(stage='continuity',reason=str(exc)));break
        # Explicit candidate judgments prevent a vacuous empty match list.
        candidates=identity_pairs(memory,bundle['opened_scenes'])
        for start in range(0,len(candidates),3):
            inp=dict(pairs=candidates[start:start+3],source_type='caption')
            try:
                for attempt in range(2):
                    report,call=meter.json('partial_temporal_identity',inp,('decisions',))
                    try:
                        commit_identities(memory,inp,report,call['call_id']);break
                    except ValueError as exc:
                        if attempt:raise
                        inp['validation_feedback']=str(exc)
            except (ResourceExhausted,ToolFailure) as exc:
                if not exploration_exhausted(exc):raise
                incomplete.append(dict(stage='identity',reason=str(exc)));break
        view = context(memory,original,query)
        # Audit the unchanged upstream evidence before applying a final-input
        # intervention. The new prompt must not disable fixed-evidence checks.
        baseline_qa = qa_context_inputs(query,packet['captions'],selected,view,
            visual_observations=bundle['supplemental_visual']['observations'])
        if direct_input(baseline_qa) != direct_input(bundle['qa_input']):
            raise ValueError('Paired QA captions, options or visual supplement changed')
        if evidence_mode=='typed_memory_quotes':
            from .typed_memory import attach_observation_types
            view=attach_observation_types(view,memory)
        quote_limits=dict(max_quotes=cfg['query'].get('temporal_qa_max_quotes',12),
            quotes_per_fact=cfg['query'].get('temporal_qa_quotes_per_fact',2),
            information_types=cfg['query'].get('temporal_qa_information_types'))
        complete_mode=evidence_mode in ('complete_with_quotes','complete_structured')
        complete_payload=None
        segment_mode=evidence_mode=='state_segments'
        segment_memory=None
        entity_mode=evidence_mode in ('joint_entities','lazy_script')
        entity_memory=None
        if entity_mode:
            from .entity_evidence_qa import project as project_entities
            # Match E/F's explicit source-failure handling and single final QA.
            meter.cfg=deepcopy(cfg)
            meter.cfg['query'].update(tool_transient_retries=0,json_repair_retries=0)
            qa,entity_memory,entity_audit=project_entities(baseline_qa,packet,selected,view,
                meter,target,cfg,settings.get('memory_cache_directory'))
            checked,_=meter.call('temporal_qa_check',qa,final=True)
            tokens=checked['input_tokens']
            result['memory_variant']=entity_audit['variant']
            if entity_audit['failed_source_fields']:
                incomplete.append(dict(stage='memory_extraction',
                    reason='Failed fields retain complete original sources',
                    failed_source_fields=entity_audit['failed_source_fields']))
        elif segment_mode:
            from .semantic_memory import normalize
            from .state_segment_qa import sources, project as project_segments
            semantic_sources=sources(packet,selected,view)
            write_json(target/'semantic_sources.json',semantic_sources)
            semantic_memory,semantic_audit=normalize(semantic_sources,meter)
            write_json(target/'semantic_memory.json',semantic_memory)
            write_json(target/'semantic_audit.json',semantic_audit)
            qa,segment_memory,segment_audit=project_segments(baseline_qa,packet,selected,
                semantic_memory,semantic_sources,cfg['query']['state_segment_max_gap_seconds'],final=True)
            write_json(target/'state_segment_memory.json',segment_memory)
            write_json(target/'state_segment_audit.json',segment_audit)
            # Keep every source and the fixed memory/visual regions. An input
            # overflow fails visibly instead of changing D into another mode.
            checked,_=meter.call('temporal_qa_check',qa,final=True)
            tokens=checked['input_tokens']
            result['semantic_materialization']=semantic_audit
            if semantic_audit['batch_failures']:
                incomplete.append(dict(stage='semantic_extraction',
                    reason='Failed or unattempted fields retain their original source text',
                    category_coverage=semantic_audit['category_coverage']))
        elif complete_mode:
            from .complete_memory import source_bundle,coverage_index,compact_view,final_input
            from .normalized_memory import normalize
            complete_sources=source_bundle(packet,dict(context=view,selected_seconds=selected),
                bundle['supplemental_visual']['observations'])
            complete_graph,complete_audit=normalize(complete_sources,meter)
            coverage=coverage_index(packet,complete_sources,complete_graph)
            complete_payload=compact_view(complete_sources,complete_graph,coverage)
            write_json(target/'complete_sources.json',complete_sources)
            write_json(target/'complete_memory.json',complete_graph)
            write_json(target/'source_coverage.json',coverage)
            write_json(target/'source_archive.json',dict(captions=packet['captions'],selected_seconds=selected))
            qa=final_input(baseline_qa,packet,complete_payload,bundle['supplemental_visual']['observations'],
                evidence_mode=='complete_with_quotes',quote_limits['max_quotes'])
            context_fallback=False
            try:checked,_=meter.call('temporal_qa_check',qa,final=True)
            except ToolFailure as exc:
                if 'exceeds input allowance' not in str(exc):raise
                qa=baseline_qa;context_fallback=True
                checked,_=meter.call('temporal_qa_check',qa,final=True)
            tokens=checked['input_tokens']
            result['source_coverage']=dict(read_captions=coverage['read_captions'],covered_captions=coverage['covered_captions'],
                coverage=coverage['coverage'],context_fallback_original=context_fallback)
            result['complete_materialization']=complete_audit
        else:
            qa,view,tokens = fit_qa_context(query,packet['captions'],selected,view,meter,final=True,
                visual_observations=bundle['supplemental_visual']['observations'],
                evidence_mode=evidence_mode,**quote_limits)
        if evidence_mode=='full' and direct_input(qa) != direct_input(bundle['qa_input']):
            raise ValueError('Paired QA captions, options or visual supplement changed')
        evidence_audit=dict(mode='full',selected_caption_count=len(selected))
        final_memory=complete_payload
        if entity_mode:
            evidence_audit=entity_audit
            final_memory=entity_memory
        if segment_mode:
            evidence_audit=segment_audit
            final_memory=segment_memory
        if complete_mode:evidence_audit=dict(mode=evidence_mode,source_coverage=result['source_coverage'])
        if evidence_mode in ('memory_quotes','typed_memory_quotes'):
            from .memory_evidence import prepare_memory_input
            final_memory,final_visual,evidence_audit=prepare_memory_input(packet['captions'],selected,view,
                bundle['supplemental_visual']['observations'],quote_limits['max_quotes'],
                quote_limits['quotes_per_fact'],evidence_mode,quote_limits['information_types'])
            has_visual=any(final_visual.values()) if isinstance(final_visual,dict) else bool(final_visual)
            if ((not view.get('entities') or evidence_audit.get('no_retained_information'))
                    and not evidence_audit['quotes'] and not has_visual):
                incomplete.append(dict(stage='final_qa_evidence',
                    reason='No entity memory, supporting quotations or accepted visual observations'))
        result['qa_evidence']={k:v for k,v in evidence_audit.items() if k!='quotes'}
        write_json(target/'qa_context.json',dict(context=view,input_tokens=tokens,selected_seconds=selected,
            evidence_selection=evidence_audit,final_memory=final_memory))
        answer,_ = meter.call('videotree_qa',qa,final=True)
        result.update(answer=answer['data'],qa_input_tokens=tokens)
    except Exception as exc:
        result['error'] = dict(type=type(exc).__name__,message=str(exc))
    finally:
        if memory is not None:
            write_json(target/'network.json',memory)
            net=memory['temporal_network']
            result.update(entity_count=len(net['entities']),state_count=len(net['states']),
                transition_count=len(net['transitions']),identity_comparisons=len(net['identities']),
                temporal_pair_count=len(memory['temporal_decisions']))
        result.update(incomplete=incomplete,source_incomplete=deepcopy(bundle['source_incomplete']),
            cost_summary=ledger.summary(),incremental_processing_seconds=ledger.summary()['total']-bundle['preparation_seconds'])
        write_json(target/'result.json',result);meter.close()
    return result
