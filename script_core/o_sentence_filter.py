"""O/O+: N-derived per-sentence binary selection without lexical source guards."""
from copy import deepcopy
import json
import re

from . import generalized_lazy_memory as fields_core
from .schemas import array, enum, record, short_text, validate_output

VERSION = 'O-N-question-required-local-evidence-examples-v14'
PLUS_VERSION = 'Oplus-N-source-bound-renderings-v15'
EXTRACT = 'o_extract'
FILTER = 'o_filter'
COMPOSE = 'oplus_compose'
FILTER_PLAN = 'o_filter_plan'
CATEGORIES = ('actions','attributes','states','relations','changes','results','event_links')

EXTRACT_PROMPT = '''Extract the information actually stated in this ONE sentence BEFORE
any relevance filtering. No question, answer options, or question-planned field subset
are supplied. Extract the stated entities in their action/attribute/state/relation
records, including explicit spatial details. Do not decide what a future question needs.
Return structured records, not a summary. Only a leading hash tag such as #C is metadata;
the following subject C is NOT metadata and MUST be extracted as actor="C".
For "#A A opens a drawer", the explicit actor is "A", verb "opens", patient "a drawer".
Never discard the explicit actor just because the caption's tag repeats its name.
Keep different actions/events separate. Actions use actor, verb, and a roles OBJECT;
patient is the affected object, instrument the tool, body_part the body part, location
where, destination where to, source_location where from, surface the working surface,
material the consumed material, manner how, recipient the receiver. Include only roles
actually stated; omit absent role keys. ALWAYS preserve an explicit executing subject,
including names and pronouns. actor=null only for a genuinely unstated executing actor,
such as an agentless passive. In multi-person actions, keep actor and recipient distinct.
A preposition or noun is not an action: keep into/with/on details in the SAME action's
roles rather than creating extra actions. Preserve negation and modality in descriptions;
do not turn a negated
or uncertain statement into a positive fact. States use entity/description.
Use these record formats:
actions=[{"actor":"a person","verb":"places","roles":{"patient":"a spoon","destination":"a drawer"}}]
for "A person places a spoon into a drawer." No other categories are present there.
attributes=[{"entity":"cup","property":"color","value":"red"}] for an explicit red cup.
states=[{"entity":"door","description":"open"}].
relations=[{"left":"cup","predicate":"on","right":"desk","relation_type":"spatial"}].
changes=[{"entity":"door","before":"closed","after":"open"}].
results=[{"entity":"cup","predicate":"becomes","value":"empty"}].
event_links=[{"left_event":"opens the box","connector":"before","right_event":"takes the book","link_type":"before"}].
These are FORMAT examples, not facts about the input. Extract only what it actually says.
Set extraction_status="extracted" when any information is extracted.
Return empty arrays for absent categories. Do not emit guessed attributes or the string
"null" as an unknown color/property. Only if unable to extract ANY information,
set extraction_status=unavailable and all arrays empty. Do not infer unstated details.
This is extraction only, not relevance filtering. Output the required JSON only.'''
FILTER_PLAN_PROMPT = '''为后续逐句证据筛选整理问题及可选的选项，不选择答案、不规划抽取字段。
问题一定提供。若提供options，分别整理全部选项，option_tests使用O0、O1等原始ID。
若没有options，只整理问题要寻找的证据，option_tests只使用Q；不要猜测或编造选项。
每个选项分别拆为 single_observation（单次可以观察的动作/对象交互/状态/关系）、
aggregate_requirement（反复、顺序、整体过程等需多条证据累积判断的部分）、
interpretation（目的/意义等待推断部分）。single_observation 不写“反复/经常/一直”，
而写每次发生什么；保留原有主体、对象和可观察动作，不凭目的编造动作。
选项给出例子时保留其概括的行为，不把例子变成唯一允许的物品。
单次内容的转换例子：题目问“哪个动作反复发生”，没有选项时single_observation写“人物每次进行的具体动作及对象”；
选项“Repeatedly opens drawers”则single_observation写“opens a drawer”，aggregate_requirement写“repeatedly”。
不要把repeatedly、often、反复、多次等频率要求留在single_observation里；单次记录只用于之后统计/比较。
这些例子不提供当前视频事实。输出option_tests。'''
FILTER_PROMPT = '''你负责逐句收集证据，不负责回答整道题。问题一定提供，options可选。
问题及提供的选项不是事实；没有options时不要猜测选项，直接根据问题判断相关性。
evidence_task 已区分问题/选项的单次可观察内容、需累积判断的频率/过程及待推断目的。
当前只判断这条原句中的候选信息，能否作为问题或任一已提供选项的局部证据或反证。
对任一 single_observation 有帮助即可保留，不要求本句同时满足某选项的所有动作。
一次观察无法单独证明“反复/整体目的”，这不能成为删除理由；这些由后续汇总证据判断。
你判定的是保留证据，不是判定选项成立。统计某动作是否反复，首先必须保留它每次发生的记录。
例如问“反复做什么”，原句“A opens a drawer”要保留A/opens/drawer，即使这句没有次数或频率。
不能在relevance里用“只有一次、没有频率、不能证明重复”转而删除相关动作；单次动作正是累计输入。
拿起、放下、装入等具体物品操作可以成为相关活动的组成证据，不必已经证明活动目的。
不要先选择某个答案再删除其他选项的证据。共同支持多个选项的信息也可以保留。

先输出observation：简述原句明确发生的事实，保留谁做什么。
再输出relevance：说明与问题或已提供选项的哪部分可观察内容有关；确实无关才说明无关。
最后对每个候选ID输出labels整数：1保留，0省略。只选择候选，不新增/改写候选值。
同一group的动作字段联合理解：保留相关动作时，同时保留其明确主语、对象和必要角色，
包括改变动作含义的目标地点/接收者。主语或对象单看不能回答问题，不代表它可被删。
不同人的动作/给予者/接收者不能混淆。无关颜色/身体部位等额外细节可省略。
一个错误的额外字段不能使同组有效主语/动作/对象全部作废。
对问题及已提供选项都无关时允许全0，不设保留数量。raw_sentence按同样的局部证据标准整体判断。
以下示例说明“保留局部证据”的0/1含义，不是当前视频事实：
问题：Which action keeps recurring while cleaning?（没有options）
原句：M wipes a table. 候选：U0 actor=M，U1 verb=wipes，U2 patient=a table。
正确输出：{"observation":"M wipes a table","relevance":"One observed wiping event is evidence to collect when comparing recurring cleaning actions.","labels":{"U0":1,"U1":1,"U2":1}}
问题同上，options若为["Repeatedly wipes surfaces","Repeatedly rinses tools"]，这句仍输出U0=1,U1=1,U2=1。
问题同上，原句：The unused clock is purple. 候选U0为clock/color/purple。
正确输出：{"observation":"An unused clock is purple","relevance":"Clock color does not inform the cleaning actions.","labels":{"U0":0}}
请判断当前实际候选，不复制示例事实。只输出JSON：observation、relevance、labels，labels覆盖所有候选ID。'''
from .o_source_bound_composition import PROMPT as COMPOSE_PROMPT, renderings as compose_renderings



