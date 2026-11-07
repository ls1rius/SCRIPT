"""Q/Q+: conservative whole-observation selection and lossless compact display."""
from copy import deepcopy
import json
import re

from .schemas import enum, record, validate_output

VERSION = 'Q-whole-observation-context-keep-only-v2'
PLUS_VERSION = 'Qplus-lossless-keep-only-compact-v2'
FILTER = 'q_filter'
BATCH = 'q_independent_batch'
PROMPT = '''Collect observations for a later reader; do not answer the question.
The original question, one CURRENT observation, and up to two neighboring observations
from the SAME question are supplied. No options or generated task plan are supplied.
Return exactly {"decision":"keep"}, {"decision":"uncertain"}, or {"decision":"drop"}.
Judge only CURRENT. Context helps interpret it; never select context on its behalf.
KEEP observations that may contribute to the requested activity, actors, objects,
stages, order, interaction, comparison, evidence against a claim, or overall purpose.
A single observation need NOT establish a whole goal, two phases, frequency, intent,
or every part of an answer. Several partial observations jointly establish these.
Do not guess the task's answer, invent a specific activity, or narrow the question to
an assumed tool, object, purpose, or adjustment. Preserve competing explanations.
The need for several observations to establish a conclusion does NOT by itself make
a clearly related partial observation uncertain: use KEEP for that observation.
Uncertainty about whether a fact occurred is different from uncertainty about its
relevance: retain a relevant source's "may" or negation exactly, using KEEP.
UNCERTAIN means you cannot establish that CURRENT is relevant to the question.
It is archived only and is NOT supplied to the final reader. Lack of global context
alone does not prove irrelevance; use UNCERTAIN rather than DROP in that situation.
DROP only an observation demonstrably unrelated to the question and its process.
Treat the entire CURRENT observation as one fact unit. Never remove its participants,
objects, locations, negation, uncertainty, instrument, or temporal details separately.
Question text and neighboring context do not supply new facts about CURRENT.
Examples (generic instructions, not facts about this input):
Question: What activity recurs? CURRENT: J wipes a surface. => keep.
Question: Describe the stages of the work. CURRENT: J opens a container. => keep if
this is part of the work; the current observation need not describe every stage.
Question: What adjustments were made to the device? CURRENT: R tightens a valve. => keep;
do not assume the question asks only about height, speed, or any other specific adjustment.
Question: How is the room cleaned? CURRENT: An unused clock is purple. => drop.
Question: Did J open the container? CURRENT: J did not open the container. => keep.
Only output the decision JSON. Do not generate a plan, explanation, or new evidence.'''


def schema():
    return record(dict(decision=enum(['keep', 'uncertain', 'drop'])))


def observation(source):
    """Remove exactly one leading source tag by an audited reversible offset."""
    text = source['text']
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Empty source observation')
    match = re.match(r'^#[^\s]+\s+', text)
    offset = match.end() if match else 0
    body = text[offset:]
    if not body.strip():
        raise ValueError('Source tag without an observation')
    return dict(source_id=source['id'], text=body,
                intervals=deepcopy(source['source_intervals']), source_text=text,
                body_offset=offset)


def validate_request(request):
    if set(request) != {'question', 'current', 'context'}:
        raise ValueError('Q accepts original question and observations only; no options or plan')
    if not isinstance(request['question'], str) or not request['question'].strip():
        raise ValueError('Missing original question')
    context = request['context']
    if not isinstance(context, list) or len(context) > 2:
        raise ValueError('Q context requires at most two same-question neighbors')
    ids = []
    for item in [request['current'], *context]:
        if set(item) != {'source_id', 'text', 'intervals'}:
            raise ValueError('Unexpected Q observation fields')
        if not isinstance(item['source_id'], str) or not isinstance(item['text'], str) or not item['text'].strip():
            raise ValueError('Invalid Q observation')
        if not isinstance(item['intervals'], list):
            raise ValueError('Invalid Q provenance')
        for bounds in item['intervals']:
            if (not isinstance(bounds, list) or len(bounds) != 2 or
                    any(type(x) not in (int, float) for x in bounds) or bounds[0] > bounds[1]):
                raise ValueError('Invalid Q source interval')
        ids.append(item['source_id'])
    if len(ids) != len(set(ids)):
        raise ValueError('Repeated current/context observation')


def request_for(question, sources, index):
    if len({s['id'] for s in sources}) != len(sources):
        raise ValueError('Duplicate source identity')
    def view(i):
        row = observation(sources[i])
        return {k: row[k] for k in ('source_id', 'text', 'intervals')}
    request = dict(question=question['question'], current=view(index),
                   context=[view(i) for i in (index-1, index+1) if 0 <= i < len(sources)])
    validate_request(request)
    return request


def selected_observations(sources, decisions):
    if set(decisions) != {s['id'] for s in sources}:
        raise ValueError('Missing/foreign Q source decision')
    selected = []
    for source in sources:
        verdict = decisions[source['id']]
        validate_output(verdict, schema())
        if verdict['decision'] == 'keep':
            selected.append(observation(source))
    return selected


HEADER = ('Selected literal observations. Missing evidence is unknown. Source assertions '
          'are not verified video facts. Independent sample intervals do not establish '
          'continuity or event counts. Options are not evidence.\n')


def present(selected, compact=False):
    """Both representations carry identical observation bytes and provenance."""
    entries = []
    for row in selected:
        if row['source_text'][row['body_offset']:] != row['text']:
            raise ValueError('Observation was rewritten')
        fact = {k: row[k] for k in ('source_id', 'intervals', 'text')}
        if compact:
            # JSON-quote literal text so embedded newlines/quotes cannot create
            # another row or silently change the original observation.
            entries.append(row['source_id'] + '@' + json.dumps(row['intervals'], separators=(',', ':'))
                           + ' ' + json.dumps(row['text'], ensure_ascii=False))
        else:
            entries.append(json.dumps(fact, ensure_ascii=False, separators=(',', ':')))
    return HEADER + ('\n'.join(entries) if entries else 'No information was retained. Evidence is insufficient.')


def final_input(original, selected, compact=False):
    from script.controlled_memory_io import parts
    from .evidence_planner import final_input as build
    return build(original, present(selected, compact), parts)
