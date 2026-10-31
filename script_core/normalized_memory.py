"""Source-bound normalization of the exact evidence supplied to final QA.

Normalization never reads new captions, images, questions, options or answers.
Atomic fields must be literal spans of a cited unit. Units that are omitted or
lose informative words remain in a categorized literal-evidence collection.
"""
from collections import Counter,defaultdict
from copy import deepcopy
import json
import re

from .schemas import array,enum,record,short_text,validate_output

KINDS=('action','attribute','state','relation','unclassified')
FIELDS=('subject','predicate','object','value','tool','location')
STOP=set('a an the c o is are was were be been being of and or his her their its my your '
         'he she they it this that these those with using by some then'.split())-{'c'}


def read_prompt(inputs):
    prompt=inputs['prompts'][0]
    markers=dict(memory='Temporal entity memory (caption assertions and text-inferred links):\n',
        quotes='\n\nSupporting original caption quotations:\n',
        policy='\n\nEvidence coverage and quotation limits:\n',
        visual='\n\nSupplemental sampled visual observations (accepted upstream; apply only to cited samples):\n')
    result={}
    for name,marker in markers.items():
        start=prompt.index(marker)+len(marker)
        result[name]=json.JSONDecoder().raw_decode(prompt[start:])[0]
    return result


def evidence_units(inputs):
    """Extract/deduplicate only statements actually present in scheme 1 input."""
    parts=read_prompt(inputs);memory=parts['memory'];units=[];lookup={}
    labels={e['id']:e['label'] for e in memory.get('entities',[])}
    def add(text,origin,source_type,status,kind='unclassified',caption_ids=(),frame_ids=(),interval=None):
        if not isinstance(text,str) or not text.strip():return
        key=(text.strip(),source_type,status)
        if key in lookup:
            unit=lookup[key]
            unit['origins'].append(origin)
            for field,values in [('source_caption_ids',caption_ids),('source_frame_ids',frame_ids)]:
                unit[field]=list(dict.fromkeys(unit[field]+list(values)))
            if interval is not None and interval not in unit['source_intervals']:unit['source_intervals'].append(deepcopy(interval))
            return
        unit=dict(id=f'U{len(units)}',text=text.strip(),origins=[origin],source_type=source_type,
            status=status,kind_hint=kind,source_caption_ids=list(caption_ids),source_frame_ids=list(frame_ids),
            source_intervals=[deepcopy(interval)] if interval is not None else [])
        units.append(unit);lookup[key]=unit
    for e in memory.get('entities',[]):
        for i,attr in enumerate(e.get('attributes',[])):
            add(f"{e['label']} {attr['field']}: {attr['value']}",f"attribute:{e['id']}:{i}",
                'caption','caption_asserted','attribute',attr.get('source_caption_ids',[]))
    for state in memory.get('states',[]):
        add(state.get('description',''),'state:'+state['id'],'caption',state.get('status','caption_asserted'),
            caption_ids=state.get('source_caption_ids',[]),interval=state.get('observed_support_interval'))
    for i,row in enumerate(memory.get('relations',[])):
        text=' '.join([labels.get(row['subject'],row['subject']),row['predicate'],labels.get(row['object'],row['object'])])
        add(text,'relation:'+row.get('id',str(i)),'caption',row.get('status','caption_asserted'),
            'relation',row.get('source_caption_ids',[]))
    for i,row in enumerate(memory.get('unresolved_observations',[])):
        ob=row.get('observation',{});cid=ob.get('caption_id')
        add(ob.get('state',''),f'unresolved:{i}','caption','caption_asserted',
            caption_ids=[cid] if cid else [],interval=[row['pts'],row['pts']] if 'pts' in row else None)
    for i,row in enumerate(memory.get('identities',[])):
        add(row.get('reason',''),f'identity_reason:{i}','text_inference','text_inferred',
            caption_ids=row.get('caption_ids',[]))
    for quote in parts['quotes']:
        add(quote['text'],'quote:'+quote['caption_id'],'caption','caption_asserted',
            caption_ids=[quote['caption_id']],interval=quote['interval'])
    for i,ob in enumerate(parts['visual']):
        add(ob.get('description',''),f'visual:{i}','sampled_visual',
            ob.get('status','accepted_upstream_visual_model_observation'),ob.get('kind','unclassified'),
            frame_ids=ob.get('source_frame_ids',[]),interval=ob.get('interval'))
    # These were already structured in scheme 1. Preserve them without asking
    # another model to reinterpret identity, chronology or an unknown cause.
    structural=dict(entities=[{k:deepcopy(e[k]) for k in ('id','label','scene_ids') if k in e}
        for e in memory.get('entities',[])],
        temporal_slots=[{k:deepcopy(s[k]) for k in ('id','entity_id','scene_id','first_observed_at',
            'last_observed_at','valid_from_bounds','valid_to_bounds','source_caption_ids','boundary','status') if k in s}
            for s in memory.get('states',[])],
        transitions=deepcopy(memory.get('transitions',[])),lineage=deepcopy(memory.get('lineage',[])),
        identities=[{k:deepcopy(v) for k,v in row.items() if k!='reason'} for row in memory.get('identities',[])],
        evidence_policy=memory.get('evidence_policy',''))
    return dict(units=units,structural_memory=structural,qa_policy=parts['policy'])


