"""Typed source assertions: fact category, event role and provenance are separate.

No question/option reaches extraction. Source markers are metadata, not actors.
Unproven bindings keep their original clause, with multiple information tags.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import re

from .schemas import array, enum, record, validate_output

VERSION = 'semantic-memory-v2'
COLLECTIONS = dict(action='actions', attribute='attributes', state='states', relation='relations',
    change='changes', result='results', event_link='event_links')
KINDS = tuple(COLLECTIONS)
TAGS = KINDS + ('unclassified',)
ROLES = ('patient', 'instrument', 'material', 'body_part', 'surface', 'source_location',
         'destination', 'location', 'manner', 'recipient')
PROPERTIES = ('color', 'material', 'shape', 'size', 'texture', 'pattern', 'appearance', 'other')
DIMENSIONS = ('cleanliness', 'wetness', 'integrity', 'open_closed', 'power', 'posture', 'motion', 'other')
LINKS = ('before', 'after', 'simultaneous', 'cause', 'purpose')
NEGATION = re.compile(r'\b(not|never|no|without)\b|没有|并未|未曾|不曾', re.I)
UNCERTAINTY = re.compile(r'\b(maybe|might|may|possibly|appears|seems|suggests)\b|可能|似乎', re.I)
PREPOSITIONS = dict(instrument=('with', 'using'), material=('with', 'using', 'from', 'of'),
    body_part=('with', 'using', 'in'), surface=('on', 'onto', 'against'),
    source_location=('from', 'out of'), destination=('to', 'into', 'onto'),
    location=('on', 'in', 'at', 'under', 'around', 'beside', 'near'), recipient=('to', 'for'))
RELATION_PREDICATES = {
    'spatial': {'on','in','at','under','above','below','beside','near','behind','in front of',
                'next to','inside','outside','over','between','around','against','within',
                'from','into','onto','to'},
    'possession': {'has','have','holds','owns','belongs to','possesses'},
    'part_whole': {'part of','contains','includes','consists of','composed of','made of'},
    'attachment': {'attached to','connected to','joined to','fastened to','tied to','linked to'},
    # Unrecognized relation wording stays in the original, not an unverified
    # catch-all semantic class. This intentionally favors precision over recall.
    'other': set(),
}


def clean_unit(unit):
    result = deepcopy(unit)
    match = re.match(r'^\s*(#[CO])\s+', unit['text'])
    result['source_marker'] = match.group(1) if match else None
    result['text'] = unit['text'][match.end():].strip() if match else unit['text'].strip()
    result['raw_text'] = unit['text']
    return result


def output_schema(operation, inputs):
    ids = [u['id'] for u in inputs['units']]
    text = {'type':'string', 'minLength':1, 'maxLength':100}
    def variant(kind, fields, optional=()):
        return record(dict(kind=enum([kind]), source_id=enum(ids), **fields), optional)
    variants = [variant('action', dict(actor=deepcopy(text), verb=deepcopy(text),
        **{role:deepcopy(text) for role in ROLES}), ROLES),
        variant('attribute', dict(entity=deepcopy(text), property=enum(PROPERTIES), value=deepcopy(text))),
        variant('state', dict(entity=deepcopy(text), dimension=enum(DIMENSIONS), value=deepcopy(text))),
        variant('relation', dict(left=deepcopy(text), predicate=deepcopy(text), right=deepcopy(text),
            relation_type=enum(['spatial','possession','part_whole','attachment','other']))),
        variant('change', dict(entity=deepcopy(text), before=deepcopy(text), after=deepcopy(text))),
        variant('result', dict(entity=deepcopy(text), predicate=deepcopy(text), value=deepcopy(text))),
        variant('event_link', dict(left_event=deepcopy(text), connector=deepcopy(text),
            right_event=deepcopy(text), link_type=enum(LINKS)))]
    tags = array(enum(TAGS), len(TAGS)); tags['minItems'] = 1
    groups={}
    for kind,variant_schema in zip(KINDS,variants):
        variant_schema=deepcopy(variant_schema)
        variant_schema['properties'].pop('kind');variant_schema['required'].remove('kind')
        groups[COLLECTIONS[kind]]=array(variant_schema,len(ids)*4)
    if inputs.get('category') in KINDS:
        group=deepcopy(groups[COLLECTIONS[inputs['category']]])
        if inputs['category']=='action':
            # Optional decoder fields were silently skipped, including explicit
            # source locations. Review every role; nulls are removed on ingest.
            for role in ROLES:
                group['items']['properties'][role]['type']=['string','null']
                group['items']['required'].append(role)
        return record(dict(facts=group,
            uncertain_source_ids=array(enum(ids),len(ids))))
    return record(dict(**groups,
        residuals=array(record(dict(source_id=enum(ids), information_types=tags)), len(ids))))


def span(value, text):
    match = re.search(r'(?<!\w)'+re.escape(value)+r'(?!\w)', text, re.I)
    return match.span() if match else None


def binding_supported(fact, text):
    """Conservative checks; a literal word match alone does not prove a role."""
    kind = fact['kind']
    metadata = {'kind','source_id','property','dimension','relation_type','link_type'}
    values = {k:v for k,v in fact.items() if k not in metadata}
    spans = {k:span(v,text) for k,v in values.items()}
    if any(v is None for v in spans.values()): return False, 'nonliteral_field'
    if any('#' in v or v == 'C C' for v in values.values()): return False, 'source_marker_in_field'
    if NEGATION.search(text) or UNCERTAINTY.search(text):
        # Do not turn a whole uncertain/negative clause into positive facts.
        return False, 'negative_or_uncertain_clause_requires_original'
    def ordered(*keys):
        return all(spans[a][1] <= spans[b][0] for a,b in zip(keys,keys[1:]))
    def near_prefix(key, connectors):
        start = spans[key][0]
        prefix = text[:start]
        return re.search(r'\b(?:'+'|'.join(re.escape(c) for c in connectors)+
            r')\s+(?:(?:a|an|the|his|her|their|its|my|your|both)\s+)?$', prefix, re.I) is not None
    if kind == 'action':
        if fact['verb'].lower() in ('is','are','was','were','be','been','being','has','have','had'):
            return False,'copula_or_possession_is_not_action'
        if not ordered('actor','verb'): return False, 'actor_verb_order'
        gap = text[spans['actor'][1]:spans['verb'][0]]
        if 'manner' in fact and spans['actor'][1]<=spans['manner'][0] and spans['manner'][1]<=spans['verb'][0]:
            gap=re.sub(re.escape(fact['manner']),'',gap,flags=re.I)
        if not re.fullmatch(r'\s*(?:(?:is|are|was|were|has|have|had)\s+)?',gap,re.I):
            return False, 'actor_not_attached_to_verb'
        # Predicate is a verb, not a whole sentence with the object hidden in it.
        if not re.fullmatch(r'[A-Za-z]+(?:\s+(?:up|down|out|off|away|back|around|at|to))?',fact['verb']):
            return False, 'verb_contains_arguments_or_unsupported_syntax'
        if 'patient' in fact:
            if not ordered('verb','patient'): return False, 'patient_order'
            gap = text[spans['verb'][1]:spans['patient'][0]]
            if not re.fullmatch(r'\s+(?:(?:a|an|the|his|her|their|its|my|your)\s+)?',gap,re.I):
                return False, 'patient_is_not_direct_argument'
        for role, connectors in PREPOSITIONS.items():
            if role in fact and (spans[role][0] < spans['verb'][1] or not near_prefix(role,connectors)):
                return False, 'unsupported_'+role+'_binding'
        if 'manner' in fact:
            start,end = spans['manner']
            valid_manner=fact['manner'].lower().endswith('ly') or fact['manner'].lower() in (
                'back and forth','up and down','clockwise','counterclockwise','anticlockwise')
            if not valid_manner or not (end <= spans['verb'][0] or start >= spans['verb'][1]):
                return False, 'unsupported_manner_binding'
        return True, None
    if kind in ('attribute','state'):
        entity,value = spans['entity'],spans['value']
        if value[1] <= entity[0] and not text[value[1]:entity[0]].strip(): return True,None
        if entity[1] <= value[0] and re.fullmatch(r'\s+(?:is|are|was|were)\s+',text[entity[1]:value[0]],re.I):
            return True,None
        if kind == 'attribute' and fact['property']=='material' and entity[1] <= value[0] and re.fullmatch(
                r'\s+(?:is\s+)?made\s+of\s+',text[entity[1]:value[0]],re.I): return True,None
        return False,'unsupported_property_binding'
    if kind == 'relation':
        if not ordered('left','predicate','right'):return False,'relation_order'
        predicate=re.sub(r'^(?:is|are|was|were)\s+','',fact['predicate'].lower().strip())
        if predicate not in RELATION_PREDICATES[fact['relation_type']]:
            return False,'unsupported_relation_subtype_binding'
        return True,None
    if kind == 'change':
        if not ordered('before','after'): return False,'change_order'
        before = text[:spans['before'][0]]
        between = text[spans['before'][1]:spans['after'][0]]
        valid = re.search(r'\bfrom\s+$',before,re.I) and re.fullmatch(r'\s+to\s+',between,re.I)
        return (True,None) if valid else (False,'change_not_explicit')
    if kind == 'result':
        if not ordered('entity','predicate','value'): return False,'result_order'
        explicit = fact['predicate'].lower() in ('becomes','became','produces','produced','yields','yielded','results in')
        return (True,None) if explicit else (False,'result_not_explicit')
    if kind == 'event_link':
        allowed = dict(before=('before','then'), after=('after',), simultaneous=('while','as'),
            cause=('because','causes','caused'), purpose=('in order to','so as to'))
        if fact['connector'].lower() not in allowed[fact['link_type']]: return False,'link_not_explicit'
        # Connector order must match the supported simple clause template.
        if not ordered('left_event','connector','right_event'): return False,'unsupported_link_syntax'
        return True,None
    return False,'unknown_kind'


def exact_simple_clause(unit, facts):
    """Only remove an original when one simple clause has reversible bindings.

    No bag-of-words criterion: connectors, possessives and word positions matter.
    Complex/multi-fact sentences keep a multi-tagged literal clause.
    """
    if len(facts) != 1: return False
    f = facts[0]; text = unit['text'].strip().rstrip('.!?')
    if f['kind'] == 'action':
        if any(k in f for k in ROLES if k not in ('patient','instrument','surface')): return False
        expression = re.escape(f['actor'])+r'\s+'+re.escape(f['verb'])
        if 'patient' in f: expression += r'\s+(?:(?:a|an|the)\s+)?'+re.escape(f['patient'])
        if 'surface' in f: expression += r'\s+on\s+(?:(?:a|an|the)\s+)?'+re.escape(f['surface'])
        if 'instrument' in f: expression += r'\s+(?:with|using)\s+(?:(?:a|an|the)\s+)?'+re.escape(f['instrument'])
        return re.fullmatch(expression,text,re.I) is not None
    if f['kind'] in ('attribute','state'):
        return re.fullmatch(re.escape(f['entity'])+r'\s+(?:is|are|was|were)\s+'+re.escape(f['value']),text,re.I) is not None
    return False


def accept_batch(units, report):
    validate_output(report,output_schema('semantic_memory_extract',dict(units=units)))
    sources = {u['id']:u for u in units}; accepted=[]; rejected=[]; by_source=defaultdict(list)
    requested=defaultdict(set)
    for r in report['residuals']: requested[r['source_id']].update(r['information_types'])
    for raw in [dict(f,kind=k) for k,collection in COLLECTIONS.items() for f in report[collection]]:
        fact=deepcopy(raw);source=sources[fact['source_id']]
        valid,reason=binding_supported(fact,source['text'])
        if not valid:
            rejected.append(dict(record=fact,reason=reason));requested[source['id']].add(fact['kind']);continue
        accepted.append(fact);by_source[source['id']].append(fact)
    residuals=[]
    for uid,unit in sources.items():
        facts=by_source[uid]
        if requested[uid] or not exact_simple_clause(unit,facts):
            tags=requested[uid]|{f['kind'] for f in facts}
            if not tags: tags={'unclassified'}
            residuals.append(dict(source_id=uid,text=unit['text'],information_types=[k for k in TAGS if k in tags],
                reason='complex_or_unverified_bindings' if facts else 'not_reliably_structured'))
    return dict(facts=accepted,residuals=residuals,rejected=rejected)


def normalize(bundle,meter,batch_size=8):
    from .budget import ResourceExhausted,ToolFailure,exploration_exhausted
    units=[clean_unit(u) for u in bundle['units']]
    facts=[];residuals=[];rejected=[];failures=[];budget_ended=False;role_reviews=0
    category_coverage={k:dict(checked_units=0,failed_units=0,unattempted_units=0) for k in KINDS}
    for start in range(0,len(units),batch_size):
        batch=units[start:start+batch_size]
        combined={v:[] for v in COLLECTIONS.values()};uncertain=defaultdict(set)
        for kind,collection in COLLECTIONS.items():
            # Multi-source outputs misattributed change/event clauses to the
            # first ID. A single authorized ID makes those joins impossible.
            width=1
            for offset in range(0,len(batch),width):
                portion=batch[offset:offset+width]
                if budget_ended:
                    category_coverage[kind]['unattempted_units']+=len(portion)
                    for u in portion:uncertain[u['id']].add('unclassified')
                    continue
                # Every category independently sees every already-read source.
                inputs=dict(category=kind,units=[dict(id=u['id'],text=u['text']) for u in portion],source_type='caption')
                try:
                    report,call=meter.json('semantic_memory_extract',inputs,('facts','uncertain_source_ids'))
                    validate_output(report,output_schema('semantic_memory_extract',inputs))
                    if kind=='action':
                        problems=[]
                        for raw in report['facts']:
                            fact=dict(kind='action',**{k:v for k,v in raw.items() if v is not None})
                            text=next(u['text'] for u in portion if u['id']==fact['source_id'])
                            valid,reason=binding_supported(fact,text)
                            if not valid and (reason.startswith('unsupported_') and reason.endswith('_binding')
                                              or reason in ('patient_is_not_direct_argument','patient_order')):
                                problems.append(dict(record=fact,reason=reason))
                        if problems:
                            # One fresh model judgment using the SAME source and
                            # explicit validator feedback; never relabel a role
                            # automatically. Both calls remain metered/logged.
                            rejected.extend(dict(p,stage='before_role_review') for p in problems)
                            inputs['role_review']=dict(rejected=problems,
                                instruction='Re-extract this source once. Correct unsupported role bindings; do not repeat the rejected assignments. A noun after on can be a surface, not consumed material or a direct patient.')
                            role_reviews+=1
                            for p in problems:uncertain[p['record']['source_id']].add('action')
                            report,call=meter.json('semantic_memory_extract',inputs,('facts','uncertain_source_ids'))
                            validate_output(report,output_schema('semantic_memory_extract',inputs))
                    category_coverage[kind]['checked_units']+=len(portion)
                    combined[collection].extend({k:v for k,v in fact.items() if v is not None}
                                                for fact in report['facts'])
                    for uid in report['uncertain_source_ids']:uncertain[uid].add(kind)
                except (ToolFailure,ResourceExhausted,ValueError) as exc:
                    if isinstance(exc,ToolFailure) and not exploration_exhausted(exc) and any(
                        s in str(exc).lower() for s in ('cuda','out of memory','worker','timeout','deadline','exceeded reserved')): raise
                    budget_ended=isinstance(exc,ResourceExhausted) or exploration_exhausted(exc)
                    category_coverage[kind]['failed_units']+=len(portion)
                    failures.append(dict(batch_start=start+offset,category=kind,error=str(exc)))
                    for u in portion:uncertain[u['id']].add('unclassified')
        combined['residuals']=[dict(source_id=u,information_types=[k for k in TAGS if k in tags]) for u,tags in uncertain.items()]
        result=accept_batch(batch,combined)
        facts.extend(result['facts']);residuals.extend(result['residuals']);rejected.extend(result['rejected'])
    memory=dict(schema=VERSION,**{v:[] for v in COLLECTIONS.values()},residuals=residuals,
        sources=units,prior_hypotheses=deepcopy(bundle.get('structural_memory',{})))
    for i,f in enumerate(facts): memory[COLLECTIONS[f['kind']]].append(dict(f,id=f'F{i}'))
    coverage=validate_coverage(bundle,memory)
    audit=dict(source_units=len(units),facts=len(facts),facts_by_type=dict(Counter(f['kind'] for f in facts)),
        residual_units=len(residuals),residual_tags=dict(Counter(t for r in residuals for t in r['information_types'])),
        rejected_records=rejected,batch_failures=failures,role_review_calls=role_reviews,
        category_coverage=category_coverage,coverage=coverage)
    return memory,audit


def validate_coverage(bundle,memory):
    expected={u['id'] for u in bundle['units']}
    facts=[f for k in COLLECTIONS.values() for f in memory[k]]
    represented={f['source_id'] for f in facts}|{r['source_id'] for r in memory['residuals']}
    if represented != expected: raise ValueError('Semantic memory lost or invented a source unit')
    read=set(bundle.get('read_caption_ids',[]))
    covered={c for u in memory['sources'] if u['id'] in represented for c in u['source_caption_ids']}
    if not read <= covered: raise ValueError('Semantic memory lost an already-read caption')
    return dict(read_captions=len(read),covered_captions=len(read),source_units=len(expected),coverage=1.,
        policy='Source accounting; not independent proof of semantic correctness.')


def view(memory, included=None):
    """Self-contained dated observations; no joins through a source registry."""
    included=set(KINDS if included is None else included)
    if not included <= set(KINDS): raise ValueError('Unknown semantic category')
    by_source=defaultdict(lambda:defaultdict(list))
    for kind,collection in COLLECTIONS.items():
        if kind in included:
            for f in memory[collection]:
                by_source[f['source_id']][collection].append({k:deepcopy(v) for k,v in f.items()
                    if k not in ('id','source_id','kind')})
    residuals={r['source_id']:r for r in memory['residuals']}
    observations=[];overlaps=[]
    for u in memory['sources']:
        groups=dict(by_source[u['id']]);r=residuals.get(u['id'])
        if r and ('unclassified' in r['information_types'] or included & set(r['information_types'])):
            groups['categorized_original']=dict(text=r['text'],information_types=r['information_types'])
            if set(r['information_types'])-included-{'unclassified'}:overlaps.append(u['id'])
        if groups:
            observations.append(dict(source_id=u['id'],source_type=u['source_type'],status=u['status'],
                sample_intervals=deepcopy(u['source_intervals']),**groups))
    return dict(schema=VERSION,observations=observations,
        policy='Source assertions, not independent visual verification. Sample intervals are discrete observations, '
            'not continuous duration or repeated-event counts. Unknown roles are omitted. '
            'A state describes a temporary condition; properties describe relatively stable features. '
            'Identity and causal hypotheses are not established facts.',
        category_audit=dict(included=sorted(included),residual_sources_overlapping_excluded_categories=overlaps))
