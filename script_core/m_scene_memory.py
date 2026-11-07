"""M: summary-led, one-scene-at-a-time navigation followed by L evidence planning."""
from copy import deepcopy

from . import i_evidence_planner as parent, merged_lazy_memory as merged
from .schemas import record, enum, short_text, validate_output
from .state import write_json
from .accounting import metered_cpu

MODE = 'scene_planned_script'
VERSION = 'M-L-neutral-query-joint-candidates-v8'
from .m_candidate_selection import OPERATION as SELECT_OPERATION, KEYS as SELECT_KEYS, normalize_selection, prepare_inputs as prepare_selection_inputs, review_scenes
PRESENTATION_VERSION = 'M-frames-adjacent-v2-no-source-label'

SELECT_PROMPT = '''Read the question, ALL options and every scene's overall description.
FIRST establish a CLOSED set of genuinely relevant candidate scenes. For EVERY scene ID,
return scene_decisions[ID]={relevance:candidate|excluded,priority:integer,reason:...}.
Candidate priority is 1..100, larger meaning more likely to resolve the question's
distinguishing details; this is a ranking score, NOT a calibrated probability.
Excluded scenes use priority=0. Return a short overall reason as well. Do NOT return
a candidate_order array. The program derives the queue only from candidate decisions,
sorts descending priority, and breaks equal-priority ties by original directory order.
Exclude unrelated background, objects and activities; do not include scenes merely because
the answer is not yet known. No fixed candidate count or minimum: an empty set is valid
when no summary suggests useful evidence. Do not fill a quota or automatically select all.
Only this candidate set may ever be opened. Its first item is inspected first; subsequent
items are inspected in this same ranked order only if more evidence is needed.
Scene descriptions are navigation hints, never answer evidence. Do not select by video thirds,
timeline coverage, a required scene count, or option premises treated as facts.'''

REVIEW_PROMPT = '''Inspect the CURRENT scene's complete original captions in detail, together
with previous_scene_captions, already grounded observations, the question and ALL options.
Earlier opened captions remain available for comparison; local extraction IDs belong ONLY to the current scene.
FIRST return option_comparison: name the two most confusable choices, the exact detail
that separates them, and whether that detail is actually present in observed captions.
Compare ALL differing clauses, not just the last clause or the most plausible story.
Example: an observed drawer is open AFTER an action. Choices 'locked then open' and
'closed then open' share the observed endpoint. BOTH remain unresolved without the
earlier condition. Opening a drawer does not establish whether it was locked.
The gap is the earlier condition. If absent from the current scene, follow the relevant
unopened scene, not every scene. If one observed caption says both conditions, stop.
Then return option_assessments, evaluating EVERY full option independently using ONLY
current/previous observed captions, never directory hints or the option's own premises.
Each O-index has status supported/ruled_out/unresolved, observed source_caption_ids and reason.
Supported means the option's distinguishing content is grounded. Ruled_out means observed
evidence contradicts a distinguishing clause. Missing evidence is unresolved, not ruled_out.
For mutually exclusive values of the SAME observed slot, an explicit value also rules out
conflicting values: cite that same observed caption. Omission alone does not contradict a claim.
A partial match to one option is NOT enough when another option remains compatible.
Do not infer an unobserved starting condition, tool, action or goal from a later result.
Declare sufficient ONLY with exactly one supported option and all others ruled_out.
One caption can legitimately distinguish every option; then stop without more scenes.
Otherwise state the concrete distinction still unresolved; inspect locally or follow the gap.
If sufficiency_feedback is supplied, the independent check found an unresolved alternative.
Set sufficient=false and address its missing_detail; do not repeat sufficient=true until
new local extraction or a newly opened scene supplies additional evidence. Stop with
sufficient=false if no useful evidence action remains.
Then return sufficient, reason, gap, action, scene_id, caption_ids, entity_ids, focus, fields,
current_scene_assessment and local_unavailable_reason.
If observed evidence already distinguishes the choices, stop immediately: sufficient=true,
action=stop, scene_id=stop, gap="", current_scene_assessment=sufficient, empty arrays.
Otherwise explicitly identify the missing evidence that would distinguish remaining choices.
Prefer extract when the current scene can supply more relevant detail: assessment=needs_local_detail,
select current caption IDs, needed fields and focus. Prior extraction covers only those fields and sources.
Use read_scene ONLY when the current scene cannot supply the missing evidence: assessment=
cannot_resolve_locally, local_unavailable_reason explains why, and choose ONE allowed scene
whose overall description is most relevant to that gap. Empty caption/entity/focus/fields arrays.
If no useful action remains, stop with sufficient=false and assessment=no_useful_step.
Never open another scene just to cover time, video thirds, every scene or at least two scenes.
A global/count/comparison question can expose a concrete semantic gap, but its type alone
does not mandate any scene visit. Directory summaries are not observed answer evidence.
Question/option premises and absent mentions are not facts. Never repeat the same extraction.
Use literal existing IDs; keep explanations under 160 characters and return complete JSON.'''