def output_schema(inputs):
    ids=[u['id'] for u in inputs['units']]
    text={'type':['string','null'],'maxLength':80}
    refs=array(enum(ids),3);refs['minItems']=1
    field={'anyOf':[{'type':'null'},enum(['color','material','shape','size','texture','appearance','condition','other','unknown'])]}
    variants=[]
    for kind in KINDS[:-1]:
        fields={f:deepcopy(text) for f in FIELDS}
        for name in ({'action':['value'],'attribute':['object','tool','location'],
                      'state':['object','tool','location'],'relation':['value','tool','location']}[kind]):
            fields[name]={'type':'null'}
        variants.append(record(dict(kind=enum([kind]),
            attribute_field=deepcopy(field) if kind=='attribute' else {'type':'null'},
            **fields,source_ids=refs)))
    fact={'anyOf':variants}
    retained=record(dict(source_id=enum(ids),kind=enum(KINDS)))
    return record(dict(facts=array(fact,min(12,len(ids)*3)),retained=array(retained,len(ids))))


def words(text):
    # Character coverage for Chinese, word coverage for Latin text; no inference
    # of stemming/synonyms is used to authorize a newly generated value.
    latin=re.findall(r'[a-z0-9]+',text.lower())
    han=re.findall(r'[\u3400-\u9fff]',text)
    return (set(latin)-STOP)|set(han)-set('的一个人用在把将与和了')


def literal(value,text):
    if not isinstance(value,str) or not value.strip():return False
    if re.search(r'[\u3400-\u9fff]',value):return value.strip() in text
    return re.search(r'(?<!\w)'+re.escape(value.strip())+r'(?!\w)',text,re.I) is not None


def role_order_supported(fact,text):
    """Conservative English role-direction guard; unclear cases keep originals.

    This is a syntactic check, not independent semantic verification. It avoids
    accepting reversed subject/object or attributing an adjective to a nearby
    different object merely because all words occur somewhere in one sentence.
    """
    if re.search(r'[\u3400-\u9fff]',text):return False
    body=re.sub(r'^#[CO]\s*','',text)
    def span(field):
        value=fact.get(field)
        if not value:return None
        m=re.search(r'(?<!\w)'+re.escape(value.strip())+r'(?!\w)',body,re.I)
        return m.span() if m else None
    subject=span('subject');predicate=span('predicate');obj=span('object');value=span('value')
    kind=fact['kind']
    if kind in ('action','relation'):
        if not subject or not predicate or subject[1]>predicate[0]:return False
        if obj and predicate[1]>obj[0]:return False
        if kind=='action' and fact.get('tool'):
            tool=span('tool')
            if not tool or not re.search(r'\b(with|using)\s+(?:(?:a|an|the|his|her|their)\s+)?$',
                    body[max(0,tool[0]-30):tool[0]],re.I):return False
        return True
    if kind in ('attribute','state'):
        if not subject or not value:return False
        if value[1]<=subject[0]:
            return re.fullmatch(r'\s*',body[value[1]:subject[0]]) is not None
        if subject[1]<=value[0]:
            return re.fullmatch(r'\s+(?:is|are|was|were|has|have)\s+',body[subject[1]:value[0]],re.I) is not None
    return False


