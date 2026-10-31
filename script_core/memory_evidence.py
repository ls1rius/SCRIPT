"""Bounded original quotations supporting a caption-derived temporal memory."""
from copy import deepcopy
import json


def supporting_quotes(captions, selected, memory, maximum=12, per_fact=2):
    """Select cited sources without another model call or access to unread text.

    Transitions are considered first so both temporal endpoints can be retained.
    A fact's first/last citations are admitted together; shared quotations count
    once. Every limit or missing source remains explicit in the selection audit.
    """
    if any(type(n) is not int or n < 1 for n in (maximum, per_fact)):
        raise ValueError('Quotation limits must be positive integers')
    if memory.get('source_type') != 'caption':
        raise ValueError('Memory quotations require caption-derived memory')
    if any(type(i) is not int or not 0 <= i < len(captions) for i in selected):
        raise ValueError('Invalid selected caption position')
    sources = {c['id']: c for c in captions}
    if len(sources) != len(captions):
        raise ValueError('Duplicate source caption IDs')
    readable = {captions[i]['id'] for i in selected}
    states = {s['id']: s for s in memory.get('states', [])}

    def citations(value):
        refs = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('source_caption_ids', 'caption_ids', 'left_caption_ids', 'right_caption_ids'):
                    refs.extend(item)
                elif key == 'caption_id':
                    refs.append(item)
                else:
                    refs.extend(citations(item))
        elif isinstance(value, list):
            for item in value:
                refs.extend(citations(item))
        return refs

    facts = []
    for kind in ('transitions', 'causes', 'actions', 'relations', 'attributes', 'states', 'entities', 'identities', 'lineage',
                 'unclassified',
                 'unresolved_observations'):
        for index, fact in enumerate(memory.get(kind, [])):
            refs = citations(fact)
            if kind == 'transitions':
                for endpoint in ('before', 'after'):
                    if fact.get(endpoint) in states:
                        refs.extend(citations(states[fact[endpoint]]))
            if kind == 'entities':
                for state in states.values():
                    if state.get('entity_id') == fact['id']:
                        refs.extend(citations(state))
            refs = list(dict.fromkeys(refs))
            if not set(refs) <= readable:
                raise ValueError('Memory evidence cites unread or unknown captions')
            refs.sort(key=lambda cid: (sources[cid]['interval'], cid))
            # Keep the endpoints before filling intermediate supporting sources.
            ordered = list(dict.fromkeys(refs[:1] + refs[-1:] + refs[1:-1]))
            facts.append(dict(id=f"{kind}:{fact.get('id', index)}", sources=refs,
                              candidates=ordered[:per_fact]))
            if kind == 'transitions':
                cause_refs = sorted(set(citations(fact.get('cause', {}))),
                    key=lambda cid: (sources[cid]['interval'], cid))
                if cause_refs:
                    # The action/tool may occur between the before/after states.
                    # Give it its own quotation allocation rather than dropping
                    # it just because the transition already has two endpoints.
                    cause_order = list(dict.fromkeys(cause_refs[:1] + cause_refs[-1:] + cause_refs[1:-1]))
                    facts.append(dict(id=f"{kind}:{fact.get('id', index)}:cause",
                        sources=cause_refs, candidates=cause_order[:per_fact]))

    chosen = set()
    for fact in facts:
        needed = set(fact['candidates']) - chosen
        if len(chosen) + len(needed) <= maximum:
            chosen.update(needed)
    quotes = []
    for cid in sorted(chosen, key=lambda c: (sources[c]['interval'], c)):
        quotes.append(dict(caption_id=cid, interval=deepcopy(sources[cid]['interval']),
            text=sources[cid]['text'],
            supports=[f['id'] for f in facts if cid in f['sources']]))
    candidate_ids = {cid for f in facts for cid in f['sources']}
    audit = dict(mode='memory_quotes', max_quotes=maximum, quotes_per_fact=per_fact,
        source_policy='Only original captions cited by the retained memory and already read upstream.',
        selected_caption_count=len(readable), candidate_caption_count=len(candidate_ids),
        quoted_caption_ids=[q['caption_id'] for q in quotes],
        omitted_caption_ids=sorted(candidate_ids - chosen),
        facts_without_quoted_support=[f['id'] for f in facts if not chosen.intersection(f['sources'])],
        facts_with_omitted_sources=[f['id'] for f in facts if set(f['sources']) - chosen],
        fact_count=len(facts), empty_memory=not memory.get('entities'),
        no_supporting_quotes=not quotes, quotes=quotes)
    return audit