# The model wire format uses an enum discriminator, allowing LMFE to couple the
# entire decision. Internal reviews still expose the established boolean flag.
REVIEW_PROMPT += '''\nSerialization: return action FIRST, then evidence_status (sufficient or insufficient),
supported_option (O-index when sufficient, otherwise unresolved), option_comparison,
option_assessments, reason, gap, scene_id, caption_ids, entity_ids,
focus, fields, current_scene_assessment, local_unavailable_reason. Do not output sufficient.
evidence_status replaces the boolean sufficient discussed above: sufficient means true;
insufficient means false. An insufficient decision MUST name a nonempty concrete gap.
stop+sufficient uses assessment=sufficient and empty gap. stop+insufficient uses
assessment=no_useful_step and retains the unresolved gap. extract/read_scene are insufficient.'''
REVIEW_PROMPT += '''\nThe candidate set is CLOSED. allowed_scene_ids contains only the next ranked
unopened candidate, never the rest of the video. If local evidence remains insufficient,
inspect that candidate next, with a concrete gap and local inability reason. Do not stop
as no_useful_step while an uninspected candidate remains. Once all candidates are checked,
stop as insufficient with the unresolved gap; do not expand outside the set. Enough evidence
may stop immediately, before exhausting candidates. Resource limits are separate stop reasons.'''

SUFFICIENCY_PROMPT = '''Independently compare the proposed candidate with ONE alternative.
The candidate is a proposal, NOT a known correct answer. Read every differing clause of
both choices against ONLY the observed captions. Can the alternative actually be ruled out?
Return reason FIRST, verdict (ruled_out or unresolved), source_caption_ids, missing_detail.
If both choices fit the observed facts, or a difference is unobserved, return unresolved and
name that exact missing distinction. Do not assume missing starting states, tools or causes.
Example: 'locked then open' and 'closed then open' both fit a drawer observed only open
afterwards. The starting condition is unknown; the alternative is NOT ruled out.
ruled_out requires an observed conflicting value for the SAME role or event; cite the caption.
Directory summaries, question/option premises and absence of mention are not observations.
No temporal coverage or required number of scenes is involved. Keep reason/missing_detail concise.'''

RESOLVE_PROMPT = '''Resolve the explicit missing_detail in sufficiency_feedback.
The evidence is currently UNRESOLVED; this operation chooses the next evidence action,
not an answer or a new sufficiency verdict. Return resolution_status=unresolved, reason,
gap, action, scene_id, caption_ids, entity_ids, focus, fields, current_scene_assessment,
local_unavailable_reason. No option_assessments, option_comparison or sufficient field.
Read the current scene's complete captions and all previously opened captions.
If current sources can supply the missing detail, choose extract with current caption IDs,
focus and requested fields, scene_id=stop, assessment=needs_local_detail.
If the detail is absent locally, choose read_scene and exactly one allowed unopened scene
whose directory description addresses this gap. Give a concrete gap and local inability
reason, assessment=cannot_resolve_locally, empty extraction arrays.
If no useful evidence action remains, choose stop, scene_id=stop, assessment=no_useful_step,
retain the concrete gap and leave extraction arrays empty. Never fill missing facts from
option premises or directory hints. Never expand for time coverage or a minimum scene count.'''


