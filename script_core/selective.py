"""Script-guided, bounded inspection; stopping here never creates a proof."""
from .caption_context import caption_context
from .coverage import intersect
from .query_policy import strict_question


def eligible(memory, query, cfg):
    requirements = list(memory.tables['requirements'].values())
    return (cfg['query']['reading_policy'] == 'script_guided'
            and cfg['construction']['strategy'] == 'goal_lazy'
            and cfg['method'] in ('B2', 'B3') and query.get('answer_mode', 'choice' if query.get('options') else 'open') == 'choice'
            and not strict_question(query['question']) and bool(requirements)
            and all(r['quantifier'] == 'global' and not r['roles'] for r in requirements))


def script_outline(memory, query, cfg):
    rows = [{'scene_id': s['scene_id'], 'interval': memory.get(s['scene_id'])['interval'],
             'text': s['text'], 'status': s['status']}
            for s in memory.tables['summaries'].values()
            if intersect(memory.get(s['scene_id'])['interval'], query['scope'])]
    maximum = max(1, cfg['retrieval']['all_summaries_if_scene_count_le'])
    selected = rows if len(rows) <= maximum else [rows[int((i+.5)*len(rows)/maximum)] for i in range(maximum)]
    return {'status': 'unverified_navigation', 'scenes': selected, 'omitted_scenes': len(rows)-len(selected)}


def prepare_inputs(memory, query, cfg, inspected=(), remaining_reads=None):
    context = caption_context(memory, query, cfg)
    intervals = [memory.get(c['window_id'])['interval'] for c in inspected]
    bounded = [w for w in context['windows']
               if w['interval'][1]-w['interval'][0] <= cfg['observation']['max_window_seconds']]
    available = [w for w in bounded
                 if not any((overlap := intersect(w['interval'], old)) and
                             overlap[1]-overlap[0] >= .5*min(w['interval'][1]-w['interval'][0], old[1]-old[0])
                             for old in intervals)]
    context = dict(context, windows=available, selected_windows=len(available),
                   omitted_windows=context['eligible_windows']-len(available),
                   oversized_windows_excluded=len(context['windows'])-len(bounded),
                   inspected_windows_excluded=len(bounded)-len(available))
    limit = min(cfg['query']['max_script_reads'], cfg['query']['global_task_limit'])
    claims = [{'id': c['id'], 'description': c.get('description', ''), 'interval': c['interval']}
              for c in memory.tables['claims'].values() if memory.is_valid(c['id'])]
    from .query_policy import query_observation_gaps
    return {'question': query['question'], 'options': query['options'],
            'script_outline': script_outline(memory, query, cfg),
            'coarse_context': context,
            'verified_claims': claims,
            'inspected_windows': [{'window_id': c['window_id'], 'interval': memory.get(c['window_id'])['interval']}
                                  for c in inspected],
            'observation_gaps': query_observation_gaps(memory, query['id']),
            'remaining_reads': limit if remaining_reads is None else max(0, remaining_reads),
            'max_reads': 1}


def normalize_plan(report, inputs):
    if inputs['max_reads'] <= 0 or inputs.get('remaining_reads', 1) <= 0:
        return dict(report, checks=[])
    windows = {w['id']: w for w in inputs['coarse_context']['windows']}
    checks, intervals = [], []
    for row in report['checks']:
        if row['window_id'] not in windows:
            raise ValueError('Read plan refers to an unseen window')
        interval = windows[row['window_id']]['interval']
        # Neighboring overlapping windows are not two independent inspections.
        if any((overlap := intersect(interval, old)) and
               overlap[1]-overlap[0] >= .5*min(interval[1]-interval[0], old[1]-old[0]) for old in intervals):
            continue
        # Model reasoning is retained for diagnostics but is never an asserted
        # observation goal or acceptance test (e.g. "make a pizza"). Options
        # still reach the observer as unverified distinctions to inspect.
        checks.append(dict(row, proposed_goal=row['goal'],
                           goal='Record visible actions, tools, hands and their order; keep unclear details unknown.'))
        intervals.append(interval)
        if len(checks) == inputs['max_reads']:
            break
    return dict(report, checks=checks)
