"""Separate semantic information types from temporal storage and evidence source.

No sentence classifier or new extraction is run here. Missing upstream type tags
remain unclassified, and missing semantic roles are never guessed from prose.
"""
from collections import Counter, defaultdict
from copy import deepcopy

INFORMATION_TYPES = ('action', 'attribute', 'state', 'relation', 'transition', 'cause',
                     'identity', 'lineage', 'unclassified')
COLLECTIONS = dict(action='actions', attribute='attributes', state='states',
    relation='relations', transition='transitions', cause='causes', identity='identities',
    lineage='lineage', unclassified='unclassified')


def validate_types(included):
    if (not isinstance(included, (list, tuple)) or any(not isinstance(t, str) for t in included)
            or len(set(included)) != len(included) or not set(included) <= set(INFORMATION_TYPES)):
        raise ValueError('Information types must be a unique list drawn from '+', '.join(INFORMATION_TYPES))
    return tuple(t for t in INFORMATION_TYPES if t in included)


def attach_observation_types(view, memory):
    """Recover original extraction tags before temporal assembly erased the kind."""
    from .temporal_entity import canonical
    result = deepcopy(view)
    chosen = {e['id'] for e in view['entities']}
    net = memory['temporal_network']
    result['source_observations'] = []
    for original in memory['observations'].values():
        eid = canonical(net, original['entity_id'])
        if eid in chosen:
            row = deepcopy(original)
            row.update(entity_id=eid, original_entity_id=original['entity_id'])
            result['source_observations'].append(row)
    return result


