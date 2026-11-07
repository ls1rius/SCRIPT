"""J: conservative compaction, evidence-first reading, audited atomic claims.

Exact quotation and lexical guards are necessary checks, not a semantic proof.
No labels, new source extraction, hidden answer replacement, or repair loops.
"""
from copy import deepcopy
import json
import re

try:
    from .demo_evidence_revision import canonical_body
except ImportError:
    from demo_evidence_revision import canonical_body

VERSION = 'grounded-reader-J-v3-candidate-quotes'

FOCUS_SYSTEM = '''Select the observed evidence that best distinguishes the five options. Do not answer the question yet.
Return only {"evidence_ids":[...]}, at most 12 unique supplied IDs. Select the actual differentiating objects, actions, tools and stages.
For main-activity questions include several distinct relevant steps across the available time segments, not many repetitions of one incidental action.
If a detail is absent, do not substitute evidence for a merely plausible action. Keep contradictory/noisy source assertions visible when relevant.
Do not generate facts, option judgments or explanations. IDs are pointers to the exact supplied observations.'''

ANSWER_SYSTEM = '''Choose the best answer using sparse video-caption evidence and sampled visual observations.
First inspect the focused verbatim observations. The complete available evidence remains below; you may cite any supplied ID.
For each option give at most two SHORT atomic distinguishing claims (usually 2-8 words), copied VERBATIM as contiguous excerpts from that option.
Never copy a whole option or just repeat the common action shared by all options. Check the distinctive action, object, tool, purpose, or order.
Each claim has relation observed, inferred, unknown, or contradicted, and at most two evidence IDs.
observed means the cited source explicitly expresses that claim, including its objects/tools/roles. A related object or incidental action is insufficient.
inferred means a plausible task-level interpretation of cited observations, not a directly observed fact. Main purpose may require inference from several different steps.
unknown means the supplied sources do not establish the claim. Use no IDs for unknown claims.
contradicted requires an explicit contrary assertion; missing evidence MUST be unknown. Different activities can both occur, so one does not negate another.
Do not invent an unseen next step unless the question explicitly asks what happens next. Do not infer a phone's app/activity just from handling it.
Do not treat repeated captions as votes or infer continuous duration across gaps. Auxiliary caption memory is not independent corroboration.
The program will check literal option excerpts, expand cited source quotations and downgrade unsupported direct/contradiction claims. Source-text checks do not establish semantic entailment.
Finally provide exactly one selected_option A/B/C/D/E, consistent with the checks. Person C is not an option declaration.
Return only JSON {"option_checks":{"A":[{"claim":"literal short option excerpt","relation":"unknown","evidence_ids":[]}],"B":[...],"C":[...],"D":[...],"E":[...]},"selected_option":"A"}.
No free-form explanation or confidence. The program attaches the exact selected option text and quoted observations.'''

GROUNDING_SYSTEM = ANSWER_SYSTEM.replace(
    'Choose the best answer using sparse video-caption evidence and sampled visual observations.',
    'Match atomic option claims to sparse video-caption evidence and sampled visual observations. Do not choose an answer yet.'
).replace(
    'First inspect the focused verbatim observations. The complete available evidence remains below; you may cite any supplied ID.',
    'Inspect the complete available evidence and cite only the exact observations that express each distinguishing claim.'
).replace(
    'Finally provide exactly one selected_option A/B/C/D/E, consistent with the checks. Person C is not an option declaration.',
    'Do not choose an option. The program will validate these matches before a separate final selection.'
).replace(',"selected_option":"A"}', '}')

SELECT_SYSTEM = '''Choose one option using the supplied question, options, complete original evidence, and program-audited claim/evidence matches.
The matched evidence is quoted verbatim. observed matches passed necessary literal-word checks, not a complete semantic proof.
inferred matches are interpretations, not observations. unknown and rejected matches provide no positive support.
Missing evidence is not a contradiction. Compare each option's distinguishing claims and unsupported additions.
For main activity, consider multiple distinct steps; do not count repeated samples as votes or incidental actions as the whole purpose.
Do not invent a plausible next step unless asked. Select the best available option even when all options are partly unverified.
Output only {"selected_option":"A"}, with exactly one A/B/C/D/E ID. No explanation or confidence.
The program will attach the exact option text and its already-audited evidence; you cannot rewrite those matches.'''

