"""V4 uses an untrusted proposal and explicit-text pairwise evidence checks.

Readiness governs retrieval only. No proposal or reasoning enters final readers.
"""
from . import q_repair as v3
from .schemas import enum,record,short_text,validate_output
import re

VERSION='Q-comparative-evidence-keep-only-v4'
PLUS_VERSION='Qplus-comparative-evidence-keep-only-v4'
BATCH='q4_independent_batch'
NEW_OPS=('q4_propose','q4_pair')
OPS=(*NEW_OPS,'q3_filter','q3_navigate','q3_reconsider')
WINDOW=v3.WINDOW
MAX_NEW_SCENES=v3.MAX_NEW_SCENES
views=v3.views
windows=v3.windows
decision=v3.decision
temporal_regions=v3.temporal_regions
available_scenes=v3.available_scenes
read_scene=v3.read_scene

PROPOSE_PROMPT='''Nominate the answer option best supported by the observations, or undetermined
if no option has relevant evidence. Each letter is explicitly bound to its option text.
This is an untrusted proposal for further checking, not a sufficiency verdict or final
answer. Options and question premises are not facts. Cite up to two useful observation
IDs, including relevant partial evidence, or none. For each required time region cite
a question-relevant observation from that region or none. Do not invent observations.
Return JSON only.'''

PAIR_PROMPT='''Choose A, B, or U by comparing the TWO complete option texts with the observations.
A: option A has positive distinguishing evidence and its essential concrete claims
are supported. B: the same is true for option B. U: the observations do not resolve
which option is supported, or an essential concrete part is missing.

Neither A nor B is preferred. Check the actual difference between them, including
shared essential claims. If only their shared first step is observed, choose U.
A clear observed action can favor one option without proving the other never occurs.
Do not require formal disproof of the other option. Ordinary paraphrase is allowed.
Function/overall activity may be reasonably inferred from observed use and actions.
Do not demand the caption literally state such a function. However, do NOT invent
new actions, participants, objects, phases, locations, later outcomes or specific
private intentions. Generic handling does not establish a specific destination.
Respect negation and modality; possible is not certain. No mention is not proof of
non-occurrence. A question's premise and either option's story are not observations.
Incidental evaluative wording need not be quoted; extra concrete facts remain material.

For A/B, cite actual distinguishing observation IDs, use basis direct, inference,
or conflict, and gap=none with missing="". For U, use basis unknown, a gap category,
and a SHORT question naming the missing distinguishing fact (at most 12 words).
For each required time region cite a question-relevant observation FROM that region
or none. An empty required-region list adds no time requirement. Return JSON only.'''


def letters(request):return [chr(65+i) for i in range(len(request['options']))]


def validate_request(operation,request):
    if operation not in NEW_OPS:
        if operation not in OPS:raise ValueError('Unknown V4 operation')
        return v3.validate_request(operation,request)
    required={'question','options','observations','regions'}
    if operation=='q4_pair':required|={'candidate_index','alternative_index'}
    if set(request)!=required:raise ValueError('Invalid V4 request fields')
    if not 2<=len(request['options'])<=8 or any(not isinstance(o,str) or not o for o in request['options']):
        raise ValueError('V4 needs 2..8 literal options')
    v3.validate_request('q3_coverage',{k:request[k] for k in ('question','options','observations','regions')})
    if 'none' in {s['source_id'] for s in request['observations']}:raise ValueError('Reserved observation ID')
    names=[z['id'] for z in request['regions']]
    if len(names)!=len(set(names)):raise ValueError('Duplicate temporal region')
    for z in request['regions']:
        if len(z['interval'])!=2 or z['interval'][0]>=z['interval'][1]:raise ValueError('Invalid temporal region')
    if operation=='q4_pair':
        if not all(type(request[k]) is int and 0<=request[k]<len(request['options']) for k in ['candidate_index','alternative_index']):raise ValueError('Invalid option index')
        if request['candidate_index']==request['alternative_index']:raise ValueError('Self comparison')


def time_schema(request):
    return record({z['id']:enum(['none',*[s['source_id'] for s in request['observations']
        if any(a<z['interval'][1] and b>z['interval'][0] for a,b in s['intervals'])]]) for z in request['regions']})