def coverage_status():
    return dict(enforced=False, met=None, missing=[], sample_times=[],
                status='No temporal coverage gate; sufficiency follows observed evidence and explicit gaps')


def output_schema(operation, inputs):
    if operation == 'm_resolve_gap':
        from .adaptive_transition import output_schema as transition_schema
        schema=transition_schema('transition_next',inputs)
        schema['properties'].pop('sufficient')
        schema['properties']=dict(resolution_status=enum(['unresolved']),**schema['properties'])
        schema['properties']['gap']['minLength']=1
        schema['properties'].update(current_scene_assessment=enum([
            'needs_local_detail','cannot_resolve_locally','no_useful_step']),
            local_unavailable_reason=short_text(160))
        schema['required']=list(schema['properties'])
        variants=[]
        for action,assessment in [('extract','needs_local_detail'),
                                  ('read_scene','cannot_resolve_locally'),('stop','no_useful_step')]:
            if action=='read_scene' and not inputs['allowed_scene_ids']:continue
            if inputs.get('candidate_boundary_enforced') and inputs['allowed_scene_ids'] and action=='stop':continue
            if inputs.get('local_scope_exhausted') and action=='extract':continue
            variant=deepcopy(schema)
            props=variant['properties']
            props['action']=enum([action])
            props['current_scene_assessment']=enum([assessment])
            props['scene_id']=enum(inputs['allowed_scene_ids'] if action=='read_scene' else ['stop'])
            if action=='read_scene':props['local_unavailable_reason']['minLength']=1
            for key in ('caption_ids','entity_ids','focus','fields'):
                if action!='extract':props[key]['maxItems']=0
                elif key!='entity_ids':props[key]['minItems']=1
            variants.append(variant)
        return dict(anyOf=variants)
    if operation == 'm_sufficiency_check':
        from .schemas import array
        ids = [c['id'] for c in inputs['captions']]
        refs = array(enum(ids),3)
        variants = []
        for ruled_out in (True,False):
            sources=deepcopy(refs)
            if ruled_out:sources['minItems']=1
            gap=short_text(160)
            if not ruled_out:gap['minLength']=1
            else:gap=enum([''])
            variants.append(record(dict(reason=short_text(160),
                verdict=enum(['ruled_out' if ruled_out else 'unresolved']),
                source_caption_ids=sources,missing_detail=gap)))
        return dict(anyOf=variants)
    if operation == 'm_select_scene':
        ids=[s['id'] for s in inputs['directory']]
        explanation=short_text(100);explanation['minLength']=1
        item=dict(anyOf=[record(dict(relevance=enum(['candidate']),
            priority=dict(type='integer',enum=list(range(1,101))),reason=deepcopy(explanation))),
            record(dict(relevance=enum(['excluded']),priority=dict(type='integer',enum=[0]),reason=deepcopy(explanation)))])
        return record(dict(scene_decisions=record({sid:deepcopy(item) for sid in ids}),reason=short_text(160)))
    if operation == 'm_transition_next':
        from .adaptive_transition import output_schema as transition_schema
        schema = transition_schema('transition_next', inputs)
        from .schemas import array
        ids = list(dict.fromkeys(c['id'] for c in
            inputs.get('captions', []) + inputs.get('previous_scene_captions', [])))
        variants = []
        for status in ('supported','ruled_out','unresolved'):
            refs = array(enum(ids),3)
            if status != 'unresolved':
                refs['minItems'] = 1
            variants.append(record(dict(reason=short_text(100),status=enum([status]),
                source_caption_ids=refs)))
        assessments = record({f'O{i}':{'anyOf':deepcopy(variants)}
                              for i in range(len(inputs['options']))})
        schema['properties'] = dict(option_comparison=short_text(160),
                                   option_assessments=assessments, **schema['properties'])
        schema['properties'].update(current_scene_assessment=enum([
            'sufficient', 'needs_local_detail', 'cannot_resolve_locally', 'no_useful_step']),
            local_unavailable_reason=short_text(160))
        schema['required'] = list(schema['properties'])
        schema['properties'].pop('sufficient')
        variants=[]
        for action,status,assessment in [('extract','insufficient','needs_local_detail'),
                ('read_scene','insufficient','cannot_resolve_locally'),
                ('stop','sufficient','sufficient'),('stop','insufficient','no_useful_step')]:
            if action=='read_scene' and not inputs['allowed_scene_ids']:continue
            if (inputs.get('candidate_boundary_enforced') and inputs['allowed_scene_ids']
                    and action=='stop' and status=='insufficient'):continue
            props=deepcopy(schema['properties'])
            props.pop('action')
            props=dict(action=enum([action]),evidence_status=enum([status]),**props)
            props['current_scene_assessment']=enum([assessment])
            props['scene_id']=enum(inputs['allowed_scene_ids'] if action=='read_scene' else ['stop'])
            if status=='sufficient':props['gap']['maxLength']=0
            else:props['gap']['minLength']=1
            if action=='read_scene':props['local_unavailable_reason']['minLength']=1
            for key in ('caption_ids','entity_ids','focus','fields'):
                if action!='extract':props[key]['maxItems']=0
                elif key!='entity_ids':props[key]['minItems']=1
            for selected in range(len(inputs['options'])) if status=='sufficient' else [None]:
                branch=deepcopy(props)
                if selected is not None:
                    for i,field in enumerate(branch['option_assessments']['properties'].values()):
                        wanted='supported' if i==selected else 'ruled_out'
                        chosen=next(v for v in field['anyOf']
                            if v['properties']['status']['enum']==[wanted])
                        field.clear();field.update(chosen)
                first={k:branch.pop(k) for k in ('action','evidence_status')}
                branch=dict(**first,supported_option=enum([f'O{selected}' if selected is not None else 'unresolved']),**branch)
                variants.append(record(branch))
        return dict(anyOf=variants)
    raise ValueError('Unknown M navigation operation: ' + operation)


