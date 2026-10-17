"""Independent short answers, reversible option presentation and legacy comparison."""
from copy import deepcopy
from .config import digest


def prepare_answer_inputs(inputs, strategy='direct', order='content_hash', seed=0):
    """Present choices without tying a semantic answer to its dataset position.

    Sort on question and option text, never labels or model judgments. The same
    semantic options have the same presentation after a dataset permutation.
    The returned map stays with the caller, outside the model's input.
    """
    result = deepcopy(inputs)
    result['answer_strategy'] = strategy
    for table in ('claims', 'evidence'):
        if table in result:
            result[table].sort(key=lambda row: (row.get('interval', [0., 0.]), row.get('id', '')))
    ids = list(inputs.get('option_ids', []))
    if inputs.get('answer_mode') != 'choice':
        return result, []
    if ids != [str(i) for i in range(len(inputs['options']))]:
        raise ValueError('Canonical option IDs must match the original option list')
    positions = list(range(len(ids)))
    if order == 'content_hash':
        positions.sort(key=lambda i: (digest([seed, inputs['question'], inputs['options'][i]]), i))
    elif order != 'original':
        raise ValueError('Unknown answer option order')
    presented_to_original = [ids[i] for i in positions]
    result['options'] = [inputs['options'][i] for i in positions]
    # A gate candidate is a hypothesis, not an answer hint. Preserve only the
    # stop status for the independent final decision; compare canonically later.
    decision = result.get('stop_decision', {})
    for field in ('candidate_option', 'selected_option'):
        decision.pop(field, None)
    return result, presented_to_original


def restore_answer_order(report, presented_to_original):
    result = deepcopy(report)
    if not presented_to_original:
        return result
    presented = result['selected_option']
    if presented not in [str(i) for i in range(len(presented_to_original))]:
        raise ValueError('Answer uses an illegal presented option ID')
    result['selected_option'] = presented_to_original[int(presented)]
    if 'option_alignment' in result:
        result['option_alignment'] = {presented_to_original[int(k)]: v
                                      for k, v in result['option_alignment'].items()}
    if 'alignment_decision' in result:
        decision = result['alignment_decision']
        for key in ('model_selected_option', 'selected_option'):
            decision[key] = presented_to_original[int(decision[key])]
        decision['matching_options'] = [presented_to_original[int(k)] for k in decision['matching_options']]
    result['choice_presentation'] = {'presented_to_original': presented_to_original,
                                      'model_selected_option': presented}
    return result


def reconcile_answer(report, inputs):
    """Enforce internal consistency, without claiming semantic entailment.

    A unique match can correct an inconsistent option ID. Multiple matches and
    missing observations remain uncertain; they never create a proof or facts.
    """
    result = deepcopy(report)
    if inputs.get('answer_mode') != 'choice' or not inputs.get('options'):
        return result
    if inputs.get('answer_strategy') == 'direct':
        if result['selected_option'] not in inputs['option_ids']:
            raise ValueError('Final answer uses an illegal option ID')
        return result
    checks = result.get('option_alignment', {})
    allowed = inputs['option_ids']
    if set(checks) != set(allowed) or result['selected_option'] not in allowed:
        raise ValueError('Final option comparison must cover every legal option exactly once')
    valid_claims = {c['id'] for c in inputs.get('claims', [])}
    if any(not set(row['claim_ids']) <= valid_claims for row in checks.values()):
        raise ValueError('Final option comparison cites an unseen claim')
    matches = [key for key in allowed if checks[key]['match'] == 'matches']
    original = result['selected_option']
    selected = matches[0] if len(matches) == 1 else original
    corrected = selected != original
    result['alignment_decision'] = {'model_selected_option': original, 'selected_option': selected,
                                    'corrected': corrected, 'matching_options': matches}
    if corrected:
        result['selected_option'] = selected
        result['text'] = (result['evidence_summary'] + '; ' + checks[selected]['reason'])[:160]
        result['claim_ids'] = checks[selected]['claim_ids'][:]
        # The original final citations may explain the discarded choice.
        result['evidence_ids'] = []
    return result
