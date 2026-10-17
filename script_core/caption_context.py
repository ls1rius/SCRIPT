"""Chronological caption inputs, following VideoTree's retained-description QA."""
import re
from .coverage import intersect


def caption_context(memory, query, cfg):
    eligible = [w for w in memory.tables['windows'].values()
                if intersect(w['interval'], query['scope']) == w['interval'] and w.get('caption')]
    eligible.sort(key=lambda w: (w['interval'][0], w['id']))
    terms = set(re.findall(r'\w+', query['question'].lower()+' '+' '.join(query.get('options', []))))
    # Temporal representatives survive even when lexical relevance is weak.
    representatives = []
    for i in range(min(6, len(eligible))):
        representatives.append(eligible[min(len(eligible)-1, int((i+.5)*len(eligible)/min(6, len(eligible))))])
    ranked = sorted(eligible, key=lambda w: (-len(terms & set(re.findall(r'\w+', w['caption'].lower()))), w['interval'][0]))
    selected = {}
    # Bound whole records; never truncate caption text or claim it is verified.
    byte_limit = cfg['models']['max_input_tokens']
    used = 0
    from .selective import eligible as guided_eligible
    guided = bool(guided_eligible(memory, query, cfg))
    maximum = cfg['query']['script_context_windows'] if guided else len(eligible)
    if guided:
        from collections import Counter
        from math import log
        from .query_policy import option_distinctions
        stop = set('a an the is are was were be been being to of in on at by for and or then with using it its this that as from into she he they c'.split())
        words = lambda text: set(re.findall(r'\w+', text.lower()))-stop
        differences = [words(row['difference_text']) for row in option_distinctions(query.get('options', []))]
        frequencies = Counter(word for row in differences for word in row)
        question_terms = words(query['question'])
        token_sets = {w['id']: words(w['caption']) for w in eligible}
        document_frequency = Counter(word for row in token_sets.values() for word in row)
        def relevance(window):
            return sum((int(word in question_terms)+2*int(word in frequencies)/max(1, frequencies[word]))*
                       log(1+len(eligible)/(1+document_frequency[word]))
                       for word in token_sets[window['id']] if word in question_terms or word in frequencies)
        ranked = sorted(eligible, key=lambda w: (-relevance(w), w['interval'][0], w['id']))
        # Keep beginning/middle/end coverage and spend the remaining slots on
        # relevance. Retain whole records; all original records stay in M0.
        representatives = [eligible[min(len(eligible)-1, int((i+.5)*len(eligible)/min(3, maximum)))]
                           for i in range(min(3, maximum))] if eligible else []
    for w in representatives+ranked:
        if len(selected) >= maximum:
            break
        if w['id'] in selected:
            continue
        record = {k: w[k] for k in ('id', 'interval', 'caption', 'caption_status')}
        size = len(w['caption'].encode())+128
        if used+size > byte_limit:
            continue
        selected[w['id']] = record
        used += size
    windows = sorted(selected.values(), key=lambda w: (w['interval'][0], w['id']))
    return {'status': 'unverified_caption_context', 'selection': 'temporal_and_question_option_relevance',
            'reading_policy': cfg['query'].get('reading_policy', 'coverage_first'),
            'eligible_windows': len(eligible), 'selected_windows': len(windows),
            'omitted_windows': len(eligible)-len(windows), 'windows': windows}