def validate_candidates(selection,inputs):
    validate_output(selection,output_schema('m_select_scene',inputs))
    positions={s['id']:i for i,s in enumerate(inputs['directory'])}
    order=[sid for sid,item in selection['scene_decisions'].items() if item['relevance']=='candidate']
    if not selection['reason'].strip() or any(not item['reason'].strip()
            for item in selection['scene_decisions'].values()):
        raise ValueError('M candidate inclusion/exclusion needs explicit reasons')
    return sorted(order,key=lambda sid:(-selection['scene_decisions'][sid]['priority'],positions[sid]))


def temporal_attribute_sentence(label,attribute):
    import json
    from .adaptive_transition import FIELDS
    required={'field','value','source_caption_ids'}
    if (not required<=attribute.keys() or attribute.keys()-required-{'observed_at','source_status','scene_id','batch_id'}
            or attribute['field'] not in FIELDS or not isinstance(attribute['value'],str)
            or not attribute['value'].strip() or not isinstance(attribute['source_caption_ids'],list)
            or any(not isinstance(ref,str) for ref in attribute['source_caption_ids'])
            or any(not isinstance(attribute[k],str) for k in ('source_status','scene_id','batch_id') if k in attribute)):
        raise ValueError('Unsupported temporal attribute; explicit source-bound verbalization required')
    return ('The text memory records attribute '+json.dumps(attribute['field'],ensure_ascii=False)+
        ' with value '+json.dumps(attribute['value'],ensure_ascii=False)+' for '+label+'.')