def prepare_memory_input(captions, selected, memory, visual_observations=(), maximum=12,
                         per_fact=2, mode='memory_quotes', included_types=None):
    """Build one final evidence view and its audit, shared by preview and inference."""
    if mode == 'memory_quotes':
        audit=supporting_quotes(captions,selected,memory,maximum,per_fact)
        return memory, list(visual_observations), audit
    if mode != 'typed_memory_quotes':
        raise ValueError('Unknown memory quotation mode')
    from .typed_memory import typed_context, INFORMATION_TYPES
    typed,visual,types=typed_context(captions,selected,memory,visual_observations,
        INFORMATION_TYPES if included_types is None else included_types)
    audit=supporting_quotes(captions,selected,typed,maximum,per_fact)
    audit.update(mode=mode,information_types=types)
    audit['no_retained_information']=not (
        sum(types['retained_caption_counts'].values())+sum(types['retained_visual_counts'].values()))
    for quote in audit['quotes']:
        quote['information_types']=types['source_caption_types'].get(quote['caption_id'],[])
    return typed,visual,audit


def qa_inputs(query, memory, audit, visual_observations=()):
    """Present the memory first, with quotations kept verbatim and separately cited."""
    dump = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    policy = {k: v for k, v in audit.items() if k != 'quotes'}
    if 'information_types' in policy:
        types=policy['information_types']
        # Full pre-filter counts and overlap diagnostics belong in the audit,
        # not in a prompt intended to omit a semantic information category.
        policy['information_types']={k:types[k] for k in ('schema','included_types',
            'retained_caption_counts','retained_visual_counts','category_policy')}
    prompt = 'Temporal entity memory (caption assertions and text-inferred links):\n' + dump(memory)
    prompt += '\n\nSupporting original caption quotations:\n' + dump(audit['quotes'])
    prompt += '\n\nEvidence coverage and quotation limits:\n' + dump(policy)
    prompt += '\n\nSupplemental sampled visual observations (accepted upstream; apply only to cited samples):\n'
    prompt += dump(visual_observations if isinstance(visual_observations,dict) else list(visual_observations))
    prompt += '\n\nQuestion:\n' + dump(query['question'])
    prompt += '\n\nOptions (original order):\n' + dump(
        {chr(65+i): option for i, option in enumerate(query['options'])})
    prompt += '\n\nRespond with prediction: <one letter A-E>\nexplanation: <one sentence>\nconfidence: <1-100>'
    head = ('Answer the video question using the temporal entity memory, its original supporting '
        'quotations, and sampled visual observations. The memory is partial; unreported facts, '
        'continuity and causes remain unknown. A citation identifies a source, not independent '
        'verification of its interpretation. Use explicit objects, tools, actions and state changes '
        'to distinguish the options. Repetition of a generic action does not establish the overall '
        'goal. Do not invent task details from common practice or turn a local observation into a '
        'whole-video conclusion. If evidence is insufficient, choose the closest supported option '
        'and state the uncertainty. The prediction field must contain only a single option letter; '
        'the person named C is not an option identifier.')
    if memory.get('layout') == 'typed-memory-v1':
        head += (' Information types and evidence sources are separate. Actions describe events; '
            'attributes describe properties; states describe conditions. Temporal slots are storage '
            'indexes, not proof of a physical state or change. Action participant IDs are not '
            'necessarily actors. Null semantic roles were not extracted. Preserve unclassified '
            'and inferred information as uncertain, and do not silently reclassify it.')
    return dict(query_id=query['id'], head=head, prompts=[prompt])
