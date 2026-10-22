"""Controlled migration from VideoTree to SCRIPT on fixed, real evidence."""
from copy import deepcopy
import json
import re

from .answering import prepare_answer_inputs, restore_answer_order

STAGES = {
    'v0': 'VideoTree QA on original frozen selected captions',
    'v1': 'v0: replace selected captions with all original seconds',
    's2': 'v1: replace QA module with SCRIPT direct JSON, original choice order',
    's3': 's2: content-hash choice presentation',
    's4': 's3: overlapping window/scene description representation',
    's5': 's4: add frozen guided visual facts and their sources',
    's6': 's5: replace guided facts with frozen coverage facts',
    'g7': 'v0 baseline protected; audit s5 changes against new distinguishing facts',
    'c8': 's3: replace direct QA with joint caption answer/inspection decision, no execution',
}
PARENTS = {'v1': 'v0', 's2': 'v1', 's3': 's2', 's4': 's3', 's5': 's4', 's6': 's5', 'g7': 's5', 'c8': 's3'}


def original_call(packet, all_seconds=False):
    """Use the original factory and template, with labels absent from the item."""
    from prompts import PromptFactory
    factory = PromptFactory().get('qa_standard')
    item = {'uid': packet['question_id'], 'question': packet['question'],
            'narration': packet['captions'], 'duration': packet['duration'],
            **{f'option{chr(65+i)}': text for i, text in enumerate(packet['options'])}}
    indices = list(range(len(packet['captions']))) if all_seconds else packet['selected_seconds']
    if indices is None:
        raise ValueError('Original VideoTree did not produce a cached tree/QA for this question')
    # Original build_fewshot_examples('', ...) returns None, whose literal
    # rendering is part of the saved baseline prompt. Preserve it exactly.
    prompts = factory.fill(**item, fps=1., clip_length=1., num_words=300, examplars=None, loc_pred=indices)
    return {'head': factory.head, 'prompts': prompts}


def scripted_call(packet, stage):
    inputs = {'question': packet['question'], 'options': packet['options'], 'answer_mode': 'choice',
              'option_ids': list('01234'), 'claims': [], 'evidence': [], 'requirements': [],
              'bindings': [], 'identity_links': [], 'query_facts': [], 'observation_gaps': [],
              'stop_decision': {'reason': 'fixed_evidence_ablation', 'evidence_status': 'insufficient'}}
    if stage in ('s2', 's3'):
        inputs['caption_timeline'] = [{'id': f'C{i+1}', 'interval': [i, i+1], 'text': text}
                                      for i, text in enumerate(packet['captions'])]
        inputs['caption_status'] = 'unverified_descriptions'
    else:
        inputs['coarse_context'] = deepcopy(packet['coarse_context'])
        inputs['script_outline'] = deepcopy(packet['script_outline'])
    if stage in ('s5', 's6'):
        source = packet['facts']['script_guided' if stage == 's5' else 'coverage_first']
        inputs['claims'], inputs['evidence'] = deepcopy(source['claims']), deepcopy(source['evidence'])
    return prepare_answer_inputs(inputs, 'direct', 'original' if stage == 's2' else 'content_hash', 0)


def restore_script(report, mapping):
    return restore_answer_order(report, mapping)


def review_call(packet):
    inputs = {'question': packet['question'], 'answer_mode': 'choice', 'options': packet['options'],
              'option_ids': list('01234'),
              'caption_timeline': [{'id': f'C{i+1}', 'interval': [i, i+1], 'text': text} for i, text in enumerate(packet['captions'])],
              'caption_status': 'unverified_descriptions',
              'candidate_windows': [{'id': w['id'], 'interval': w['interval']} for w in packet['coarse_context']['windows']
                                    if w['interval'][1] - w['interval'][0] <= 8],
              'verified_claims': [], 'inspected_windows': [], 'observation_gaps': [], 'remaining_reads': 3, 'max_reads': 1}
    return prepare_answer_inputs(inputs, 'direct', 'content_hash', 0)


def distinguishing_claims(packet, baseline, candidate):
    """Conservative lexical prefilter; semantic acceptance is a separate audit."""
    if candidate is None or candidate == baseline:
        return []
    stop = set('a an the is are was were be been to of in on at by for and or then with using it its this that as from into c she he they their his her'.split())
    words = lambda s: set(re.findall(r'[a-z]+', s.lower())) - stop
    contrast = words(packet['options'][candidate]) ^ words(packet['options'][baseline]) if baseline is not None else words(packet['options'][candidate])
    source = packet['facts']['script_guided']
    evidence = {e['id']: e for e in source['evidence']}
    result = []
    for c in source['claims']:
        verification = c.get('verification', {})
        if (c.get('status') != 'supported' or c.get('source_type') != 'visual' or c.get('fact_type') == 'inference'
                or verification.get('whole_claim_supported') is False or verification.get('unresolved_conflicts')):
            continue
        # Do not admit the known state/other loophole for this protective audit.
        dynamic = c.get('fact_type') == 'action' or bool(re.search(
            r'\b(uses?|removes?|puts?|takes?|turns?|opens?|closes?|rolls?|cuts?|places?|passes?|plants?)\b', c.get('description', '').lower()))
        if dynamic:
            ids = set(c.get('source_frame_ids', []))
            pts = {f['pts'] for ref in c['evidence_ids'] for f in evidence[ref].get('frame_inputs', []) if f['frame_id'] in ids}
            if len(pts) < 3 or not verification.get('process_visible'):
                continue
        if words(c.get('description', '')) & contrast:
            result.append(c)
    return result


def accept_guard(report, eligible):
    allowed = {c['id'] for c in eligible}
    refs = report.get('claim_ids', [])
    return (report.get('accept') is True and report.get('resolves_distinction') is True
            and isinstance(refs, list) and bool(refs) and set(refs) <= allowed)


GUARD_HEAD = '''Audit a proposed change to a video QA baseline. The input facts have visual provenance,
but you must still check their meaning. Accept only if cited NEW facts directly resolve the specific
difference between the baseline and proposed option. Shared actions, generic tools, inferred purposes,
or missing observations do not establish that difference. Do not change an answer just because another
model prefers it. A fact is limited to its own time range. Output one JSON object only:
{"accept": boolean, "resolves_distinction": boolean, "claim_ids": [exact input IDs], "reason": "brief reason"}.
Reject with empty claim_ids when there is no direct distinguishing support. This is an experiment
in conservative answer updates, not a guarantee of visual or semantic correctness.'''


def guard_call(packet, baseline, candidate, claims):
    return {'head': GUARD_HEAD, 'prompts': [json.dumps({
        'question': packet['question'], 'baseline_option': packet['options'][baseline] if baseline is not None else None,
        'proposed_option': packet['options'][candidate],
        'new_facts': [{'id': c['id'], 'interval': c['interval'], 'description': c.get('description', '')} for c in claims],
        'selected_descriptions': [{'second': i, 'text': packet['captions'][i]} for i in (packet['selected_seconds'] or [])]
    }, ensure_ascii=False)]}