GROUNDING_SYSTEM = '''Match the supplied atomic option excerpts to actual video-caption evidence.
The program has already split the options. For each fixed claim ID, select at most two source IDs that actually express the excerpt.
Then label it observed, inferred, unknown, or contradicted. Do not generate a claim or an answer choice.
observed: the source explicitly states the action/object/tool/role in this excerpt. Nearby or related actions are insufficient.
inferred: a task-level interpretation of multiple observed steps; never claim an unobserved concrete action or tool as an inference.
unknown: no supplied source establishes this excerpt. Use an empty evidence_ids list.
contradicted: an explicit contrary assertion; absence alone is unknown. One activity does not exclude another.
Exact action examples: a source saying 'C cuts wood' supports the excerpt 'cutting'; it does not support 'nailing'.
Main purpose may be inferred from different relevant steps, not repeated votes or incidental hand cleaning.
Match every supplied claim ID. Return only {"claim_checks":{"A0":{"evidence_ids":[],"relation":"unknown"},...}}.
Do not copy the option or write explanations. IDs are pointers to observations, not actor names.'''
GROUNDING_SYSTEM += '''
Each claim has program-provided candidate quotes. exact_candidate_ids contain all content words of the claim; verify the actual meaning and negation before calling it observed.
When there are no exact candidates, direct observation is unavailable; use unknown, or inferred only for a higher-level purpose supported by multiple actual steps.
Read the candidate quote text, not just its ID. Candidate lexical matches are not an entailment proof.'''


def object_schema(properties):
    return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)


def focus_schema(ids):
    return object_schema(dict(evidence_ids=dict(type='array',items=dict(type='string',enum=sorted(ids)),minItems=1,maxItems=12)))


def answer_schema(ids,claims=None,candidates=None):
    if claims is not None:
        properties={}
        for c in claims:
            candidate=candidates[c['claim_id']] if candidates is not None else None
            allowed=candidate['exact_candidate_ids'] if candidate and candidate['exact_candidate_ids'] else sorted(ids)
            relations=['observed','inferred','unknown','contradicted'] if candidate is None or candidate['exact_candidate_ids'] else ['inferred','unknown']
            properties[c['claim_id']]=object_schema(dict(evidence_ids=dict(type='array',items=dict(type='string',enum=allowed),maxItems=2),
                relation=dict(type='string',enum=relations)))
        return object_schema(dict(claim_checks=object_schema(properties)))
    item=object_schema(dict(claim=dict(type='string',minLength=1,maxLength=90),
        relation=dict(type='string',enum=['observed','inferred','unknown','contradicted']),
        evidence_ids=dict(type='array',items=dict(type='string',enum=sorted(ids)),maxItems=2)))
    return object_schema(dict(option_checks=object_schema({o:dict(type='array',items=deepcopy(item),minItems=1,maxItems=2) for o in 'ABCDE'})))


def intervals_union(intervals):
    merged=[]
    for a,b in sorted(intervals):
        if merged and merged[-1][1]==a:merged[-1][1]=b
        else:merged.append([a,b])
    return merged


def compact_rows(rows):
    """Factor conservatively equivalent wording; retain all discrete sources."""
    groups=[];lookup={}
    for index,row in enumerate(rows):
        key=canonical_body(row['body'])
        if key not in lookup:
            lookup[key]=len(groups);groups.append(dict(body=row['body'],source_indices=[]))
        groups[lookup[key]]['source_indices'].append(index)
    lines=[];evidence=[]
    for index,group in enumerate(groups,1):
        sources=[deepcopy(rows[i]) for i in group['source_indices']]
        eid=f'J{index:03}'
        intervals=intervals_union([r['interval'] for r in sources])
        bounds=', '.join('['+','.join(str(t) for t in interval)+')' for interval in intervals)
        lines.append(f'[{eid}] Frames {bounds}: '+group['body'])
        evidence.append(dict(evidence_id=eid,representative_text=group['body'],display_intervals=intervals,
            sources=sources,source_indices=group['source_indices']))
    return '\n'.join(lines),dict(version=VERSION,source_rows=len(rows),rendered_rows=len(groups),rows=evidence,
        source_order_restorable=True,source_order_in_prompt='first occurrence of each group; all original timestamps retained',
        question_blind=True,identities_merged=False,continuous_duration_inferred=False,
        lexical_paraphrase_groups=sum(len({r['body'] for r in e['sources']})>1 for e in evidence))


