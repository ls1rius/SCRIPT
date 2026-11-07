"""Q v3: literal observation windows, symmetric option audits and bounded retrieval.

No labels, preferred answers, generated evidence or cross-question state enter here.
The audit is a conservative model assessment, not a proof of video ground truth.
"""
from copy import deepcopy
import re

from . import q_evidence as q
from .schemas import enum, record, short_text, validate_output

VERSION = 'Q-event-context-keep-only-v3'
PLUS_VERSION = 'Qplus-event-context-keep-only-compact-v3'
BATCH = 'q3_independent_batch'
OPS = ('q3_filter', 'q3_option_audit', 'q3_coverage', 'q3_navigate', 'q3_reconsider')
WINDOW = 8
MAX_NEW_SCENES = 4

FILTER_PROMPT = '''Classify each observed step by its CONTRIBUTION to understanding the original question.
First read the observations together as a bounded process. Then label EVERY target ID:
- direct: directly describes a requested action, participant, object, component,
  ingredient, stage, relationship or counterevidence.
- context: an observed preparation, treatment, effect or completion step on the SAME
  work object that helps interpret the requested process or the role of a tool/material.
  The step need not repeat the tool/material name. For purpose questions, keep the
  actual operations before and after its use, so the reader can infer the function.
- irrelevant: a separate unrelated event or incidental property with no contribution.
- uncertain: relevance cannot be established even from this observed process.
Direct and context become keep; uncertain and irrelevant are excluded from the reader.
Judge contribution, not whether a step independently answers the entire question.
Do not demand a complete goal, both phases, or repeated occurrences in each statement.
A concrete step in the relevant activity is direct/context even if its overall goal
is unknown. Do not infer a specific answer first and filter to match it.
Preserve competing evidence, negation, factual uncertainty and all literal bindings.
Context does not establish continuity or invent relations; nearby unrelated events
remain irrelevant/uncertain. Question/option premises are not observed facts.
Return only the contribution label for every ID, without rewriting observations.'''


OPTION_PROMPT = '''Independently audit option_to_check against the supplied observed statements.
option_to_check is the EXACT text you must assess. alternative_options are only for
identifying distinctions; do not assess a different option or infer an index mapping.
No option is a preferred answer. The other options indicate distinctions to check,
not facts. Read the COMPLETE option, including actors, time order, preparation,
later steps, object roles and purpose. Every material claim must be grounded for
supported. A partial match or a more plausible story is unresolved.
contradicted requires an observed conflicting fact about the SAME actor/event/slot.
Missing a mention, missing a phase, or seeing another activity is NOT contradiction.
Negation, modality and scope of every observation must be respected. A question or
option cannot supply a missing action, motive or starting condition.
Return status, up to four distinct literal observation references (unused slots none),
missing_kind, and a brief reason. Supported/contradicted require actual witnesses and
missing_kind=none. Unresolved must identify a missing kind. Witness IDs alone do not
establish a claim: check what their text actually says. Do not invent evidence.'''

COVERAGE_PROMPT = '''Audit whether the observed evidence addresses the original question's
required participants, relationship, process and temporal distinctions. Options are
hypotheses only. Seeing two people is not evidence of interaction. A lone phase does
not establish a change. A single action does not establish a repetitive process.
For each supplied temporal region, cite an observation relevant to the question from
THAT region, or unresolved. For question_content, require evidence for the material
parts of the question, including different actors or steps when requested. Use
unresolved if an essential event, relationship, phase, or purpose cannot be established.
Do not claim evidence is complete merely because one option sounds plausible.
No fixed number of scenes or whole-video scan is required. Return JSON only.'''

NAV_PROMPT = '''Choose ONE unopened scene most likely to resolve the recorded evidence gap.
The scene descriptions are navigation hints, never answer evidence. Read the original
question, competing options and unresolved assessments without assuming an answer.
Prefer a missing required time region when specified; otherwise seek the missing
actor, action, relationship or process step. Do not repeat already read captions.
Return only the allowed scene_id. This requests source reading, not a sufficient verdict.'''

