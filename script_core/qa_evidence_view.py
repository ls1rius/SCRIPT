"""Deterministic presentation of archived evidence for final QA, without models."""
from copy import deepcopy
import json
import math


CATEGORIES = {
    'actions': 'Action', 'attributes': 'Attribute', 'states': 'State',
    'relations': 'Relation', 'changes': 'Change', 'results': 'Result',
    'event_links': 'Event link',
}

NARRATIVE_VERSION = 'F-fact-sentences-v1'


def _sentence(text):
    text = text.strip()
    return text if text.endswith(('.', '!', '?', '。', '！', '？')) else text + '.'


def _fact_sentence(category, fact):
    """Verbalize literal values; schema labels remain in the archive only."""
    specifications = {
        'actions': ({'actor', 'verb'}, {'patient', 'instrument', 'material',
            'body_part', 'surface', 'source_location', 'destination', 'location',
            'manner', 'recipient'}),
        'attributes': ({'entity', 'property', 'value'}, set()),
        'states': ({'entity', 'dimension', 'value'}, set()),
        'relations': ({'left', 'predicate', 'right', 'relation_type'}, set()),
        'changes': ({'entity', 'before', 'after'}, set()),
        'results': ({'entity', 'predicate', 'value'}, set()),
        'event_links': ({'left_event', 'connector', 'right_event', 'link_type'}, set()),
    }
    required, optional = specifications[category]
    if not required <= fact.keys() or fact.keys() - required - optional:
        raise ValueError('Unsupported narrative fact fields; preserve the source clause')
    if category == 'actions':
        words = [fact['actor']]
        if 'manner' in fact:
            words.append(fact['manner'])
        words.append(fact['verb'])
        if 'patient' in fact:
            words.append(fact['patient'])
        # These connect the typed action roles without inventing an actor,
        # object, outcome, purpose, or a relation between separate samples.
        for role, connector in [('instrument', 'using'), ('body_part', 'with'),
                ('source_location', 'from'), ('destination', 'to'), ('recipient', 'to')]:
            if role in fact:
                words.extend([connector, fact[role]])
        sentences = [_sentence(' '.join(words))]
        # Neutral role sentences avoid guessing the original preposition
        # (e.g. in/on/under for location, or on/against for a surface).
        for role, template in [('material', 'The material used in this action is {}'),
                ('surface', 'The working surface for this action is {}'),
                ('location', 'The location of this action is {}')]:
            if role in fact:
                sentences.append(_sentence(template.format(fact[role])))
        return ' '.join(sentences)
    if category == 'attributes':
        return _sentence('The ' + fact['property'] + ' of ' + fact['entity'] + ' is ' + fact['value'])
    if category == 'states':
        return _sentence('The observed state of ' + fact['entity'] + ' is ' + fact['value'])
    if category == 'relations':
        predicate = fact['predicate']
        finite = {'has', 'have', 'holds', 'owns', 'possesses', 'belongs to',
            'contains', 'includes', 'consists of'}
        copula = '' if predicate.casefold() in finite or predicate.casefold().startswith(
            ('is ', 'are ', 'was ', 'were ')) else 'is '
        return _sentence(fact['left'] + ' ' + copula + predicate + ' ' + fact['right'])
    if category == 'changes':
        return _sentence(fact['entity'] + ' changes from ' + fact['before'] + ' to ' + fact['after'])
    if category == 'results':
        return _sentence(fact['entity'] + ' ' + fact['predicate'] + ' ' + fact['value'])
    # Retain the source connector rather than strengthening temporal order
    # into a causal or intentional interpretation.
    return _sentence(fact['left_event'] + ' ' + fact['connector'] + ' ' + fact['right_event'])


def narrative_description(payload):
    """F's sentence view; original clauses and all sample occurrences survive.

    This does not change extraction or cached facts. The legacy renderer is
    retained for other variants and frozen experiment presentation.
    """
    _, audit = compact_description(payload)
    rendered = deepcopy(payload)
    for row in rendered['observations']:
        evidence = row['evidence']
        if 'original_text' in evidence or 'categorized_original' in evidence:
            continue
        sentences = [_fact_sentence(category, fact)
            for category in CATEGORIES for fact in evidence.get(category, [])]
        row['evidence'] = dict(original_text=' '.join(sentences))
    text, sentence_audit = compact_description(rendered)
    for before, after in zip(audit['rows'], sentence_audit['rows']):
        before['prompt_line'] = after['prompt_line']
    audit.update(render_version=NARRATIVE_VERSION, fact_presentation='sentences')
    return text, audit