def typed_context(captions, selected, view, visual_observations=(), included=INFORMATION_TYPES):
    """Return typed facts plus an audit suitable for category ablations.

    Entity IDs and temporal slots are structural indexes. An assembler's `states`
    record is not automatically a semantic state: only upstream kind=state earns
    that type. Shared original sentences across types are reported explicitly.
    """
    included = validate_types(included)
    if view.get('source_type') != 'caption':
        raise ValueError('Typed memory requires caption-derived sources')
    if any(type(i) is not int or not 0 <= i < len(captions) for i in selected):
        raise ValueError('Invalid selected caption position')
    sources = {c['id']: c for c in captions}
    readable = {captions[i]['id'] for i in selected}
    if len(sources) != len(captions):
        raise ValueError('Duplicate source caption IDs')
    groups = {kind: [] for kind in INFORMATION_TYPES}
    covered = set()
    kinds = defaultdict(set)

    def add(kind, record, basis):
        record = deepcopy(record)
        refs = record.get('source_caption_ids', [])
        if not set(refs) <= readable:
            raise ValueError('Typed information cites unread or unknown captions')
        record.update(information_type=kind, classification_basis=basis,
            source_type='caption')
        record.setdefault('status', 'caption_asserted')
        groups[kind].append(record)

    for entity in view.get('entities', []):
        for index, attr in enumerate(entity.get('attributes', [])):
            add('attribute', dict(attr, id=f"attribute:{entity['id']}:{index}",
                entity_id=entity['id']), 'upstream entity attribute field')
    for index, obs in enumerate(view.get('source_observations', [])):
        cid = obs['caption_id']
        if cid not in readable:
            raise ValueError('Typed observation cites unread or unknown captions')
        kind = obs.get('kind') if obs.get('kind') in ('action', 'state') else 'unclassified'
        kinds[(obs['entity_id'], cid)].add(kind)
        covered.add((obs['entity_id'], cid))
        row = dict(id=f"observation:{obs.get('id', index)}", entity_id=obs['entity_id'],
            scene_id=obs.get('scene_id'), description=sources[cid]['text'],
            interval=deepcopy(sources[cid]['interval']), source_caption_ids=[cid],
            upstream_kind=obs.get('kind', 'unknown'), status=obs.get('status', 'caption_asserted'),
            continuity=obs.get('continuity', 'uncertain'),
            continuity_basis=obs.get('continuity_basis', 'unknown'))
        if kind == 'action':
            # entity_id identifies a mentioned participant, not necessarily actor.
            row['roles'] = dict(actor=None, action=None, object=None, tool=None,
                status='not_separately_extracted; preserve the cited description')
        add(kind, row, 'upstream observation.kind; no prose reclassification')

    slots = []
    for history in view.get('states', []):
        refs = history.get('source_caption_ids', [])
        if not set(refs) <= readable:
            raise ValueError('Temporal slot cites unread or unknown captions')
        slots.append({k: deepcopy(history[k]) for k in ('id', 'entity_id', 'scene_id',
            'first_observed_at', 'last_observed_at', 'valid_from_bounds', 'valid_to_bounds',
            'source_caption_ids', 'boundary', 'status') if k in history})
        missing = [cid for cid in refs if (history['entity_id'], cid) not in covered]
        if missing or not refs:
            add('unclassified', dict(id='history:'+history['id'], entity_id=history['entity_id'],
                description=history.get('description', ''), source_caption_ids=missing,
                reason='No original semantic observation kind available'),
                'temporal storage alone does not establish semantic type')

    for kind, collection in [('relation', 'relations'), ('identity', 'identities'), ('lineage', 'lineage')]:
        for index, fact in enumerate(view.get(collection, [])):
            row = deepcopy(fact)
            row.setdefault('id', f'{kind}:{index}')
            if 'source_caption_ids' not in row:
                row['source_caption_ids'] = list(dict.fromkeys(row.get('caption_ids', []) +
                    row.get('left_caption_ids', []) + row.get('right_caption_ids', [])))
            add(kind, row, 'upstream '+collection+' record')

    histories = {s['id']: s for s in view.get('states', [])}
    for index, transition in enumerate(view.get('transitions', [])):
        row = deepcopy(transition); row.setdefault('id', f'transition:{index}')
        cause = row.pop('cause', {})
        refs = []
        row['cause_id'] = None
        if cause.get('basis', 'unknown') != 'unknown' and cause.get('source_caption_ids'):
            cause_id = 'cause:'+row['id']
            add('cause', dict(cause, id=cause_id, transition_id=row['id'],
                entity_id=row.get('entity_id'),before=row.get('before'),after=row.get('after'),
                status='text_inferred'), 'upstream structured cause; not proof of causality')
            if 'cause' in included:
                row['cause_id'] = cause_id
        endpoint_kinds = []
        for side in ('before', 'after'):
            history = histories.get(row.get(side), {})
            refs.extend(history.get('source_caption_ids', []))
            endpoint_kinds.append(set().union(*(kinds[(history.get('entity_id'), cid)]
                for cid in history.get('source_caption_ids', []))))
        row['source_caption_ids'] = list(dict.fromkeys(refs))
        row['transition_kind'] = ('state_change' if all(k == {'state'} for k in endpoint_kinds)
            else 'action_sequence' if any('action' in k for k in endpoint_kinds)
            else 'unclassified_change')
        row['status'] = transition.get('status', 'text_inferred')
        add('transition', row, 'upstream temporal judgment; endpoint tags qualify its meaning')

    visual_groups = {kind: [] for kind in INFORMATION_TYPES}
    for index, observation in enumerate(visual_observations):
        kind = observation.get('kind') if observation.get('kind') in ('action', 'state') else 'unclassified'
        row = deepcopy(observation)
        row.update(id=f'visual:{index}', information_type=kind,
            classification_basis='upstream visual observation.kind', source_type='sampled_visual')
        row.setdefault('status', 'accepted_upstream_visual_model_observation')
        visual_groups[kind].append(row)

    source_types = defaultdict(set)
    for kind, facts in groups.items():
        for fact in facts:
            for cid in fact.get('source_caption_ids', []):
                source_types[cid].add(kind)
    excluded = set(INFORMATION_TYPES) - set(included)
    overlap = {cid: sorted(types) for cid, types in source_types.items()
        if types.intersection(included) and types.intersection(excluded)}
    typed = dict(layout='typed-memory-v1', source_type='caption',
        entities=[{k: deepcopy(e[k]) for k in ('id', 'label', 'scene_ids') if k in e}
            for e in view.get('entities', [])],
        temporal_slots=slots if set(included)&{'transition','cause'} else [],
        evidence_policy=view.get('evidence_policy', ''),
        context_selection=deepcopy(view.get('context_selection', {})))
    for kind, collection in COLLECTIONS.items():
        typed[collection] = groups[kind] if kind in included else []
    audit = dict(schema='typed-memory-v1', included_types=list(included), excluded_types=sorted(excluded),
        available_caption_counts={k: len(groups[k]) for k in INFORMATION_TYPES},
        retained_caption_counts={k: len(typed[COLLECTIONS[k]]) for k in INFORMATION_TYPES},
        available_visual_counts={k: len(visual_groups[k]) for k in INFORMATION_TYPES},
        retained_visual_counts={k: len(visual_groups[k]) if k in included else 0 for k in INFORMATION_TYPES},
        attribute_fields=dict(Counter(f.get('field', 'unknown') for f in groups['attribute'])),
        source_caption_types={cid: sorted(types) for cid, types in sorted(source_types.items())},
        retained_sources_overlapping_excluded_types=overlap,
        category_policy='Types reflect upstream tags, not independent semantic verification. '
            'Counts are records, not importance scores. Shared quotations can reveal excluded information.')
    typed_visual = {k: v for k, v in visual_groups.items() if k in included}
    return typed, typed_visual, audit
