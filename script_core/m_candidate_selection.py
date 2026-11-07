"""Shared M/N retrieval planning across competing hypotheses, then joint ranking."""
from copy import deepcopy
import re

from .schemas import record, short_text, validate_output

VERSION = 'MN-neutral-query-grounded-levels-v6'
PLAN_OPERATION = 'm_candidate_query'
OPERATION = 'm_candidate_rank'
KEYS = ('scene_reviews',)
PLAN_PROMPT = '''Prepare a retrieval brief, not an answer. Read the question and ALL
options. For EACH option, separate its observable claim (actions, objects, states,
order or other facts that can be checked) from any proposed purpose or explanation.
Keep alternative claims separate; none is a known premise. Preserve each option's
meaning without inventing extra actions. A purpose may be unknown until detailed
evidence is read. Then state the neutral question the evidence must distinguish.
Return option_claims keyed by every original option ID, each with observable_claim
and proposed_explanation (empty if absent), then retrieval_question. Be concise.
'''
PROMPT = '''阅读问题、全部选项、retrieval_brief和全部场景摘要，选择值得进一步查看的场景，不是选出答案。
选项及brief都是待验证假设，不是已发生事实。逐一考虑全部选项，不能只按第一个选项排除场景。
对每个场景，从该场景的summary_clauses复制最能说明实际活动的一条原文作为anchor，没有相关内容可填null；
再判断evidence_level：task_evidence（具体操作、对象交互、状态或过程，有助区分选项）、context_only（一般动作或背景，相关但区分力较弱）、unrelated（对所有选项都无关）。
只能引用本场景原文，不能引用选项或其他场景。引用本身不代表相关；无关原文也必须判unrelated。
只要涉及待查事实就可保留，不要求已证明选项的目的。不要凭猜测意图把一种动作当成未提及的另一种动作。
联合比较全部场景：具体任务操作应优先于一般走动或环境背景。无强制数量、时间覆盖或非空要求。
程序固定按task_evidence、context_only顺序查看，unrelated不查看。同等级按原目录顺序。
返回scene_reviews，覆盖全部场景ID；无需另写猜测性解释，原文引用及证据等级就是检索依据。'''



def prepare_inputs(inputs):
    result = deepcopy(inputs)
    if not result['options'] or not result['directory']:
        raise ValueError('Candidate selection requires options and scene summaries')
    ids = [s['id'] for s in result['directory']]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate scene IDs')
    for scene in result['directory']:
        clauses=[m.group().strip() for m in re.finditer(r'[^。！？；.!?;\n，,]+[。！？；.!?;\n，,]*',scene['description']) if m.group().strip()]
        scene['summary_clauses']=clauses
    return result


def output_schema(operation, inputs):
    option_ids = [f'O{i}' for i in range(len(inputs['options']))]
    if not option_ids:
        raise ValueError('Empty options')
    if operation == PLAN_OPERATION:
        return record(dict(option_claims=record({oid: record(dict(
            observable_claim=short_text(180), proposed_explanation=short_text(120)))
            for oid in option_ids}), retrieval_question=short_text(240)))
    if operation != OPERATION:
        raise ValueError('Unknown candidate operation')
    inputs = prepare_inputs(inputs)
    return record(dict(scene_reviews=record({s['id']:record(dict(
        anchor=dict(anyOf=[dict(type='string',enum=s['summary_clauses']),dict(type='null')]),
        evidence_level=dict(type='string',enum=['task_evidence','context_only','unrelated'])))
        for s in inputs['directory']})))



def normalize_selection(raw, inputs):
    validate_output(raw, output_schema(OPERATION, inputs))
    decisions = {}
    for sid, review in raw['scene_reviews'].items():
        if review['evidence_level']!='unrelated' and review['anchor'] is None:
            raise ValueError('Relevant scene requires a literal source anchor')
        priority={'task_evidence':100,'context_only':50,'unrelated':0}[review['evidence_level']]
        decisions[sid] = dict(relevance='candidate' if priority else 'excluded',
            priority=priority, reason='Level='+review['evidence_level']+'; literal source quote archived.')
    return dict(scene_decisions=decisions,
        reason='Program maps task_evidence/context_only/unrelated to 100/50/0; directory-order ties.')


def review_scenes(inputs, meter, on_review=None):
    prepared = prepare_inputs(inputs)
    request = {k:deepcopy(prepared[k]) for k in ('question','options')}
    brief, call = meter.json(PLAN_OPERATION, request, ('option_claims','retrieval_question'))
    validate_output(brief, output_schema(PLAN_OPERATION, request))
    if on_review:
        on_review('query_plan', request, brief, call)
    request = dict(prepared, retrieval_brief=brief)
    raw, call = meter.json(OPERATION, request, KEYS)
    normalize_selection(raw, prepared)
    if on_review:
        on_review('joint_ranking', request, raw, call)
    return raw