def compact_description(payload, *, merge_repeated=False):
    """Return readable evidence and a source map kept outside the model prompt.

    Retain each sample separately. Where the validated archive retains a literal
    clause, use that clause instead of repeating it alongside derived facts.
    No relevance filtering, identity merging, inference or token truncation occurs.
    """
    if merge_repeated:
        text, audit = compact_description(payload)
        return _merge_repeated_observations(payload, text, audit)
    if set(payload) != {'observations'} or not isinstance(payload['observations'], list):
        raise ValueError('Expected the archived observation list')
    lines = []
    source_map = []
    seen = set()
    for row in payload['observations']:
        if set(row) != {'caption_id', 'frame_index', 'sample_interval', 'evidence'}:
            raise ValueError('Unknown observation fields; explicit projection required')
        cid = row['caption_id']
        if not isinstance(cid, str) or cid in seen:
            raise ValueError('Duplicate or invalid caption ID')
        seen.add(cid)
        interval = row['sample_interval']
        if (not isinstance(interval, list) or len(interval) != 2
                or any(isinstance(t, bool) or not isinstance(t, (int, float))
                       or not math.isfinite(t) for t in interval)
                or interval[0] >= interval[1]):
            raise ValueError('Invalid sample interval')
        evidence = row['evidence']
        if not isinstance(evidence, dict) or set(evidence) - (set(CATEGORIES) | {
                'original_text', 'categorized_original'}):
            raise ValueError('Unknown evidence fields; explicit projection required')
        facts = []
        for category, label in CATEGORIES.items():
            records = evidence.get(category, [])
            if not isinstance(records, list):
                raise ValueError('Expected a fact list')
            for record in records:
                if (not isinstance(record, dict) or not record
                        or any(not isinstance(k, str) or not isinstance(v, str) or not v
                               for k, v in record.items())):
                    raise ValueError('Expected nonempty literal fact fields')
                facts.append(label + ': ' + '; '.join(
                    k + '=' + json.dumps(v, ensure_ascii=False) for k, v in record.items()))
        residual = evidence.get('categorized_original')
        if residual is not None and (not isinstance(residual, dict)
                or set(residual) != {'original_text', 'information_types'}):
            raise ValueError('Unknown residual fields')
        original = evidence.get('original_text')
        if residual is not None:
            if original is not None and original != residual['original_text']:
                raise ValueError('Conflicting original clauses')
            original = residual['original_text']
        if original is not None:
            if not isinstance(original, str) or not original.strip():
                raise ValueError('Empty original clause')
            body = original
            mode = 'original'
        elif facts:
            body = ' | '.join(facts)
            mode = 'facts'
        else:
            raise ValueError('Observation has neither facts nor original text')
        start_line = 1 + sum(line.count('\n') + 1 for line in lines)
        bounds = ','.join(json.dumps(t) for t in interval)
        lines.append('Frame [' + bounds + '): ' + body)
        source_map.append(dict(caption_id=cid, frame_index=row['frame_index'],
            sample_interval=deepcopy(interval), prompt_line=start_line,
            representation=mode, archived_fact_count=len(facts)))
    return '\n'.join(lines), dict(source_rows=len(source_map), rendered_rows=len(source_map),
        rows=source_map, relevance_filtering=False, source_rows_merged=False,
        storage_ids_in_prompt=False, sample_intervals_in_prompt=True,
        literal_original_preferred_to_duplicate_facts=True)


def _merge_repeated_observations(payload, text, audit):
    """Factor consecutive identical assertions, retaining every sampled interval.

    This groups repeated records rather than proving continuous physical state,
    entity identity, or that repeated action samples constitute one event.
    Any different intervening record starts a new group, even if an earlier
    assertion later returns. No semantic equivalence is inferred from wording.
    """
    source_lines = text.split('\n')
    groups = []
    for index, (row, provenance) in enumerate(zip(payload['observations'], audit['rows'])):
        start = provenance['prompt_line'] - 1
        end = audit['rows'][index+1]['prompt_line'] - 1 if index+1 < len(audit['rows']) else len(source_lines)
        rendered = '\n'.join(source_lines[start:end])
        prefix = 'Frame [' + ','.join(json.dumps(t) for t in row['sample_interval']) + '): '
        if not rendered.startswith(prefix):
            raise ValueError('Rendered observation and source mapping differ')
        key = json.dumps(row['evidence'], sort_keys=True, ensure_ascii=False)
        if not groups or groups[-1]['key'] != key:
            groups.append(dict(key=key, body=rendered[len(prefix):], source_indices=[]))
        groups[-1]['source_indices'].append(index)
    lines = []
    group_map = []
    prompt_line = 1
    for group_index, group in enumerate(groups):
        indices = group['source_indices']
        rows = [payload['observations'][i] for i in indices]
        intervals = [deepcopy(row['sample_interval']) for row in rows]
        bounds = ['[' + ','.join(json.dumps(t) for t in interval) + ')' for interval in intervals]
        line = ('Frames ' if len(rows) > 1 else 'Frame ') + ', '.join(bounds) + ': ' + group['body']
        lines.append(line)
        for index in indices:
            audit['rows'][index].update(prompt_line=prompt_line, rendered_group_index=group_index)
        group_map.append(dict(caption_ids=[row['caption_id'] for row in rows],
            frame_indices=[row['frame_index'] for row in rows], sample_intervals=intervals,
            prompt_line=prompt_line))
        prompt_line += line.count('\n') + 1
    audit.update(rendered_rows=len(groups), source_rows_merged=len(groups) < audit['source_rows'],
        grouping='consecutive_identical_evidence', groups=group_map,
        identities_merged=False, continuous_duration_inferred=False, event_counts_inferred=False)
    return '\n'.join(lines), audit
