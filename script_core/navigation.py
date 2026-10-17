"""Distinct flat baseline policies; all methods retain the same tools and stop gate."""
from copy import deepcopy


def text_feedback(result):
    if not result:
        return ''
    lines = [f"Observation result: {result['task_status']}"]
    for label, field in [('Supported facts', 'supported_claim_ids'), ('Refuted facts', 'refuted_claim_ids'),
                         ('Read sources', 'evidence_ids'), ('Checked coverage', 'coverage_ids')]:
        lines.append(label+': '+', '.join(result.get(field, [])))
    for gap in result.get('unresolved', []):
        lines.append(f"Unresolved {gap['requirement_id']}: {gap['reason']}")
    for gap in result.get('observation_gaps', []):
        lines.append(f"Observation gap in {gap['interval']}: {gap['reason']}")
    for lead in result.get('next_leads', []):
        lines.append(f"Possible {lead['route']} route to {lead['scene_id']} from {', '.join(lead['basis_refs'])}")
    return '\n'.join(lines)


def delivered_feedback(result, cfg):
    feedback = deepcopy(result)
    if not cfg['ablations']['feedback_unresolved']:
        feedback['unresolved'] = []
        feedback['observation_gaps'] = []
    if not cfg['ablations']['feedback_next_leads']:
        feedback['next_leads'] = []
    return feedback


def baseline_input(method, inputs, frontier, memory, state):
    """B0 free iterative retrieval, B1 explicit routing/text, B1S full structured global state."""
    common = {'method': method, 'question': inputs['question'], 'variables': inputs['variables'],
              'candidates': frontier, 'bindings': inputs['bindings'],
              'claims': [c for c in memory.tables['claims'].values() if memory.is_valid(c['id'])],
              'identity_links': [l for l in memory.tables['identity_links'].values() if memory.is_valid(l['id'])],
              'summaries': list(memory.tables['summaries'].values()),
              'available_tools': ['summary_retrieval', 'window_retrieval', 'entity_index_known_only',
                                  'time_lookup', 'observe', 'detect_track', 'extract_verify']}
    if method == 'B1S':
        common.update(inputs, requirements=list(memory.tables['requirements'].values()),
                      previous_results=state.get('last_result'),
                      coverage=list(memory.tables['coverage'].values()),
                      branches=list(memory.tables['branches'].values()))
    else:
        common['feedback_text'] = text_feedback(state.get('last_result'))
        common['check_description'] = (f"Check {inputs['requirement']['predicate']} "
                                       f"in {inputs['requirement']['scope']}; {inputs['requirement']['quantifier']}")
        # Technical tool arguments remain available; these variants do not receive a JSON requirement ledger.
        common['candidates'] = [{k: v for k, v in c.items() if k not in
                                ('priority_class', 'basis_rank', 'branch_id')} for c in frontier]
        if method == 'B0':
            common['routing_policy'] = 'Choose freely among all legal tool routes; revise retrieval using observed clues.'
        else:
            common['routing_policy'] = ('Use confirmed entity successors first when they address the goal; '
                'use chronological neighbors for temporal requirements; fall back to summary retrieval for index gaps. '
                'known_only never proves absence. Preserve contradicted and unvisited alternatives.')
            common['recommended_candidate_id'] = inputs['candidate']['id']
    return common
