"""D: store C assertions once and attach sampled support to temporal segments."""
from copy import deepcopy
import json
import math

try:
    from .qa_evidence_view import CATEGORIES, compact_description
except ImportError:
    from qa_evidence_view import CATEGORIES, compact_description


SCHEMA = 'state-segment-memory-v2'


def persistent_anchor(evidence):
    """Only explicit condition/property records support state persistence."""
    if not evidence or set(evidence) - {'states', 'attributes'}:
        return None
    anchors = []
    for category, facts in evidence.items():
        for fact in facts:
            field = 'dimension' if category == 'states' else 'property'
            if set(fact) != {'entity', field, 'value'}:
                return None
            anchors.append((category, fact['entity'], fact[field]))
    return json.dumps(sorted(anchors), ensure_ascii=False) if anchors and len(set(anchors))==len(anchors) else None


def persistent_values(evidence):
    return {(category,fact['entity'],fact['dimension' if category=='states' else 'property']):fact['value']
            for category,facts in evidence.items() for fact in facts}


def evidence_for(record, facts):
    return {category:([deepcopy(facts[fid]['fields']) for fid in value] if category in CATEGORIES else deepcopy(value))
            for category,value in record['evidence_refs'].items()}


def state_family(fact):
    category= fact['category'];fields=fact['fields']
    if category=='states':
        return category,fields['entity'],fields['dimension']
    if category=='attributes':
        return category,fields['entity'],fields['property']
    if category=='relations':
        return category,fields['left'],fields['relation_type'],fields['predicate']
    return None


def fact_segments(observations,records,facts,max_gap):
    """Track condition, property and relation facts independently of actions."""
    segments=[];active={}
    for index,observation in enumerate(observations):
        record=records[observation['record_id']]
        current={}
        for category,refs in record['evidence_refs'].items():
            if category not in ('states','attributes','relations'):
                continue
            for fid in refs:
                current.setdefault(state_family(facts[fid]),set()).add(fid)
        # Explicitly different values split an old track, including a return to
        # its old value after an intervening changed observation.
        for family,values in current.items():
            for key in list(active):
                if key[0]==family and (key[1] not in values or len(values)>1):
                    del active[key]
        for family,values in current.items():
            for fid in sorted(values):
                key=(family,fid);candidate=active.get(key)
                if candidate is not None:
                    segment=segments[candidate]
                    last=observations[segment['observation_indices'][-1]]
                    gap=observation['sample_interval'][0]-last['sample_interval'][1]
                    if 0<=gap<=max_gap and len(values)==1:
                        segment['observation_indices'].append(index)
                        segment['interval'][1]=observation['sample_interval'][1]
                        segment['continuity']='inferred_from_repeated_caption_assertions'
                        continue
                segments.append(dict(id=f'P{len(segments)}',fact_id=fid,
                    observation_indices=[index],interval=deepcopy(observation['sample_interval']),
                    continuity='single_sample',identity_basis='same_literal_entity_in_local_observations'))
                if len(values)==1:
                    active[key]=len(segments)-1
        # An intervening action or unexplained clause about the same entity
        # cannot by itself establish continued physical state/spatial relation.
        evidence=evidence_for(record,facts)
        touched={value for action in evidence.get('actions',[]) for key,value in action.items()
                 if key in ('actor','patient')}
        original=evidence.get('original_text')
        if 'categorized_original' in evidence:
            original=evidence['categorized_original']['original_text']
        for key in list(active):
            family=key[0]
            if family in current or family[0]=='attributes':
                continue
            if family[1] in touched or (original and family[1].casefold() in original.casefold()):
                del active[key]
    return segments