def make_input(question,description,original,auxiliary,audit=None):
    options='\n'.join(f'{chr(65+i)}: {v}' for i,v in enumerate(question['options']))
    body='Complete available evidence:\n'+description+'\n\nQuestion: '+question['question']+'\nOptions:\n'+options
    if auxiliary:body+='\n\n'+auxiliary
    body+='\n\nFixed atomic option excerpts (each is a literal source-option substring):\n'+json.dumps(option_fragments(question['options']),ensure_ascii=False)
    if audit is not None:body+='\n\nProgram-matched candidate quotes per claim:\n'+json.dumps(claim_candidates(question['options'],audit),ensure_ascii=False)
    return dict(query_id=original['query_id'],head=GROUNDING_SYSTEM,prompts=[body])


def parse_json(raw):
    clean=raw.strip()
    if clean.startswith('```') and clean.endswith('```'):
        clean=re.sub(r'^```(?:json)?\s*','',clean)[:-3].strip()
    return json.loads(clean)


def quote_map(audit):
    return {r['evidence_id']:r for r in audit['rows']}


def focus_input(base,selected,audit):
    by_id=quote_map(audit)
    # This block is copied by code, not generated or paraphrased by the model.
    focused='\n'.join(f'[{eid}] '+by_id[eid]['representative_text'] for eid in selected)
    return dict(base,head=ANSWER_SYSTEM,prompts=['Focused verbatim observations:\n'+focused+'\n\n'+base['prompts'][0]])


STOP=set('a an the c x man woman individual person is are was were be been being to of and with by on in into at for from it he she his her their then first finally subsequently initially ultimately currently carefully skillfully accurately precisely thoroughly gently securely meticulously continuously repeatedly important most parts crucial aspects project process involved concerning used use uses interacted interact materials material'.split())
ALIASES=dict(cuts='cut',cutting='cut',measuring='measure',measured='measure',measures='measure',
    nailing='nail',nails='nail',painting='paint',paints='paint',sanding='sand',gluing='glue',
    applying='apply',applied='apply',applies='apply',holding='hold',holds='hold',
    walking='walk',walks='walk',touches='touch',touching='touch',lumber='wood',timber='wood',
    tablet='ipad',tablets='ipad',typing='type',types='type',writing='write',writes='write')


def tokens(text):
    return {ALIASES.get(w,w) for w in re.findall(r"[a-z]+",text.casefold()) if w not in STOP}


def option_fragments(options):
    """Plan bounded literal excerpts without consulting evidence or labels."""
    candidates=[]
    for option in options:
        parts=[p.strip() for p in re.split(r'[,.;]|\b(?:and|then|finally|subsequently|ultimately|initially|lastly|by|while|using|utilizing|into|were|involved|with)\b',option,flags=re.I)]
        parts=[p for p in parts if tokens(p)]
        candidates.append(list(dict.fromkeys(parts)) or [option])
    document_tokens=[tokens(o) for o in options]
    df={t:sum(t in ts for ts in document_tokens) for ts in document_tokens for t in ts}
    plan=[]
    for i,parts in enumerate(candidates):
        # Keep one distinguishing fragment and one additional independent fragment.
        # This bounded heuristic is not a general linguistic atomizer.
        ranking=sorted(range(len(parts)),key=lambda j:(-max(1/df[t] for t in tokens(parts[j])),len(parts[j]),j))
        chosen=ranking[:2]
        for slot,j in enumerate(sorted(chosen)):
            plan.append(dict(claim_id=f'{chr(65+i)}{slot}',option_id=chr(65+i),claim=parts[j]))
    return plan


def claim_candidates(options,audit):
    result={}
    for c in option_fragments(options):
        wanted=tokens(c['claim']);matches=[]
        for row in audit['rows']:
            if wanted and wanted<=tokens(row['representative_text']):
                matches.append(dict(evidence_id=row['evidence_id'],quote=row['representative_text']))
        result[c['claim_id']]=dict(exact_candidate_ids=[r['evidence_id'] for r in matches],quotes=matches[:4],
            quote_preview_limit=4,complete_observations_retained=True)
    return result


