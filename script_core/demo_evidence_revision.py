"""Conservative source-only paraphrase folding and evidence-grounded demo QA.

Independent of I/H storage. Accept only archived *visible* assertions; never
expose additional archived facts, infer identities, or use a question to merge.
"""
from copy import deepcopy
import json
import re

VERSION = 'demo-evidence-revision-v2-constrained'
PARTICIPLES = dict(walking='walks', holding='holds', touching='touches',
    picking='picks', putting='puts', placing='places', moving='moves',
    washing='washes', wiping='wipes', mixing='mixes', cutting='cuts',
    looking='looks', turning='turns', typing='types', scrolling='scrolls',
    writing='writes', carrying='carries', adjusting='adjusts', weaving='weaves')

SYSTEM = '''Answer a multiple-choice video question using only the supplied evidence.
Captions are sparse and may be noisy. Auxiliary memory is caption-derived, not independent visual verification.
First provide a short factual check for each option's distinguishing claims, citing evidence IDs.
Use supported, partial, contradicted, or unknown. Missing evidence is unknown, not a contradiction.
Treat an option as partial when some of its claims are observed and other claims remain unverified.
Compare the strength of positive support and unsupported distinguishing claims; do not discard all options just because none is completely verified.
For overall purpose or main activity, consider how observed steps fit a task across available time segments.
Do not count duplicate captions as independent votes, infer unseen duration across gaps, or let an incidental action alone determine overall purpose.
Do not infer that an activity occurred merely because it would be a plausible next step. Use next-step prediction only if the question asks for it.
Keep observed devices, actors, actions, tools and purposes distinct. Do not invent unobserved actions or purposes to complete an option.
Finally select exactly one option ID, keeping it consistent with the factual checks and explanation. Person names such as C are not option IDs.
Return only one JSON object, in this key order:
{"option_checks":{"A":{"basis":"one short factual sentence","evidence_ids":["E001"],"status":"partial"},"B":{...},"C":{...},"D":{...},"E":{...}},
"decisive_evidence_ids":["E001"],"selected_option":"A","explanation":"one concise evidence-based sentence","confidence":60}.
For every option, basis is at most 140 characters and evidence_ids contains at most two supplied IDs. Do not enumerate all captions.
Use at most three decisive_evidence_ids and an explanation of at most 180 characters.
Use empty evidence_ids when no evidence supports a claim. Never fabricate an evidence ID.
The selected_option must be one of A,B,C,D,E even when evidence is insufficient. Do not add option text to this field.'''


def canonical_body(body):
    # Structured field values retain their exact semantics and ordering.
    if re.match(r'^(Action|Attribute|State|Relation|Change|Result|Event link):', body):
        return re.sub(r'\s+', ' ', body).strip()
    text = re.sub(r'^\s*#C\s+', '', body)
    text = re.sub(r'\s+', ' ', text).strip().casefold().rstrip('.!?')
    for participle, finite in PARTICIPLES.items():
        text = re.sub(r'\b(?:is|are) '+participle+r'\b', finite, text)
    # Articles do not establish cross-sample physical identity. Possessives,
    # numerals, negation, sides, tools, objects and all other words remain.
    text = re.sub(r'\b(?:a|an|the)\s+', '', text)
    text = re.sub(r'\b(?:cell phone|mobile phone)\b', 'cellphone', text)
    return text


def compact_rows(rows):
    """Merge only adjacent, touching samples with equivalent normalized bodies."""
    groups = []
    for index, row in enumerate(rows):
        key = canonical_body(row['body'])
        if groups and groups[-1]['key'] == key and groups[-1]['interval'][1] == row['interval'][0]:
            group = groups[-1]
            group['interval'][1] = row['interval'][1]
            group['source_indices'].append(index)
        else:
            groups.append(dict(key=key, body=row['body'], interval=deepcopy(row['interval']), source_indices=[index]))
    lines, evidence = [], []
    for index, group in enumerate(groups, 1):
        eid = f'E{index:03}'
        a,b = group['interval']
        lines.append(f'[{eid}] Frames [{a},{b}): '+group['body'])
        evidence.append(dict(evidence_id=eid, representative_text=group['body'],
            display_interval=group['interval'], sources=[deepcopy(rows[i]) for i in group['source_indices']]))
    return '\n'.join(lines), dict(version=VERSION, source_rows=len(rows), rendered_rows=len(groups),
        lexical_paraphrase_groups=sum(len({rows[i]['body'] for i in g['source_indices']})>1 for g in groups),
        rows=evidence, question_blind=True, source_order_preserved=True, identities_merged=False,
        policy='Local touching samples only; whitespace/articles/progressive tense and cellphone spelling; no broad synonym or event inference.')


def make_input(question, description, original, auxiliary):
    options = '\n'.join(f'{chr(65+i)}: {v}' for i,v in enumerate(question['options']))
    # The old auxiliary block remains verbatim, with its caption-derived caveat.
    body = ('Evidence (IDs refer to observed caption samples; merged intervals only abbreviate adjacent samples):\n'
        +description+'\n\nQuestion: '+question['question']+'\nOptions:\n'+options)
    if auxiliary:
        body += '\n\n'+auxiliary
    body += '\n\nReturn the JSON object specified by the system instruction.'
    return dict(query_id=original['query_id'], head=SYSTEM, prompts=[body])


def parse_answer(raw, valid_ids):
    clean = raw.strip()
    if clean.startswith('```') and clean.endswith('```'):
        clean = re.sub(r'^```(?:json)?\s*', '', clean)[:-3].strip()
    value = json.loads(clean)
    choice = value.get('selected_option')
    if not isinstance(choice,str) or choice not in list('ABCDE'):
        raise ValueError('selected_option must be an exact option ID')
    checks = value.get('option_checks')
    if isinstance(checks,dict) and set(checks)==set('ABCDE'):
        checks=[dict(option_id=o,**checks[o]) for o in 'ABCDE']
    if not isinstance(checks,list) or len(checks)!=5 or {c.get('option_id') for c in checks}!=set('ABCDE'):
        raise ValueError('Expected exactly one factual check per option')
    citations = list(value.get('decisive_evidence_ids',[]))
    for check in checks:
        if check.get('status') not in {'supported','partial','contradicted','unknown'}:
            raise ValueError('Invalid option support status')
        citations.extend(check.get('evidence_ids',[]))
    if any(c not in valid_ids for c in citations):
        raise ValueError('Fabricated evidence ID')
    return dict(option=str(ord(choice)-65), method='strict_json_option_id', answer=value)


def output_schema(valid_ids):
    def obj(properties):
        return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)
    def refs(limit):
        return dict(type='array',items=dict(type='string',enum=sorted(valid_ids)),maxItems=limit)
    check=obj(dict(basis=dict(type='string',maxLength=140),evidence_ids=refs(2),
        status=dict(type='string',enum=['supported','partial','contradicted','unknown'])))
    return obj(dict(option_checks=obj({o:deepcopy(check) for o in 'ABCDE'}),
        decisive_evidence_ids=refs(3),selected_option=dict(type='string',enum=list('ABCDE')),
        explanation=dict(type='string',maxLength=180),confidence=dict(type='integer',minimum=0,maximum=100)))