def materialize(payload, max_gap_seconds=2):
    """Preserve C exactly; infer bounded persistence only for repeated states.

    The identity assumption is a shared literal entity within nearby selected
    observations, not verified global identity. Non-state observations interrupt
    active state tracks. Actions retain all discrete supports and no event count.
    """
    compact_description(payload)
    if (isinstance(max_gap_seconds, bool) or not isinstance(max_gap_seconds, (int,float))
            or not math.isfinite(max_gap_seconds) or max_gap_seconds < 0):
        raise ValueError('Invalid maximum persistence gap')
    registry = []
    fact_registry=[]
    fact_lookup={}
    registry_evidence=[]
    lookup = {}
    observations = []
    segments = []
    active = {}
    previous = None
    for row in payload['observations']:
        if previous is not None and row['sample_interval'][0] < previous:
            raise ValueError('Observations must follow sample-time order')
        previous = row['sample_interval'][0]
        key = json.dumps(row['evidence'], sort_keys=True, ensure_ascii=False)
        if key not in lookup:
            evidence_refs={}
            for category,value in row['evidence'].items():
                if category not in CATEGORIES:
                    evidence_refs[category]=deepcopy(value)
                    continue
                refs=[]
                for fact in value:
                    fact_key=json.dumps([category,fact],sort_keys=True,ensure_ascii=False)
                    if fact_key not in fact_lookup:
                        fid=f'F{len(fact_registry)}'
                        fact_lookup[fact_key]=fid
                        fact_registry.append(dict(id=fid,category=category,fields=deepcopy(fact)))
                    refs.append(fact_lookup[fact_key])
                evidence_refs[category]=refs
            lookup[key] = len(registry)
            registry.append(dict(id=f'R{len(registry)}', evidence_refs=evidence_refs))
            registry_evidence.append(row['evidence'])
        record_index = lookup[key]
        record = registry[record_index]
        observation_index = len(observations)
        observations.append(dict(caption_id=row['caption_id'], frame_index=row['frame_index'],
            sample_interval=deepcopy(row['sample_interval']), record_id=record['id']))
        anchor = persistent_anchor(row['evidence'])
        if anchor is not None:
            current = persistent_values(row['evidence'])
            for old_anchor, segment_index in list(active.items()):
                old_record = segments[segment_index]['record_id']
                previous_values = persistent_values(registry_evidence[int(old_record[1:])])
                if any(previous_values[k] != current[k] for k in previous_values.keys() & current.keys()):
                    del active[old_anchor]
        candidate = active.get(anchor) if anchor is not None else (len(segments)-1 if segments else None)
        if anchor is None:
            active.clear()
        merged = False
        if candidate is not None:
            segment = segments[candidate]
            last = observations[segment['observation_indices'][-1]]
            gap = row['sample_interval'][0] - last['sample_interval'][1]
            if (segment['record_id'] == record['id'] and 0 <= gap <= max_gap_seconds
                    and ((anchor is not None and segment['kind'] == 'persistent')
                         or (anchor is None and segment['kind'] == 'sampled_assertion'
                             and segment['observation_indices'][-1] == observation_index-1))):
                segment['observation_indices'].append(observation_index)
                segment['interval'][1] = row['sample_interval'][1]
                if anchor is not None:
                    segment['continuity'] = 'inferred_from_repeated_caption_assertions'
                merged = True
        if not merged:
            segments.append(dict(id=f'T{len(segments)}', record_id=record['id'],
                kind='persistent' if anchor is not None else 'sampled_assertion',
                interval=deepcopy(row['sample_interval']), observation_indices=[observation_index],
                continuity='single_sample' if anchor is not None else 'discrete_samples',
                identity_basis='same_literal_entity_in_local_observations' if anchor is not None else None))
            candidate = len(segments)-1
        if anchor is not None:
            active[anchor] = candidate
    memory = dict(schema=SCHEMA, max_gap_seconds=max_gap_seconds, records=registry,facts=fact_registry,
        observations=observations, segments=segments,
        policy=dict(source_content_preserved=True, state_continuity_is_inferred=True,
                    action_duration_inferred=False, event_counts_inferred=False))
    memory['state_segments']=fact_segments(observations,{r['id']:r for r in registry},
                                          {f['id']:f for f in fact_registry},max_gap_seconds)
    validate(memory, payload)
    return memory


def restore(memory):
    facts={f['id']:f for f in memory['facts']}
    records = {r['id']:evidence_for(r,facts) for r in memory['records']}
    return dict(observations=[dict(caption_id=r['caption_id'],frame_index=r['frame_index'],
        sample_interval=deepcopy(r['sample_interval']), evidence=deepcopy(records[r['record_id']]))
        for r in memory['observations']])