def extraction_schema():
    # The fixed taxonomy is available for EVERY sentence. No question-dependent
    # field plan can disable a role or category before binary filtering.
    requested=fields_core.FIELDS
    text=short_text(160);text['minLength']=1
    nullable={'anyOf':[deepcopy(text),{'type':'null'}]}
    roles=['patient']+sorted({f.split('.',1)[1] for f in requested if f.startswith('actions.') and f!='actions.base'})
    defs={
        'actions':(record(dict(actor=nullable,verb=text,roles=record({r:nullable for r in roles},optional=roles))),4),
        'attributes':(record(dict(entity=text,property=enum([f.split('.',1)[1] for f in requested if f.startswith('attributes.')]),value=text)),6),
        'states':(record(dict(entity=text,description=text)),4),
        'relations':(record(dict(left=text,predicate=text,right=text,relation_type=enum([f.split('.',1)[1] for f in requested if f.startswith('relations.')]))),6),
        'changes':(record(dict(entity=text,before=text,after=text)),2),
        'results':(record(dict(entity=text,predicate=text,value=text)),2),
        'event_links':(record(dict(left_event=text,connector=text,right_event=text,link_type=enum([f.split('.',1)[1] for f in requested if f.startswith('event_links.')]))),2)}
    props={k:array(s,n) for k,(s,n) in defs.items()}
    return record(dict(extraction_status=enum(['extracted','unavailable']),**props))


def sentence_sources(units):
    """Split source text, retaining source/time metadata; never join source sentences."""
    result=[]
    for unit in units:
        parts=re.split(r'(?<=[.!?。！？])\s+',unit['text'].strip())
        for index,text in enumerate(parts):
            if text.strip():
                result.append(dict(id=f'S{len(result)}',text=text,
                    original_source_id=unit['id'],part=index,
                    source_caption_ids=deepcopy(unit.get('source_caption_ids',[])),
                    source_frame_ids=deepcopy(unit.get('source_frame_ids',[])),
                    source_intervals=deepcopy(unit.get('source_intervals',[])),
                    source_type=unit.get('source_type','caption'),
                    source_status=unit.get('status','caption_asserted')))
    return result


def extraction_input(source):
    # No question/options/labels, and no source-derived allowed-value enums.
    return dict(sentence=source['text'])


def candidates(source, extracted):
    validate_output(extracted,extraction_schema())  # Shape/types only.
    units=[]
    if extracted['extraction_status']=='unavailable' and any(extracted[k] for k in CATEGORIES):
        raise ValueError('Unavailable extraction must have empty categories')
    for category in CATEGORIES:
        for index,item in enumerate(extracted[category]):
            group=f'{category}_{index}'
            if category=='actions':
                values=dict(actor=item['actor'],verb=item['verb'],**item['roles'])
                for field,value in values.items():
                    if value is not None:
                        units.append(dict(id=f'U{len(units)}',kind='action_field',group=group,field=field,value=value))
            else:
                units.append(dict(id=f'U{len(units)}',kind=category,group=group,record=deepcopy(item)))
    if not units:
        units=[dict(id='U0',kind='raw_sentence',group=source['id'],text=source['text'])]
    return units


