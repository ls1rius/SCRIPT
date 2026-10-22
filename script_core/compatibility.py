"""Configurable VideoTree components inside SCRIPT's query controller.

All variants share SCRIPT Memory, Meter, Materializer and result lifecycle.
With extensions off the only inference is original width -> depth -> original QA.
"""
from copy import deepcopy
from pathlib import Path

from .budget import ToolFailure, ResourceExhausted
from .config import digest
from .state import obj
from .accounting import metered_cpu


def qa_inputs(query, captions, selected, claims=()):
    from prompts import PromptFactory
    factory = PromptFactory().get('qa_standard')
    item = {'uid': query['id'], 'question': query['question'], 'narration': [c['text'] for c in captions],
            'duration': len(captions), **{f'option{chr(65+i)}': t for i, t in enumerate(query['options'])}}
    prompts = factory.fill(**item, fps=1., clip_length=1., num_words=300, examplars=None, loc_pred=sorted(selected))
    if claims:
        prompts[-1] += '\n\nAdditional local visual observations (valid only for their stated intervals; not inferred goals):\n'
        prompts[-1] += '\n'.join(f"[{c['interval'][0]:g},{c['interval'][1]:g}) {c['description']}" for c in claims)
    return {'query_id': query['id'], 'head': factory.head, 'prompts': prompts}


def add_graph_seconds(context, selected, maximum):
    result = set(selected)
    if context:
        events = sorted(context['events'], key=lambda e: (-e.get('retrieval_score', 0), e['interval']))
        for event in events:
            for second in range(int(event['interval'][0]), int(event['interval'][1])):
                if second not in result:
                    result.add(second)
                    if len(result)-len(selected) >= maximum:
                        return sorted(result)
    return sorted(result)


def eligible_visual_claims(memory):
    return [c for c in memory.tables['claims'].values() if memory.is_valid(c['id'])
            and c.get('source_type') == 'visual' and c.get('fact_type') != 'inference']


def protected_claims(claims):
    return [c for c in claims if c.get('fact_type') == 'action'
            and c.get('verification', {}).get('whole_claim_supported') is True
            and c['verification'].get('process_visible')
            and not c['verification'].get('unresolved_conflicts')
            and len(set(c.get('source_frame_ids', []))) >= 3]


def observe_checks(memory, query, cfg, meter, output, checks, captions):
    from .materialize import Materializer
    materializer = Materializer(memory, meter, cfg, query['id'], output)
    sources = {c['id']: c for c in captions}
    requirement = obj(memory.new_id(query['id'], 'requirement'), predicate='other', roles={},
        scope=query['scope'], quantifier='global', prerequisite_ids=[], acceptable_modalities=['visual'],
        answer_slot='choice_distinction', support_sets=[], status='unresolved')
    memory.put('requirements', requirement)
    visited = []
    errors = []
    for check in checks[:cfg['compatibility']['max_visual_reads']]:
        source = sources[check['caption_id']]
        point = sum(source['interval'])/2
        window = next((w for w in memory.tables['windows'].values() if w['interval'][0] <= point < w['interval'][1]), None)
        if window is None:
            continue
        interval = window['interval']
        if any(min(interval[1], old[1])-max(interval[0], old[0]) >= .5*min(interval[1]-interval[0], old[1]-old[0]) for old in visited):
            continue
        visited.append(interval)
        scene_ids = [s['id'] for s in memory.tables['scenes'].values() if max(s['interval'][0], interval[0]) < min(s['interval'][1], interval[1])]
        task = obj(memory.new_id(query['id'], 'task'), candidate_id=None, scene_ids=scene_ids,
            allowed_interval=interval, requirement_ids=[requirement['id']], target_binding_ids=[],
            goal='Inspect this unresolved detail without assuming an answer: '+check['question'],
            acceptance_checks=['Record only visible distinctions; keep uncertain identity and purpose unknown.'],
            route='summary', basis_refs=[window['id']], relevant_versions={}, max_new_observations=1,
            cost_cap=min(cfg['budget']['local_task_cap'], meter.ledger.remaining),
            extraction_scope={'predicates': ['other'], 'target_occurrence_ids': [], 'include_context': True, 'mode': 'goal'},
            target_signature=digest([query['id'], interval, check['question']]), question=query['question'],
            options=query['options'], variables=[])
        memory.put('tasks', task)
        try:
            materializer.execute(task, [{'interval': interval}], interpret=False)
        except (ToolFailure, ResourceExhausted, ValueError) as exc:
            errors.append(str(exc))
    return {'intervals': visited, 'errors': errors}