def validate(memory, payload=None):
    if memory['schema'] != SCHEMA:
        raise ValueError('Unknown state segment schema')
    restored = restore(memory)
    compact_description(restored)
    if payload is not None and restored != payload:
        raise ValueError('State segment storage changed original C evidence')
    records = {r['id']:r for r in memory['records']}
    facts={f['id']:f for f in memory['facts']}
    if len(records) != len(memory['records']):
        raise ValueError('Duplicate record IDs')
    covered = []
    for segment in memory['segments']:
        indices = segment['observation_indices']
        if not indices or indices != sorted(set(indices)):
            raise ValueError('Invalid segment support')
        support = [memory['observations'][i] for i in indices]
        if any(r['record_id'] != segment['record_id'] for r in support):
            raise ValueError('Segment includes a different assertion')
        if segment['interval'] != [support[0]['sample_interval'][0],support[-1]['sample_interval'][1]]:
            raise ValueError('Segment bounds differ from sampled support')
        if segment['kind'] == 'persistent':
            if persistent_anchor(evidence_for(records[segment['record_id']],facts)) is None:
                raise ValueError('Action or original text cannot become a persistent state')
        elif segment['kind'] != 'sampled_assertion':
            raise ValueError('Unknown segment kind')
        for left,right in zip(support,support[1:]):
            if not 0 <= right['sample_interval'][0]-left['sample_interval'][1] <= memory['max_gap_seconds']:
                raise ValueError('Segment bridges an unsupported gap')
        covered.extend(indices)
    if sorted(covered) != list(range(len(memory['observations']))):
        raise ValueError('Segment support lost or duplicated an observation')
    expected={(i,fid) for i,row in enumerate(memory['observations'])
        for category,refs in records[row['record_id']]['evidence_refs'].items()
        if category in ('states','attributes','relations') for fid in refs}
    supported=[]
    for segment in memory['state_segments']:
        fid=segment['fact_id'];support=segment['observation_indices']
        if not support or support!=sorted(set(support)) or state_family(facts[fid]) is None:
            raise ValueError('Invalid state fact support')
        intervals=[memory['observations'][i]['sample_interval'] for i in support]
        if segment['interval']!=[intervals[0][0],intervals[-1][1]]:
            raise ValueError('State fact bounds differ from support')
        if any(not 0<=b[0]-a[1]<=memory['max_gap_seconds'] for a,b in zip(intervals,intervals[1:])):
            raise ValueError('State fact bridges an unsupported gap')
        supported.extend((i,fid) for i in support)
    if set(supported)!=expected or len(supported)!=len(set(supported)):
        raise ValueError('State fact support lost or duplicated an assertion')
    return dict(source_rows=len(restored['observations']), unique_records=len(records),
        unique_facts=len(facts),
        rendered_segments=len(memory['segments']),
        persistent_segments=len(memory['state_segments']),
        inferred_persistent_segments=sum(s['continuity']=='inferred_from_repeated_caption_assertions'
                                         for s in memory['state_segments']),
        restored_payload_equal=True)


def render(memory):
    audit = validate(memory)
    records = {r['id']:r for r in memory['records']}
    facts={f['id']:f for f in memory['facts']}
    repeated=[s for s in memory['state_segments'] if len(s['observation_indices'])>1]
    entries=[]
    repeated_support={}
    for segment in repeated:
        repeated_support.setdefault(segment['fact_id'],[]).append(set(segment['observation_indices']))
        fields=facts[segment['fact_id']]['fields'];category=facts[segment['fact_id']]['category']
        body=CATEGORIES[category]+': '+'; '.join(k+'='+json.dumps(v,ensure_ascii=False) for k,v in fields.items())
        entries.append(dict(segment_id=segment['id'],indices=segment['observation_indices'],
            intervals=[segment['interval']],body=body,continuity=segment['continuity'],priority=0))
    for segment in memory['segments']:
        record=records[segment['record_id']]
        if segment['kind']=='persistent':
            refs=[fid for values in record['evidence_refs'].values() for fid in values]
            if all(any(set(segment['observation_indices'])<=support for support in repeated_support.get(fid,[])) for fid in refs):
                continue
        support=[memory['observations'][i] for i in segment['observation_indices']]
        intervals=[segment['interval']] if segment['kind']=='persistent' else [r['sample_interval'] for r in support]
        row=dict(support[0]);row.pop('record_id');row['evidence']=evidence_for(record,facts)
        text,_=compact_description(dict(observations=[row]))
        entries.append(dict(segment_id=segment['id'],indices=segment['observation_indices'],
            intervals=intervals,body=text.split('): ',1)[1],continuity=segment['continuity'],priority=1))
    lines = []
    source_map = []
    line_number = 1
    for entry in sorted(entries,key=lambda e:(memory['observations'][e['indices'][0]]['sample_interval'][0],e['priority'])):
        support=[memory['observations'][i] for i in entry['indices']]
        intervals=entry['intervals']
        bounds = ', '.join('['+','.join(json.dumps(t) for t in interval)+')' for interval in intervals)
        text = ('Frames ' if len(support)>1 else 'Frame ')+bounds+': '+entry['body']
        lines.append(text)
        source_map.append(dict(segment_id=entry['segment_id'],prompt_line=line_number,
            caption_ids=[r['caption_id'] for r in support],
            sample_intervals=[deepcopy(r['sample_interval']) for r in support],
            displayed_intervals=deepcopy(intervals),continuity=entry['continuity']))
        line_number += text.count('\n')+1
    return '\n'.join(lines), dict(audit,rendered_segments=len(entries), rows=source_map)