RECONSIDER_PROMPT = FILTER_PROMPT + '''
This is a post-filter evidence-loss review. Options and prior source witness IDs are
provided ONLY to identify relevant distinctions. They are not trusted facts or a
preferred answer. Reassess each target observation against its literal source and
the full observed context. A prior witness nomination does NOT force keep. Keep
only observations confirmed relevant; uncertain and drop still stay out of the reader.'''

PROMPTS = dict(zip(OPS, [FILTER_PROMPT, OPTION_PROMPT, COVERAGE_PROMPT, NAV_PROMPT, RECONSIDER_PROMPT]))


def decision(contribution):
    return {'direct':'keep','context':'keep','irrelevant':'drop','uncertain':'uncertain'}[contribution]


def views(sources):
    return [{k: q.observation(s)[k] for k in ('source_id', 'text', 'intervals')} for s in sources]


def validate_request(operation, request):
    if operation not in OPS or not request.get('question'):
        raise ValueError('Missing Q3 operation/question')
    expected = {'q3_filter': {'question','observations','context'},
                'q3_reconsider': {'question','options','observations','context','witness_ids'},
                'q3_option_audit': {'question','options','option_index','observations'},
                'q3_coverage': {'question','options','observations','regions'},
                'q3_navigate': {'question','options','assessments','missing_regions','scenes'}}[operation]
    if set(request) != expected:
        raise ValueError('Unexpected Q3 request fields')
    if operation == 'q3_navigate':
        if not request['scenes'] or len({s['id'] for s in request['scenes']}) != len(request['scenes']):
            raise ValueError('Invalid navigation allowlist')
        return
    observations = request['observations']
    ids = [s['source_id'] for s in observations]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate observation ID')
    context = request.get('context', [])
    if operation in ('q3_filter','q3_reconsider') and not 1 <= len(observations) <= WINDOW:
        raise ValueError('Q3 window must have 1..8 targets')
    if operation == 'q3_filter' and len(context) > 4:
        raise ValueError('Q3 local context exceeds four observations')
    allids = ids + [s['source_id'] for s in context]
    if len(allids) != len(set(allids)):
        raise ValueError('Repeated target/context ID')
    for s in observations + context:
        q.validate_request(dict(question=request['question'], current=s, context=[]))
    if operation == 'q3_option_audit' and not 0 <= request['option_index'] < len(request['options']):
        raise ValueError('Invalid option index')
    if operation == 'q3_reconsider' and not set(request['witness_ids']) <= set(allids):
        raise ValueError('Foreign witness reference')


def witness_schema(ids, required=False):
    return record({f'w{i}': enum(ids if i == 0 and required else ['none', *ids]) for i in range(4)})


def schema(operation, request):
    validate_request(operation, request)
    ids = [s['source_id'] for s in request.get('observations', [])]
    if operation in ('q3_filter','q3_reconsider'):
        return record({sid: enum(['direct','context','irrelevant','uncertain']) for sid in ids})
    if operation == 'q3_navigate':
        return record(dict(scene_id=enum(s['id'] for s in request['scenes'])))
    if operation == 'q3_option_audit':
        branches = []
        for status in (['supported','contradicted','unresolved'] if ids else ['unresolved']):
            branches.append(record(dict(status=enum([status]),
                witnesses=witness_schema(ids, status != 'unresolved'),
                missing_kind=enum(['none'] if status != 'unresolved' else
                    ['action','actor','time','relationship','purpose','ambiguous_source']),
                reason=short_text(160))))
        return {'anyOf': branches}
    fields = {}
    for name, eligible in [('question_content', ids), *[(r['id'], [s['source_id'] for s in request['observations']
            if any(a < r['interval'][1] and b > r['interval'][0] for a,b in s['intervals'])]) for r in request['regions']]]:
        branches = [record(dict(status=enum(['unresolved']), witness=enum(['none'])))]
        if eligible:
            branches.append(record(dict(status=enum(['supported']), witness=enum(eligible))))
        fields[name] = {'anyOf': branches}
    return record(fields)