def build_pool(memory,original_input,parts):
    return parent.build_pool(memory,original_input,parts,
                             temporal_attribute_sentence=temporal_attribute_sentence)


def answer_without_candidates(packet,bundle,backend,cfg,target,settings):
    """Preserve the choice-answer protocol, explicitly without observed evidence."""
    from .budget import BudgetLedger, Meter
    from .config import digest
    from script.controlled_memory_io import parts
    target.mkdir(parents=True,exist_ok=True)
    ledger=BudgetLedger(settings['online_seconds'],initialization_cost=bundle['preparation_seconds'],
        answer_reserve=cfg['budget']['final_answer_reserve'],calls_path=target/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,target/'raw_calls')
    result=dict(arm='adaptive_partial_temporal',method=VERSION,memory_variant='M',
        question_id=packet['question']['question_id'],answer=None,error=None,
        opened_scenes=[],selected_seconds=[],paired_evidence_hash=digest(bundle),
        incomplete=[dict(stage='candidate_selection',reason='No relevant candidate scenes; evidence insufficient')],
        source_incomplete=deepcopy(bundle['source_incomplete']),
        supplemental_visual=deepcopy(bundle['supplemental_visual']),
        shared_preparation_seconds=bundle['preparation_seconds'],
        qa_evidence=dict(variant='M',observed_source_rows=0,planner_skipped_no_evidence=True))
    try:
        qa=deepcopy(bundle['qa_input'])
        prefix,_,suffix=parts(qa)
        qa['prompts'][0]=prefix+'No relevant candidate scene was selected. No detailed source observations were read. Evidence is insufficient; do not infer evidence from option premises.'+suffix
        write_json(target/'M/empty_evidence.json',dict(reason='no_relevant_candidates',
            observed_sources=[],planner_called=False,evidence_status='insufficient'))
        write_json(target/'M/final_input.json',qa)
        checked,_=meter.call('temporal_qa_check',qa,final=True)
        answer,_=meter.call('m_answer',dict(inputs=qa,options=packet['question']['options']),final=True)
        result.update(answer=answer['data'],qa_input_tokens=checked['input_tokens'])
    except Exception as exc:
        result['error']=dict(type=type(exc).__name__,message=str(exc))
    finally:
        result['cost_summary']=ledger.summary()
        write_json(target/'result.json',result)
        meter.close()
    return result


def validate_step(plan, inputs):
    """A scene expansion needs a concrete gap and an explicit local assessment."""
    blocked=bool(inputs.get('sufficiency_blocked'))
    validate_output(plan, output_schema('m_resolve_gap' if blocked else 'm_transition_next', inputs))
    if blocked:
        # Translate the explicit enum decision into the internal review flag;
        # the raw model output remains recorded with resolution_status.
        plan['sufficient']=False
    else:
        plan['sufficient']=plan['evidence_status']=='sufficient'
    assessments = plan.get('option_assessments',{})
    for item in assessments.values():
        if not item['reason'].strip():
            raise ValueError('M option assessment needs a reason')
        if item['status'] != 'unresolved' and not item['source_caption_ids']:
            raise ValueError('M supported/ruled-out options need observed caption sources')
    if plan['sufficient']:
        statuses = [item['status'] for item in assessments.values()]
        if statuses.count('supported') != 1 or statuses.count('ruled_out') != len(statuses)-1:
            raise ValueError('M sufficiency requires one supported option and every alternative ruled out')
    action = plan['action']
    expected = ('sufficient' if plan['sufficient'] else
        dict(extract='needs_local_detail', read_scene='cannot_resolve_locally', stop='no_useful_step')[action])
    if plan['current_scene_assessment'] != expected:
        raise ValueError('M action contradicts its current-scene assessment')
    if plan['sufficient'] and (action != 'stop' or plan['gap'].strip()):
        raise ValueError('M sufficient evidence must stop without an unresolved gap')
    if not plan['sufficient'] and not plan['gap'].strip():
        raise ValueError('M insufficient evidence requires an explicit gap')
    if action == 'read_scene':
        if not plan['local_unavailable_reason'].strip() or plan['scene_id'] not in inputs['allowed_scene_ids']:
            raise ValueError('M expansion needs a local inability reason and one unopened scene')
    elif plan['scene_id'] != 'stop':
        raise ValueError('M local inspection/stop cannot open a scene')
    if action != 'extract' and any(plan[k] for k in ('caption_ids', 'entity_ids', 'focus', 'fields')):
        raise ValueError('M scene navigation cannot also request extraction')
    if action == 'extract' and not all(plan[k] for k in ('caption_ids', 'focus', 'fields')):
        raise ValueError('M local inspection requires sources, focus and fields')


