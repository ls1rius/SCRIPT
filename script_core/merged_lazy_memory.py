"""I-v2: reversible grouping of H's visible assertions and discrete samples.

No model, question, identity matching, persistence inference or source filtering
is involved. H's literal fallback always takes precedence over archived facts.
"""
from copy import deepcopy
import json

try:
    from .qa_evidence_view import compact_description as parent_description
except ImportError:  # Frozen experiment's control directory.
    from qa_evidence_view import compact_description as parent_description


VERSION = 'merged-lazy-memory-I-v2-H'
RENDER_VERSION = VERSION + '-adjacent-v1'


def validate_parent_payload(payload):
    """Do not relabel legacy F fixed states as H-derived I evidence."""
    for row in payload['observations']:
        for state in row['evidence'].get('states', []):
            if set(state) != {'entity', 'description'}:
                raise ValueError('I inherits H open states: entity/description required')
        for action in row['evidence'].get('actions', []):
            if 'actor' in action and action['actor'] is None:
                raise ValueError('I inherits H storage: omit unknown actors')


def visible_bodies(payload):
    validate_parent_payload(payload)
    text, audit = parent_description(payload)
    lines = text.split('\n')
    bodies = []
    for index, (row, provenance) in enumerate(zip(payload['observations'], audit['rows'])):
        start = provenance['prompt_line'] - 1
        end = audit['rows'][index+1]['prompt_line'] - 1 if index+1 < len(audit['rows']) else len(lines)
        rendered = '\n'.join(lines[start:end])
        prefix = 'Frame [' + ','.join(json.dumps(t) for t in row['sample_interval']) + '): '
        if not rendered.startswith(prefix):
            raise ValueError('H source mapping differs from its visible assertion')
        bodies.append(rendered[len(prefix):])
    return bodies, text, audit


def materialize(payload):
    bodies, _, _ = visible_bodies(payload)
    if not bodies:
        raise ValueError('I requires nonempty H observations')
    records = []
    lookup = {}
    observations = []
    groups = []
    group_lookup = {}
    for index, (row, body) in enumerate(zip(payload['observations'], bodies)):
        # Keep field insertion order, since H renders that order verbatim.
        key = json.dumps(row['evidence'], ensure_ascii=False)
        if key not in lookup:
            lookup[key] = len(records)
            records.append(dict(id=f'R{len(records)}', evidence=deepcopy(row['evidence'])))
        observations.append(dict(caption_id=row['caption_id'], frame_index=row['frame_index'],
            sample_interval=deepcopy(row['sample_interval']), record_id=records[lookup[key]]['id']))
        if body not in group_lookup:
            group_lookup[body] = len(groups)
            groups.append(dict(id=f'G{len(groups)}', body=body, observation_indices=[]))
        groups[group_lookup[body]]['observation_indices'].append(index)
    memory = dict(schema=VERSION, parent_memory_variant='H', records=records, observations=observations, groups=groups,
        policy=dict(grouping='identical_visible_assertion_across_selected_samples',
            group_order='first_observation', source_order_restorable=True,
            identities_merged=False, continuous_duration_inferred=False, event_counts_inferred=False))
    validate(memory, payload)
    return memory


def restore(memory):
    records = {r['id']: r['evidence'] for r in memory['records']}
    if len(records) != len(memory['records']):
        raise ValueError('Duplicate I record IDs')
    return dict(observations=[dict(caption_id=r['caption_id'], frame_index=r['frame_index'],
        sample_interval=deepcopy(r['sample_interval']), evidence=deepcopy(records[r['record_id']]))
        for r in memory['observations']])


def validate(memory, payload=None):
    if memory.get('schema') != VERSION:
        raise ValueError('Unknown I memory version')
    if memory.get('parent_memory_variant') != 'H':
        raise ValueError('I-v2 requires H parent memory')
    restored = restore(memory)
    bodies, text, source_audit = visible_bodies(restored)
    if payload is not None and (restored != payload or visible_bodies(payload)[1] != text):
        raise ValueError('I changed the original H evidence')
    if not bodies:
        raise ValueError('I requires nonempty observations')
    covered = []
    first = []
    seen_bodies = set()
    for index, group in enumerate(memory['groups']):
        indices = group['observation_indices']
        if group['id'] != f'G{index}' or not indices or indices != sorted(set(indices)):
            raise ValueError('Invalid I group support')
        if any(type(i) is not int or i < 0 or i >= len(bodies) for i in indices):
            raise ValueError('Invalid I observation index')
        if group['body'] in seen_bodies or any(bodies[i] != group['body'] for i in indices):
            raise ValueError('I group contains a different or duplicated assertion')
        seen_bodies.add(group['body'])
        first.append(indices[0])
        covered.extend(indices)
    if first != sorted(first) or sorted(covered) != list(range(len(bodies))):
        raise ValueError('I lost, reordered groups, or duplicated source observations')
    expected = dict(grouping='identical_visible_assertion_across_selected_samples',
        group_order='first_observation', source_order_restorable=True,
        identities_merged=False, continuous_duration_inferred=False, event_counts_inferred=False)
    if memory.get('policy') != expected:
        raise ValueError('I changed its discrete-observation policy')
    return text, source_audit


def adjacent_intervals(intervals):
    """Compact touching source intervals without bridging any unsampled gap."""
    merged = []
    for start, end in sorted(intervals):
        if merged and merged[-1][1] == start:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def render(memory):
    original, source_audit = validate(memory)
    observations = memory['observations']
    lines = []
    rows = []
    prompt_line = 1
    for group in memory['groups']:
        support = [observations[i] for i in group['observation_indices']]
        intervals = [deepcopy(r['sample_interval']) for r in support]
        displayed = adjacent_intervals(intervals)
        bounds = ', '.join('[' + ','.join(json.dumps(t) for t in interval) + ')' for interval in displayed)
        line = ('Frames ' if len(support) > 1 else 'Frame ') + bounds + ': ' + group['body']
        lines.append(line)
        rows.append(dict(group_id=group['id'], observation_indices=deepcopy(group['observation_indices']),
            caption_ids=[r['caption_id'] for r in support], frame_indices=[r['frame_index'] for r in support],
            sample_intervals=intervals, display_intervals=displayed, prompt_line=prompt_line))
        prompt_line += line.count('\n') + 1
    text = '\n'.join(lines)
    return text, dict(version=RENDER_VERSION, parent_memory_variant='H', source_rows=len(observations), covered_source_rows=len(observations),
        rendered_rows=len(lines), merged_groups=sum(len(g['observation_indices']) > 1 for g in memory['groups']),
        duplicate_assertion_rows_removed=len(observations)-len(lines), source_rows_merged=len(lines)<len(observations),
        rows=rows, source_rows_metadata=source_audit['rows'], source_order_restorable=True,
        restored_payload_equal=True, grouping=memory['policy']['grouping'], relevance_filtering=False,
        identities_merged=False, continuous_duration_inferred=False, event_counts_inferred=False,
        interval_presentation='coalesce_exactly_adjacent_intervals_within_identical_body',
        original_description_characters=len(original), merged_description_characters=len(text),
        literal_original_preferred_to_duplicate_facts=True)


def compact_description(payload):
    """Opt-in renderer usable directly with H's ScriptDB.view callback."""
    return render(materialize(payload))