def filter_input(question,source,units):
    result=dict(question=question['question'],
                sentence=source['text'],candidates=deepcopy(units))
    if 'options' in question:result['options']=deepcopy(question['options'])
    if 'evidence_task' in question:result['evidence_task']=deepcopy(question['evidence_task'])
    return result


def filter_schema(units):
    ids=[u['id'] for u in units]
    if not ids or len(set(ids))!=len(ids):raise ValueError('Candidates need unique IDs')
    return record({i:dict(type='integer',enum=[0,1]) for i in ids})


def select(units,labels):
    validate_output(labels,filter_schema(units))
    if any(type(v) is not int for v in labels.values()):raise ValueError('Only integer 0/1 labels')
    return [deepcopy(u) for u in units if labels[u['id']]==1]


def preserve_action_core(units,model_labels):
    """Retained verbs keep their already-extracted actor/patient, not source text."""
    select(units,model_labels)  # Validate before applying the explicit dependency.
    labels=deepcopy(model_labels);overrides=[]
    active={u['group'] for u in units if u.get('kind')=='action_field'
        and u.get('field')=='verb' and labels[u['id']]==1}
    for u in units:
        if u.get('kind')=='action_field' and u['group'] in active and u.get('field') in ('actor','patient') and labels[u['id']]==0:
            labels[u['id']]=1
            overrides.append(dict(id=u['id'],before=0,after=1,group=u['group'],
                rule='retained_action_keeps_its_extracted_actor_and_patient'))
    return labels,overrides


def compose_input(question,selected,*,include_options=False):
    if not selected:raise ValueError('Do not call composition for empty selection')
    groups={}
    for unit in selected:groups.setdefault(unit['group'],[]).append(deepcopy(unit))
    request=dict(question=question['question'],groups=groups)
    if include_options:request['options']=deepcopy(question['options'])
    return request


def compose_schema(request):
    if not isinstance(request.get('question'),str) or not request['question'].strip():
        raise ValueError('O+ composition requires the question')
    groups={}
    for gid,units in request['groups'].items():
        text=enum(compose_renderings(units))
        groups[gid]=record(dict(text=text))
    return record(dict(groups=record(groups)))


def schema(operation,request):
    if operation==FILTER_PLAN:return record(dict(option_tests=record({key:record(dict(
        single_observation=short_text(240),aggregate_requirement=short_text(120),interpretation=short_text(160)))
        for key in ([f'O{i}' for i in range(len(request['options']))] if 'options' in request else ['Q'])})))
    if operation==EXTRACT:
        if set(request)!={'sentence'}:raise ValueError('O extraction accepts only the source sentence')
        return extraction_schema()
    if operation==FILTER:return record(dict(observation=short_text(240),relevance=short_text(360),labels=filter_schema(request['candidates'])))
    if operation==COMPOSE:return compose_schema(request)
    raise ValueError('Unknown O operation')


def present(sources,decisions,*,composed=None):
    """Only selected units/text enter QA. Source provenance remains in the archive."""
    rows=[]
    for source in sources:
        chosen=decisions[source['id']]['selected']
        if not chosen:continue
        row=dict(source_id=source['id'],intervals=source['source_intervals'])
        if composed is None:
            # Mechanical serialization of selected slots, not semantic merging.
            # Serialize the final selection, including separately audited core dependencies.
            groups={}
            for unit in chosen:
                if unit['kind']=='action_field':
                    value=groups.setdefault(unit['group'],dict(kind='action',event_id=unit['group']))
                    value[unit['field']]=unit['value']
                else:
                    groups[unit['group']]=dict(kind=unit['kind'],**deepcopy(
                        unit.get('record',{'text':unit.get('text')})))
            row['information']=list(groups.values())
        else:
            item=composed[source['id']]
            request=compose_input(dict(question='Validate selected evidence only'),chosen)
            validate_output(dict(groups=item.get('groups')),compose_schema(request))
            text=' '.join(item['groups'][g]['text'].strip() for g in request['groups']
                          if item['groups'][g]['text'].strip())
            if item['text'].strip()!=text:
                raise ValueError('O+ composed text differs from validated source-bound groups')
            if not text:continue
            row['text']=text
        rows.append(row)
    return rows


def final_input(original,rows):
    from script.controlled_memory_io import parts
    from .evidence_planner import final_input as build
    description=('Selected observations; missing fields are unknown. Independent sample intervals '
        'do not establish continuity or event counts. Options are not evidence.\n'+
        ('\n'.join(json.dumps(row,ensure_ascii=False,separators=(',',':')) for row in rows)
         if rows else 'No information was retained. Evidence is insufficient.'))
    return build(original,description,parts)