def schema(operation,request):
    validate_request(operation,request)
    if operation not in NEW_OPS:return v3.schema(operation,request)
    ids=[s['source_id'] for s in request['observations']]
    if operation=='q4_propose':
        return record(dict(choice=enum([*letters(request),'undetermined'] if ids else ['undetermined']),
            w0=enum(['none',*ids]),w1=enum(['none',*ids]),temporal=time_schema(request)))
    variants=[]
    if ids:
        variants.append(record(dict(choice=enum(['A','B']),w0=enum(ids),w1=enum(['none',*ids]),
            basis=enum(['direct','inference','conflict']),gap=enum(['none']),missing=enum(['']),temporal=time_schema(request))))
    variants.append(record(dict(choice=enum(['U']),w0=enum(['none',*ids]),w1=enum(['none',*ids]),basis=enum(['unknown']),
        gap=enum(['action','actor','time','relation','object','function','ambiguity']),
        missing=dict(type='string',minLength=1,maxLength=100),temporal=time_schema(request))))
    return {'anyOf':variants}


def messages(operation,request):
    import json
    validate_request(operation,request)
    wire=dict(question=request['question'],observations=request['observations'],required_time_regions=request['regions'])
    temporal={z['id']:'source_id in region or none' for z in request['regions']}
    if operation=='q4_propose':
        wire['options']=dict(zip(letters(request),request['options']))
        display=dict(choice='option letter or undetermined',w0='source_id or none',w1='source_id or none',temporal=temporal)
        prompt=PROPOSE_PROMPT
    else:
        wire['option_A']=request['options'][request['candidate_index']]
        wire['option_B']=request['options'][request['alternative_index']]
        display=dict(choice='A|B|U',w0='source_id or none',w1='source_id or none',basis='direct|inference|conflict|unknown',
            gap='none|action|actor|time|relation|object|function|ambiguity',missing='short missing-fact question, empty for A/B',temporal=temporal)
        prompt=PAIR_PROMPT
    return [dict(role='system',content=prompt+'\nRequired output shape:\n'+json.dumps(display,separators=(',',':'))),
            dict(role='user',content=json.dumps(wire,ensure_ascii=False))]


def validate_verdict(operation,request,value):
    validate_output(value,schema(operation,request));return value


def qualified(text):
    # Conservative lexical guard, not a complete modality parser. Negation is
    # not uncertainty. Facts remain intact in filtering and final reader inputs.
    return bool(re.search(r'\b(may|might|could|perhaps|possibly|maybe|apparently|seems?|appears?|if)\b',text,re.I))


def uncertainty_block(request,value):
    if value['choice'] not in ('A','B'):return False
    index=request['candidate_index'] if value['choice']=='A' else request['alternative_index']
    if qualified(request['options'][index]):return False
    ids={value[k] for k in ('w0','w1') if value[k]!='none'}
    texts={s['source_id']:s['text'] for s in request['observations']}
    return bool(ids) and all(qualified(texts[sid]) for sid in ids)


def certificate(assessment):
    p=assessment['proposal'];pairs=assessment['pairs'];labels=assessment['option_labels'];chosen=p['choice'];problems=[]
    if chosen not in labels:problems.append('no_candidate')
    expected=set(labels)-{chosen} if chosen in labels else set(labels)
    if set(pairs)!=expected:problems.append('incomplete_comparisons')
    if any(v['choice']!='A' or v['gap']!='none' for v in pairs.values()):problems.append('unresolved_or_stronger_alternative')
    if any(assessment.get('qualification_blocks',{}).values()):problems.append('qualified_only_evidence_for_factual_choice')
    temporal=[p['temporal'],*[x['temporal'] for x in pairs.values()]]
    missing=[z['id'] for z in assessment['regions'] if any(t.get(z['id'],'none')=='none' for t in temporal)]
    if missing:problems.append('missing_required_time')
    if p['w0']=='none' and p['w1']=='none':problems.append('no_proposed_evidence')
    return dict(sufficient=not problems,readiness_meaning='relative evidence readiness, not proof that every rival is false',
                missing_regions=missing,blocking_reasons=problems,assessment=assessment)


def witness_ids(audit):
    value=audit['assessment'];parts=[value['proposal'],*value['pairs'].values()];result=set()
    for x in parts:
        result.update(x[k] for k in ('w0','w1') if x[k]!='none')
        result.update(v for v in x['temporal'].values() if v!='none')
    return result
