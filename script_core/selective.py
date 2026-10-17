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


def prepare_inputs(memory, query, cfg):
    context = caption_context(memory, query, cfg)
    available = [w for w in context['windows'] if w['interval'][1]-w['interval'][0] <= cfg['observation']['max_window_seconds']]
    context = dict(context, windows=available, selected_windows=len(available),
                   omitted_windows=context['eligible_windows']-len(available),
                   oversized_windows_excluded=len(context['windows'])-len(available))
    return {'question': query['question'], 'options': query['options'],
            'script_outline': script_outline(memory, query, cfg),
            'coarse_context': context,
            'max_reads': min(cfg['query']['max_script_reads'], cfg['query']['global_task_limit'])}


def normalize_plan(report, inputs):
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
        checks.append(dict(row))
        intervals.append(interval)
        if len(checks) == inputs['max_reads']:
            break
    return dict(report, checks=checks)