def validate_verdict(operation, request, value):
    validate_output(value, schema(operation, request))
    # Repeated witnesses do not add evidence. Their positions are slots, not events;
    # leave raw slots unchanged and use a set only for coverage bookkeeping.
    return value


def windows(question, sources):
    """Time-ordered windows, at most 8 targets over 20 seconds and four neighbors.

    This is a bounded temporal context, not a claim of event identity. Deduplicated
    statements keep all their times; no hidden rewrite or continuity inference.
    """
    ordered = sorted(views(sources), key=lambda s: min((a for a,b in s['intervals']), default=float('inf')))
    result = []
    start = 0
    while start < len(ordered):
        first = min((a for a,b in ordered[start]['intervals']), default=0)
        end = start + 1
        while end < len(ordered) and end-start < WINDOW:
            t = min((a for a,b in ordered[end]['intervals']), default=first)
            if t-first > 20:
                break
            end += 1
        context = ordered[max(0,start-2):start] + ordered[end:end+2]
        result.append(dict(question=question, observations=ordered[start:end], context=context))
        start = end
    return result


def temporal_regions(question, scope, scenes):
    """Conservative lexical requirements for explicit temporal language only.

    These are necessary sampling regions, NOT evidence of semantic coverage.
    Model coverage still must cite relevant content in each region.
    """
    lo, hi = scope
    if hi <= lo:
        raise ValueError('Invalid question scope')
    lower = question.lower()
    if re.search(r'first half|second half|latter half', lower):
        mid = (lo+hi)/2
        return [dict(id='first_half', interval=[lo,mid]), dict(id='second_half', interval=[mid,hi])]
    result = []
    ordered = sorted((s for s in scenes if s['interval'][0] < hi and s['interval'][1] > lo), key=lambda s:s['interval'][0])
    if ordered and re.search(r'\b(beginning|initially|at the start)\b', lower):
        result.append(dict(id='beginning', interval=[lo, min(hi, ordered[0]['interval'][1])]))
    if ordered and re.search(r'\b(at the end|finally|ending)\b', lower):
        result.append(dict(id='ending', interval=[max(lo, ordered[-1]['interval'][0]), hi]))
    return result


def certificate(option_checks, coverage):
    supported = [i for i,c in enumerate(option_checks) if c['status']=='supported']
    sufficient = len(supported)==1 and all(c['status']=='contradicted' for i,c in enumerate(option_checks) if i not in supported)
    sufficient = sufficient and all(c['status']=='supported' for c in coverage.values())
    return dict(sufficient=sufficient, supported_options=supported,
                missing_regions=[k for k,v in coverage.items() if k!='question_content' and v['status']!='supported'],
                option_checks=option_checks, coverage=coverage)


def witness_ids(audit):
    result = set()
    for c in audit['option_checks']:
        if c['status'] in ('supported','contradicted'):
            result.update(v for v in c['witnesses'].values() if v!='none')
    result.update(v['witness'] for v in audit['coverage'].values() if v['status']=='supported')
    return result


def available_scenes(directory, sources):
    seen = {c for s in sources for c in s.get('source_caption_ids', [])}
    return [dict(id=s['id'], interval=s['interval'], description=s['description']) for s in directory['scenes']
            if set(s['caption_ids'])-seen]


def read_scene(directory, sources, scene_id):
    """Read only requested scene captions from a frozen archive. No model synthesis."""
    if scene_id not in {s['id'] for s in available_scenes(directory, sources)}:
        raise ValueError('Unavailable/repeated scene')
    scene = next(s for s in directory['scenes'] if s['id']==scene_id)
    seen = {c for s in sources for c in s.get('source_caption_ids', [])}
    captions = {c['id']: c for c in directory['captions']}
    result = deepcopy(sources)
    for cid in scene['caption_ids']:
        if cid in seen:
            continue
        c = captions[cid]
        result.append(dict(id='R_'+cid, text=c['text'], source_intervals=[c['interval']],
                           source_caption_ids=[cid], source_type='caption', source_status='caption_asserted',
                           retrieved_scene_id=scene_id))
    if len({s['id'] for s in result}) != len(result):
        raise ValueError('Retrieved source ID collision')
    return result
