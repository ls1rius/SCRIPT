"""N-only option-wise scene review; program derives the closed candidate union."""
from copy import deepcopy
import re

from .schemas import record, short_text, validate_output

VERSION = 'N-isolated-scene-option-pairs-v5-reason-first'
OPERATION = 'n_select_scene'
KEYS = ('scene_reviews',)
PROMPT = '''Read the actual QUESTION, ALL answer OPTIONS, focus_option and the supplied scene summary.
Select places to inspect, not an answer. Options are competing hypotheses, never
question premises or observed facts. Do not use the first or any favored option
as the sole search target. The controller reviews every scene/option pair separately.
For THIS call assess only focus_option.text, with the other options as alternatives.
focus_option is a hypothesis to inspect, NOT a known answer or observed event.

Inspect the ONE supplied scene. Its full description and literal summary_clauses belong
only to that scene. Do not transfer activities from another scene or an option
into this scene. Return scene_reviews for the supplied scene, containing option_reviews
with only the requested focus_option.option_id. Do not evaluate another option instead.
For each scene/option pair return:
reason FIRST: name what the scene explicitly describes, then whether it contains
the observable behavior needed to inspect focus_option. Separate an option's
observable action from its proposed purpose. A guessed purpose cannot establish
a match to an unobserved action. Do not restate the option as an observation.
Unrelated actions are not evidence just because an imagined intention could connect them;
priority: integer 0..100. Zero means unrelated, positive means worth inspecting;
anchor LAST: copy a literal TEXT value from THIS scene's summary_clauses to cite
the observation just discussed, or null when no relevant observation exists.
Do not choose an arbitrary quote first and then invent a justification for it.
Larger priorities mean stronger inspection priority, not answer confidence. Null
anchors require zero priority; positive priorities require an actual local anchor.
A non-null anchor may and SHOULD receive zero when the quoted content is unrelated.
Quoting a source is not a reason to include it. Keep each reason concise and finish
the explanation within 20 words. Do not speculate that an unmentioned action might
happen in this setting. A place where an action could hypothetically occur is NOT
an observed cue. Unrelated background cannot refute an option about another event.
Use 80..100 for concrete interactions directly addressing distinctive option
claims, 30..70 for partial relevant context, and 0 for unrelated content.
An inferred purpose is not an observed activity. General locomotion alone should
not outrank explicit task-specific object interactions when comparing such claims.

A scene is useful if its described activity, object interaction, condition or
sequence can supply evidence for OR AGAINST a distinguishing claim. It need not
prove the whole option, its inferred purpose, or the whole-video pattern yet.
A description of a competing activity can distinguish options; it is not
irrelevant merely because it differs from this option. Shared relevant steps
can also warrant inspection when the question asks about those steps. Conversely,
generic background or a shared room/person alone is not useful evidence.
An omitted detail in a summary does not prove it never occurred. Inspect relevant
activity whose details need checking; do not require exact wording matches.

The program includes a scene iff ANY option review gives it positive priority,
and ranks it by its maximum priority, ties in directory order. No separate
global exclusion or answer choice is requested. Review all options even if
earlier ones have no useful scenes. If no summary offers useful evidence for
any option, all zeros is valid. No quota, time-coverage rule, or automatic full
selection. Summaries are navigation hints, not final-answer evidence.
'''


def prepare_inputs(inputs):
    """Index literal clauses without changing or replacing any original summary."""
    result = deepcopy(inputs)
    for scene in result['directory']:
        clauses = [m.group().strip() for m in re.finditer(r'[^。！？；.!?;\n，,]+[。！？；.!?;\n，,]*', scene['description'])
                   if m.group().strip()]
        if not clauses:
            raise ValueError('N selection needs nonempty scene descriptions')
        indexed = {f'E{i}': clause for i, clause in enumerate(clauses)}
        if 'summary_clauses' in scene and scene['summary_clauses'] != indexed:
            raise ValueError('Summary clause index does not match original text')
        scene['summary_clauses'] = indexed
    return result


def output_schema(operation, inputs):
    if operation != OPERATION:
        raise ValueError('Unknown N scene-selection operation')
    options = inputs['options']
    ids = [scene['id'] for scene in inputs['directory']]
    if not options or not ids or len(ids) != len(set(ids)):
        raise ValueError('N selection needs options and a nonempty unique scene directory')
    reason = short_text(96)
    reason['minLength'] = 1
    scenes = {}
    focus = inputs.get('focus_option')
    if focus is not None:
        index = next((i for i in range(len(options)) if focus == dict(option_id=f'O{i}',text=options[i])),None)
        if index is None:
            raise ValueError('Focus option must exactly match one original option and ID')
        option_indices = [index]
    else:
        option_indices = range(len(options))
    for scene in prepare_inputs(inputs)['directory']:
        review = dict(anyOf=[
            record(dict(reason=deepcopy(reason),priority=dict(type='integer',enum=list(range(101))),
                        anchor=dict(type='string',enum=list(scene['summary_clauses'].values())))),
            record(dict(reason=deepcopy(reason),priority=dict(type='integer',enum=[0]),anchor=dict(type='null')))])
        scenes[scene['id']] = record(dict(option_reviews=record({f'O{i}':deepcopy(review) for i in option_indices})))
    return record(dict(scene_reviews=record(scenes)))


def normalize_selection(raw, inputs):
    """No semantic relabeling: union positive reviews, preserving all raw reasons."""
    validate_output(raw, output_schema(OPERATION, inputs))
    decisions = {}
    for scene in inputs['directory']:
        sid = scene['id']
        reviews = raw['scene_reviews'][sid]['option_reviews']
        scores = {oid: review['priority'] for oid, review in reviews.items()}
        if any(not review['reason'].strip() for review in reviews.values()):
            raise ValueError('N option-scene review needs a nonblank reason')
        score = max(scores.values())
        decisions[sid] = dict(relevance='candidate' if score else 'excluded', priority=score,
            reason=('Positive reviews: '+', '.join(f'{oid}={value}' for oid, value in scores.items() if value))
                if score else 'All option reviews assigned zero; individual model reasons are archived.')
    return dict(scene_decisions=decisions,
        reason='Program union of positive option-scene reviews; maximum priority, ties in original directory order.')


def review_scenes(inputs, meter, on_review=None):
    """Independent pairs avoid both scene leakage and switching option IDs mid-output."""
    prepared = prepare_inputs(inputs)
    combined = dict(scene_reviews={})
    for scene in prepared['directory']:
        combined['scene_reviews'][scene['id']] = dict(option_reviews={})
        for index,text in enumerate(prepared['options']):
            request = dict(prepared, directory=[deepcopy(scene)],focus_option=dict(option_id=f'O{index}',text=text))
            output, call = meter.json(OPERATION, request, KEYS)
            validate_output(output, output_schema(OPERATION, request))
            combined['scene_reviews'][scene['id']]['option_reviews'].update(output['scene_reviews'][scene['id']]['option_reviews'])
            if on_review:
                on_review(scene['id'], request, output, call)
    validate_output(combined, output_schema(OPERATION, prepared))
    return combined