def audit_answer(raw,options,audit):
    value=parse_json(raw);choice=value.get('selected_option');checks=value.get('option_checks')
    if choice not in list('ABCDE') or not isinstance(choice,str):raise ValueError('Exact selected_option ID required')
    if not isinstance(checks,dict) or set(checks)!=set('ABCDE'):raise ValueError('All five option checks required')
    by_id=quote_map(audit);expanded={};downgrades=[]
    for letter,items in checks.items():
        if not isinstance(items,list) or not 1<=len(items)<=2:raise ValueError('One or two atomic checks per option required')
        expanded[letter]=[]
        for item in items:
            claim=item['claim'];relation=item['relation'];ids=item['evidence_ids']
            if relation not in {'observed','inferred','unknown','contradicted'} or len(ids)>2 or any(e not in by_id for e in ids):
                raise ValueError('Invalid claim relation or reference')
            reason=None
            if not claim.strip() or claim.casefold() not in options[ord(letter)-65].casefold():reason='claim_is_not_a_literal_option_excerpt'
            quotes=[by_id[e]['representative_text'] for e in ids]
            observed=' '.join(quotes)
            if relation!='unknown' and not ids:reason='no_cited_observation'
            # Lexical overlap is only a necessary guard; inference remains explicit.
            if relation=='observed' and (not tokens(claim) or not tokens(claim)<=tokens(observed)):
                reason='direct_claim_contains_unobserved_content_words'
            if relation=='observed' and quotes and all(re.search(r'\b(?:not|never|no|without)\b',q,re.I) for q in quotes) and not re.search(r'\b(?:not|never|no|without)\b',claim,re.I):
                reason='negative_observation_cannot_directly_support_positive_claim'
            if relation=='contradicted' and (not re.search(r'\b(?:not|never|no|without)\b',observed,re.I)
                    or not tokens(claim)<=tokens(observed)):
                reason='no_explicit_matching_negative_assertion'
            # Reject attributing another explicitly named actor's observation to C.
            if re.search(r'\bc\b',claim,re.I) and quotes and all(re.match(r'\s*#O\b',q) for q in quotes):
                reason='only_other_actor_sources_cited'
            effective='unknown' if reason else relation
            if reason:downgrades.append(dict(option_id=letter,claim=claim,original_relation=relation,reason=reason))
            expanded[letter].append(dict(claim=claim,model_relation=relation,relation=effective,
                evidence=[dict(evidence_id=e,quote=by_id[e]['representative_text'],
                    intervals=by_id[e]['display_intervals'],caption_ids=[s['caption_id'] for s in by_id[e]['sources']]) for e in ids],
                validation_reason=reason))
    selected=expanded[choice]
    grounded=any(c['relation'] in {'observed','inferred'} for c in selected)
    return dict(option=str(ord(choice)-65),method='strict_json_option_id',option_text=options[ord(choice)-65],
        option_checks=expanded,downgrades=downgrades,selected_has_usable_support=grounded,
        semantic_entailment_proven=False,choice_changed_by_validator=False,
        insufficient_evidence=not grounded or any(c['relation']=='unknown' for c in selected))


def audit_grounding(raw,options,audit):
    value=parse_json(raw)
    if set(value)=={'claim_checks'}:
        plan=option_fragments(options)
        if set(value['claim_checks'])!={c['claim_id'] for c in plan}:raise ValueError('Atomic claim plan changed')
        expanded={o:[] for o in 'ABCDE'}
        for claim in plan:
            expanded[claim['option_id']].append(dict(claim=claim['claim'],**value['claim_checks'][claim['claim_id']]))
        value=dict(option_checks=expanded)
    if set(value)!={'option_checks'}:raise ValueError('Grounding stage must not choose an answer')
    result=audit_answer(json.dumps(dict(value,selected_option='A')),options,audit)
    return dict(option_checks=result['option_checks'],downgrades=result['downgrades'],
        semantic_entailment_proven=False)


def selection_input(question,grounding,query_id,base=None):
    # The final decision sees actual quotes after rejected matches become unknown.
    options='\n'.join(f'{chr(65+i)}: {v}' for i,v in enumerate(question['options']))
    prompt=('Question: '+question['question']+
        '\nOptions:\n'+options+'\n\nAudited atomic matches and verbatim source quotations:\n'+
        json.dumps(grounding,ensure_ascii=False))
    if base is not None:prompt+='\n\nComplete evidence retained for unresolved distinctions:\n'+base['prompts'][0].split('\n\nFixed atomic option excerpts',1)[0]
    return dict(query_id=query_id,head=SELECT_SYSTEM,prompts=[prompt])


def finish(raw,options,grounding):
    value=parse_json(raw)
    if set(value)!={'selected_option'} or value['selected_option'] not in list('ABCDE'):
        raise ValueError('Final answer must contain one exact option ID')
    letter=value['selected_option'];checks=grounding['option_checks'][letter]
    usable=any(c['relation'] in {'observed','inferred'} for c in checks)
    return dict(option=str(ord(letter)-65),method='strict_json_option_id',option_text=options[ord(letter)-65],
        **grounding,selected_has_usable_support=usable,insufficient_evidence=not usable or any(c['relation']=='unknown' for c in checks),
        choice_changed_by_validator=False)
