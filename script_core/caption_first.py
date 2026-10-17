"""Caption-based hypotheses and bounded visual checks, never proof records."""
from .coverage import intersect


def timeline(memory, query):
    """Read original timed descriptions once; window overlap must not repeat them."""
    if memory.manifest.get('caption_uri'):
        from .video import load_captions
        spans = load_captions(memory.manifest)['data']
    else:
        spans = [{'start': w['interval'][0], 'end': w['interval'][1], 'text': w['caption']}
                 for w in memory.tables['windows'].values() if w.get('caption')]
    records = {}
    for span in spans:
        interval = intersect([span['start'], span['end']], query['scope'])
        if interval == [span['start'], span['end']]:
            # A generated description crossing a restricted query scope can
            # reveal out-of-scope actions; do not relabel it as a shorter span.
            # Preserve distinct seconds even when their descriptions are equal.
            records[(tuple(interval), span['text'])] = interval
    return [{'id': f'C{i+1}', 'interval': interval, 'text': text}
            for i, ((_, text), interval) in enumerate(sorted(records.items()))]


def prepare_review(memory, query, cfg, captions, inspected, remaining_reads):
    from .selective import prepare_inputs
    available = prepare_inputs(memory, query, cfg, inspected, remaining_reads)
    return {'question': query['question'], 'answer_mode': 'choice', 'options': query['options'],
            'option_ids': [str(i) for i in range(len(query['options']))],
            'caption_timeline': captions, 'caption_status': 'unverified_descriptions',
            'candidate_windows': [{'id': w['id'], 'interval': w['interval']}
                                  for w in available['coarse_context']['windows']],
            'verified_claims': available['verified_claims'] if inspected else [],
            'inspected_windows': available['inspected_windows'],
            'observation_gaps': available['observation_gaps'],
            'remaining_reads': max(0, remaining_reads), 'max_reads': min(1, max(0, remaining_reads))}


def normalize_review(report, inputs):
    """Validate caption references separately from visual evidence references."""
    captions = {c['id']: c for c in inputs['caption_timeline']}
    windows = {w['id']: w for w in inputs['candidate_windows']}
    if not set(report['caption_ids']) <= captions.keys():
        raise ValueError('Caption answer refers to an unseen description')
    checks = []
    for row in report['checks'][:inputs['max_reads']]:
        if row['window_id'] not in windows or not set(row['caption_ids']) <= captions.keys():
            raise ValueError('Caption check refers to an unseen window or description')
        if row['caption_ids'] and not any(intersect(captions[c]['interval'], windows[row['window_id']]['interval'])
                                          for c in row['caption_ids']):
            raise ValueError('Caption check has no temporal anchor in its selected window')
        # This is explicitly a question, not an asserted observation or purpose.
        checks.append({'window_id': row['window_id'], 'goal':
            'Unverified inspection question: '+row['question'], 'inspection_question': row['question'],
            'caption_ids': row['caption_ids'], 'reason': row['reason']})
    return {'reason': 'Inspect only unresolved caption distinctions.', 'checks': checks}


def final_inputs(inputs, captions):
    """Give final QA the timeline and concise facts, without its previous guess."""
    result = dict(inputs)
    result.pop('coarse_context', None)
    result.pop('script_outline', None)
    result['caption_timeline'] = captions
    result['caption_status'] = 'unverified_descriptions'
    return result