def check_sufficiency(plan, inputs, meter):
    """Separate each competing choice so a partial match cannot hide an alternative."""
    candidate = next(int(k[1:]) for k,v in plan['option_assessments'].items()
                     if v['status']=='supported')
    checks=[]
    for i,alternative in enumerate(inputs['options']):
        if i==candidate:continue
        request=dict(question=inputs['question'],candidate_option=inputs['options'][candidate],
            alternative_option=alternative,captions=inputs['captions']+inputs.get('previous_scene_captions',[]),
            source_type='caption')
        checked,_=meter.json('m_sufficiency_check',request,
            ('reason','verdict','source_caption_ids','missing_detail'))
        validate_output(checked,output_schema('m_sufficiency_check',request))
        checks.append(dict(option_id=f'O{i}',alternative_ruled_out=checked['verdict']=='ruled_out',**checked))
    return checks


def render(pool, plan):
    """Short frame labels; inherit L's exact-statement adjacent interval audit."""
    _, audit = parent.render(pool, plan)
    records = {r['id']: r for r in pool['records']}
    lines = [parent.POLICY]
    for group in audit['groups']:
        statements = []
        for display in group['interval_display']:
            record = records[display['record_id']]
            times = ', '.join('['+','.join(str(t) for t in pair)+')'
                              for pair in display['display_intervals']) or 'time unspecified'
            label = '' if record['qualification'] == 'source assertion' else ' ('+record['qualification']+')'
            statements.append('Frames '+times+label+': '+record['text'])
        lines.append(' '.join(statements))
    audit.update(presentation_version=PRESENTATION_VERSION, interval_label='Frames',
        display_qualifications={rid: records[rid]['qualification']
                               for group in audit['groups'] for rid in group['record_ids']})
    return '\n'.join(lines), audit


@metered_cpu('m_evidence_materialization')
def project_evidence(qa, payload, query, meter, target):
    """Run the exact L pool/decision/audit/render contracts on newly retrieved H/I data."""
    from script.controlled_memory_io import parts
    target.mkdir(parents=True, exist_ok=True)
    memory = merged.materialize(payload)
    pool = build_pool(memory, qa, parts)
    inputs = parent.planner_input(query, pool, query.get('id') or query['question_id'])
    write_json(target/'pool.json', pool)
    write_json(target/'planner_input.json', inputs)
    output, _ = meter.call('m_evidence_plan', dict(inputs=inputs, schema=parent.plan_schema(pool)))
    # Decode raw text directly: duplicate keys must never disappear through json.loads.
    write_json(target/'planner_output.json', output)
    plan = parent.decode_plan(output['raw'], pool)
    description, audit = render(pool, plan)
    final = parent.final_input(qa, description, parts)
    write_json(target/'plan.json', plan)
    write_json(target/'audit.json', audit)
    write_json(target/'final_input.json', final)
    return final, dict(version=VERSION, parent='L', pool_records=len(pool['records']), audit=deepcopy(audit))