def accept_batch(units,report):
    """Quarantine unsupported normalizations; retain all unrepresented content."""
    report=deepcopy(report)
    # Some models quote an absent nullable value. This is a serialization
    # correction, never authorization for a new semantic value.
    for row in report.get('facts',[]):
        for field in (*FIELDS,'attribute_field'):
            if isinstance(row.get(field),str) and row[field].strip().lower() in ('null','none',''):
                row[field]=None
    validate_output(report,output_schema(dict(units=units)))
    sources={u['id']:u for u in units};facts=[];rejected=[]
    covered=defaultdict(set);model_retained={r['source_id']:r['kind'] for r in report['retained']}
    for index,raw in enumerate(report['facts']):
        values=[v for f in FIELDS if (v:=raw.get(f)) is not None]
        refs=list(dict.fromkeys(raw['source_ids']))
        required={'action':('predicate',),'attribute':('subject','value'),
            'state':('subject','value'),'relation':('subject','predicate','object')}[raw['kind']]
        complete=all(raw.get(f) for f in required)
        # Every semantic field must co-occur in at least one cited statement;
        # unrelated sources cannot be stitched into an invented interaction.
        support=[cid for cid in refs if values and all(literal(v,sources[cid]['text']) for v in values)]
        negations={'not','never','no','without'}
        semantic_words=words(' '.join(values))
        support=[cid for cid in support if not ((words(sources[cid]['text'])&negations)-semantic_words)]
        support=[cid for cid in support if role_order_supported(raw,sources[cid]['text'])]
        if not complete or not support or any(len(re.findall(r'\w+',v))>6 for v in values):
            rejected.append(dict(index=index,record=deepcopy(raw),reason='Incomplete, unsupported literal fields, polarity or role direction'))
            continue
        row={k:deepcopy(v) for k,v in raw.items() if k!='source_ids'}
        if row['kind']!='attribute':row['attribute_field']=None
        row.update(evidence_ids=support,
            source_type=sorted({sources[c]['source_type'] for c in support}),
            epistemic_status=sorted({sources[c]['status'] for c in support}),
            source_caption_ids=list(dict.fromkeys(c for u in support for c in sources[u]['source_caption_ids'])),
            source_frame_ids=list(dict.fromkeys(c for u in support for c in sources[u]['source_frame_ids'])),
            source_intervals=[interval for u in support for interval in sources[u]['source_intervals']],
            source_qualifiers=sorted(set().union(*(words(sources[u]['text']) for u in support))&
                {'not','never','no','without','maybe','may','might','possibly','appears','suggests','or'}))
        facts.append(row)
        for cid in support:
            covered[cid].update(words(' '.join(values)))
    retained=[]
    for uid,unit in sources.items():
        missing=words(unit['text'])-covered[uid]
        if uid in model_retained or missing or uid not in covered:
            kinds={f['kind'] for f in facts if uid in f['evidence_ids']}
            kind=model_retained.get(uid,unit.get('kind_hint','unclassified'))
            if kind not in KINDS:kind='unclassified'
            if kind=='unclassified' and len(kinds)==1:kind=next(iter(kinds))
            retained.append(dict(unit,information_type=kind,
                retention_reason='model_requested' if uid in model_retained else 'unrepresented_content',
                unrepresented_tokens=sorted(missing)))
    return dict(facts=facts,retained=retained,rejected=rejected)


def normalize(bundle,meter,batch_size=4):
    """All failures preserve source text; infrastructure errors propagate."""
    from .budget import ToolFailure,ResourceExhausted
    facts=[];retained=[];rejected=[];failures=[]
    for start in range(0,len(bundle['units']),batch_size):
        units=bundle['units'][start:start+batch_size]
        try:
            report,call=meter.json('normalize_memory_units',dict(units=units,source_type='caption'),('facts','retained'))
            accepted=accept_batch(units,report)
            for row in accepted['facts']:row['normalization_call_id']=call['call_id']
        except (ToolFailure,ResourceExhausted,ValueError) as exc:
            message=str(exc).lower()
            if any(x in message for x in ('cuda','out of memory','worker','deadline','timeout','exceeded reserved')):raise
            failures.append(dict(batch_start=start,error=str(exc)))
            accepted=dict(facts=[],retained=[dict(u,information_type=u['kind_hint'] if u['kind_hint'] in KINDS else 'unclassified',
                retention_reason='normalization_failure') for u in units],rejected=[])
        facts.extend(accepted['facts']);retained.extend(accepted['retained']);rejected.extend(accepted['rejected'])
    represented={u for f in facts for u in f['evidence_ids']}|{r['id'] for r in retained}
    if represented!={u['id'] for u in bundle['units']}:raise ValueError('Normalization lost an evidence unit')
    normalized=dict(schema='normalized-evidence-v1',structural_memory=deepcopy(bundle['structural_memory']),
        actions=[],attributes=[],states=[],relations=[],retained_evidence={k:[] for k in KINDS},
        evidence_policy='Fields are normalized source assertions, not independent verification. '
            'Null roles remain unknown. Categorized literal evidence preserves details not represented by atomic fields.')
    groups=dict(action='actions',attribute='attributes',state='states',relation='relations')
    for i,fact in enumerate(facts):normalized[groups[fact['kind']]].append(dict(fact,id=f'N{i}'))
    for row in retained:normalized['retained_evidence'][row['information_type']].append(row)
    audit=dict(source_units=len(bundle['units']),normalized_facts=len(facts),retained_units=len(retained),
        facts_by_type=dict(Counter(f['kind'] for f in facts)),
        attribute_fields=dict(Counter(f.get('attribute_field') or 'unknown' for f in facts if f['kind']=='attribute')),
        retained_by_type=dict(Counter(r['information_type'] for r in retained)),
        rejected_records=rejected,batch_failures=failures,all_source_units_accounted_for=True,
        raw_text_removed_units=len(bundle['units'])-len(retained))
    return normalized,audit