@metered_cpu('compatibility_control')
def run_pipeline(memory, query, metadata, cfg, meter, output, state, summary_graph=None):
    from .video import load_captions
    from .summary_graph import source_records, retrieve_graph
    from .answering import prepare_answer_inputs, restore_answer_order
    options = cfg['compatibility']
    if query['answer_mode'] != 'choice' or len(query['options']) != 5 or query['scope'] != [0., memory.manifest['cutoff']]:
        raise ValueError('VideoTree compatibility currently requires full-video EgoSchema five-choice queries')
    spans = load_captions(memory.manifest)['data']
    captions = source_records(spans, memory.manifest['cutoff'])
    if [c['interval'] for c in captions] != [[i, i+1] for i in range(len(captions))]:
        raise ValueError('VideoTree compatibility requires the original one-second caption grid')
    feature = Path(options['feature_directory'])/f'{query["video_id"]}.pt'
    if not feature.is_absolute():
        feature = Path(__file__).resolve().parents[1]/feature
    width_input = {'query_id': query['id'], 'query': {k: query[k] for k in ('id', 'question', 'options')},
                   'captions': [c['text'] for c in captions], 'duration': len(captions), 'feature_uri': str(feature.resolve())}
    raw, _ = meter.call('videotree_width', width_input, build=True)
    width = raw['data']
    raw, _ = meter.call('videotree_depth', {'query_id': query['id'], 'feature_uri': str(feature.resolve()),
        'width': width['width'], 'relevance': {'cluster_relevance': width['relevance']['cluster_relevance']}}, build=True)
    selected = sorted(raw['data']['selected_seconds'])
    baseline_inputs = qa_inputs(query, captions, selected)
    raw, _ = meter.call('videotree_qa', baseline_inputs)
    baseline = raw['data']
    candidate = deepcopy(baseline)
    diagnostics = {'engine': 'videotree_in_script', 'extensions': deepcopy(options),
        'baseline_selected_seconds': selected, 'baseline_qa': deepcopy(baseline_inputs),
        'baseline_prediction': baseline['selected_option'], 'baseline_prompt_tokens': baseline['info']['prompt_tokens'],
        'extension_errors': [], 'accepted_change': False}
    state['compatibility'] = diagnostics
    original_selected = list(selected)
    if options['graph_retrieval']:
        if summary_graph is None:
            raise ValueError('Graph retrieval requires a provided caption graph')
        context = retrieve_graph(summary_graph, query)
        selected = add_graph_seconds(context, selected, options['max_added_captions'])
        diagnostics['graph_added_seconds'] = sorted(set(selected)-set(original_selected))
    plan = {'gap': '', 'caption_ids': [], 'visual_checks': []}
    if options['retrieval_feedback'] or options['visual_verification']:
        planner = {'question': query['question'], 'options': query['options'], 'captions': captions,
            'selected_caption_ids': [captions[i]['id'] for i in selected],
            'current_answer': candidate['selected_option'],
            'max_caption_additions': options['max_added_captions'] if options['retrieval_feedback'] else 0,
            'max_visual_checks': options['max_visual_reads'] if options['visual_verification'] else 0}
        try:
            plan, _ = meter.json('videotree_feedback', planner, ('gap', 'caption_ids', 'visual_checks'))
            diagnostics['feedback'] = plan
        except (ToolFailure, ResourceExhausted) as exc:
            diagnostics['extension_errors'].append(str(exc))
        if options['retrieval_feedback']:
            by_id = {c['id']: i for i, c in enumerate(captions)}
            selected = sorted(set(selected) | {by_id[c] for c in plan['caption_ids']})
    if options['visual_verification'] and plan['visual_checks']:
        diagnostics['visual_inspection'] = observe_checks(memory, query, cfg, meter, output, plan['visual_checks'], captions)
    claims = eligible_visual_claims(memory)
    candidate_claim_ids = []
    if options['final_qa'] == 'script':
        inputs = {'question': query['question'], 'options': query['options'], 'option_ids': list('01234'),
            'answer_mode': 'choice', 'caption_timeline': [captions[i] for i in selected],
            'caption_status': 'unverified_descriptions', 'claims': claims,
            'evidence': [e for e in memory.tables['evidence'].values() if e.get('online')],
            'requirements': list(memory.tables['requirements'].values()), 'bindings': [], 'identity_links': [],
            'query_facts': [], 'observation_gaps': [],
            'stop_decision': {'reason': 'compatibility_ablation', 'evidence_status': 'insufficient'}}
        inputs, mapping = prepare_answer_inputs(inputs, 'direct', cfg['query']['answer_order'], cfg['seed'])
        try:
            report, _ = meter.json('answer', inputs, ('text', 'selected_option', 'claim_ids', 'evidence_ids'))
            candidate = restore_answer_order(report, mapping)
            candidate_claim_ids = candidate['claim_ids']
        except (ToolFailure, ResourceExhausted) as exc:
            diagnostics['extension_errors'].append(str(exc))
    elif selected != original_selected or claims:
        try:
            raw, _ = meter.call('videotree_qa', qa_inputs(query, captions, selected, claims))
            candidate = raw['data']
        except (ToolFailure, ResourceExhausted) as exc:
            diagnostics['extension_errors'].append(str(exc))
    chosen = candidate
    if options['answer_protection'] and candidate['selected_option'] != baseline['selected_option']:
        chosen = baseline
        eligible = protected_claims(claims)
        if eligible:
            try:
                report, _ = meter.json('summary_graph_guard', {'question': query['question'], 'options': query['options'],
                    'baseline_option': baseline['selected_option'], 'candidate_option': candidate['selected_option'],
                    'claims': eligible}, ('reason', 'claim_ids', 'resolves_distinction', 'accept'))
                diagnostics['guard'] = report
                if report['accept'] and report['resolves_distinction'] and report['claim_ids']:
                    chosen = candidate
                    candidate_claim_ids = report['claim_ids']
                    diagnostics['accepted_change'] = True
            except (ToolFailure, ResourceExhausted) as exc:
                diagnostics['extension_errors'].append(str(exc))
    if chosen is baseline:
        candidate_claim_ids = []
    diagnostics.update(final_selected_seconds=selected, candidate_prediction=candidate['selected_option'],
                       qa_context_claim_ids=[c['id'] for c in claims])
    state['answer_source'] = 'videotree' if chosen is baseline else options['final_qa']
    decision = obj(memory.new_id(query['id'], 'stop'), reason='exploration_stop', evidence_status='insufficient',
        checked_requirement_ids=[], remaining_gap_refs=[], gate_hash=digest(options),
        actual_cost=meter.ledger.actual, budget_violation=meter.ledger.budget_violation,
        exploration_limit='configured_compatibility_components_complete')
    memory.put('stop_decisions', decision)
    answer = obj(memory.new_id(query['id'], 'answer'), query_id=query['id'],
        selected_option=chosen['selected_option'], text=query['options'][int(chosen['selected_option'])],
        explanation=chosen['text'], claim_ids=candidate_claim_ids, evidence_ids=[], stop_decision_id=decision['id'])
    memory.put('answers', answer)
    return {'answer': answer, 'stop_decision': decision, 'compatibility': diagnostics}
