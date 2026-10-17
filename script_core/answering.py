"""Keep the final option consistent with its preceding contrastive comparison."""
from copy import deepcopy


def reconcile_answer(report, inputs):
    """Enforce internal consistency, without claiming semantic entailment.

    A unique match can correct an inconsistent option ID. Multiple matches and
    missing observations remain uncertain; they never create a proof or facts.
    """
    result = deepcopy(report)
    if inputs.get('answer_mode') != 'choice' or not inputs.get('options'):
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
