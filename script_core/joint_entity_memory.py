"""E: one source-bound extraction, local entity references and entity timelines."""
from collections import Counter, defaultdict
from copy import deepcopy
import json
import re

from .semantic_memory import (COLLECTIONS, ROLES, PROPERTIES, DIMENSIONS, LINKS,
    PREPOSITIONS, NEGATION, UNCERTAINTY, RELATION_PREDICATES, binding_supported, clean_unit, span)
from .schemas import record, array, enum, validate_output

VERSION='joint-entity-memory-v3'
OPERATION='e_joint_extract'
ENTITY_ROLES={'actor','patient','instrument','material','body_part','surface',
    'source_location','destination','location','recipient','entity','left','right'}
PRONOUNS={'it','its','they','them','their','he','him','his','she','her','this','that'}
COLORS={'red','blue','green','yellow','black','white','brown','orange','pink','purple','gray','grey'}
TEMPORARY_VALUES={'wet','dry','damp','moist','clean','dirty','open','closed','broken','intact',
    'on','off','damaged','undamaged','standing','sitting','lying','moving','stationary'}
FUNCTION_WORDS={'on','in','at','under','above','below','from','to','into','onto','with','using','without',
    'is','are','was','were','be','being','has','have','had','a','an','the','and','or','then','as','before','after','because','of','for','by'}
PLURALS={word+'s':word for word in ['book','bar','rod','cup','bowl','plate','tile','door','pen','cloth','bag','box','table','shelf','hand','tool','piece','stick']}

PROMPT='''Extract ALL seven categories from this ONE source in one JSON response. No questions, options or images are supplied.
Return actions, attributes, states, relations, changes, results, event_links, uncertain. Use [] for absent categories.
ONE sentence can support MULTIPLE categories: do not stop after its action. All strings must copy literal spans from unit.text.
Actions use actor, verb, roles=[{role,value},...]. Review every role, but emit only explicitly supported roles, once each.
patient=complete direct object including adjectives; instrument=used tool; material=consumed input; body_part=hand/arm/etc;
surface=operating surface; source_location=from location; destination=to/into target; location=where; manner=how; recipient=receiver.
Role values exclude their introducing prepositions. Body parts are not tools. Never invent ink, hands, locations or placeholders.
Attributes are stable color/material/shape/size/texture/pattern/appearance/other, including adjectives in action objects.
States are explicit temporary cleanliness/wetness/integrity/open_closed/power/posture/motion/other.
Wet/dry/damp/clean/dirty/open/closed are temporary states, never size/color attributes. An action does not establish a resulting condition.
Relations link entities with spatial/possession/part_whole/attachment/other. Copy the linking predicate, including on/in/holds.
Changes require explicit before and after conditions in the SAME source. Results require an explicit product/outcome predicate.
Event links require two event phrases and their explicit connector in this SAME source; then/before mean BEFORE, not after.
Never infer identity, intention, goal or cause. Entity IDs and noun heads are assigned later by code; do not output entity lists.
If the source is uncertain, set uncertain=true. If review is present, correct its rejected bindings using this source only.
Illustrative subsets below omit empty arrays for readability; your JSON must include every required category.
C wipes the red book with a cloth. -> {"actions":[{"actor":"C","verb":"wipes","roles":[{"role":"patient","value":"red book"},{"role":"instrument","value":"cloth"}]}],"attributes":[{"entity":"book","property":"color","value":"red"}]}
C draws on paper with a pen. -> {"actions":[{"actor":"C","verb":"draws","roles":[{"role":"surface","value":"paper"},{"role":"instrument","value":"pen"}]}]}
The book is wet. -> {"states":[{"entity":"book","dimension":"wetness","value":"wet"}]}
C moves the book from the shelf into the bag with her left hand. -> {"actions":[{"actor":"C","verb":"moves","roles":[{"role":"patient","value":"book"},{"role":"source_location","value":"shelf"},{"role":"destination","value":"bag"},{"role":"body_part","value":"left hand"}]}]}
The book changes from wet to dry. -> {"changes":[{"entity":"book","before":"wet","after":"dry"}]}
The machine produces a basket. -> {"results":[{"entity":"machine","predicate":"produces","value":"basket"}]}
C folds the book then C moves the book. -> {"event_links":[{"left_event":"C folds the book","connector":"then","right_event":"C moves the book","link_type":"before"}]}
The book is on the table. -> {"relations":[{"left":"book","predicate":"on","right":"table","relation_type":"spatial"}]}
Process the real source, not these examples. Return JSON only.'''


def literal_spans(text):
    words=list(re.finditer(r"\b[\w'-]+\b",text))
    return sorted({text[a.start():words[j].end()] for i,a in enumerate(words)
        for j in range(i,min(i+12,len(words))) if words[j].end()-a.start()<=100})


def output_schema(inputs):
    source_text=inputs['unit']['text'];values=literal_spans(source_text)
    text=enum(values)
    attribute_value=enum([v for v in values if v.casefold().split()[0] not in FUNCTION_WORDS
        and v.casefold() not in TEMPORARY_VALUES])
    action=record(dict(actor=deepcopy(text),verb=deepcopy(text),
        roles=array(record(dict(role=enum(ROLES),value=deepcopy(text))),len(ROLES))))
    allowed_links=[]
    for kind,connectors in dict(before=['before','then'],after=['after'],simultaneous=['while','as'],
            cause=['because','causes','caused'],purpose=['in order to','so as to']).items():
        if any(span(c,source_text) for c in connectors):allowed_links.append(kind)
    relation_words=set().union(*RELATION_PREDICATES.values())
    relation_values=[v for v in values if re.sub(r'^(?:is|are|was|were)\s+','',v.casefold()) in relation_words]
    result_values=[v for v in values if v.casefold() in {'becomes','became','produces','produced','yields','yielded','results in'}]
    definitions={
        'actions':(action,4),
        'attributes':(record(dict(entity=text,property=enum(PROPERTIES),value=attribute_value)),6),
        'states':(record(dict(entity=text,dimension=enum(DIMENSIONS),value=text)),4),
        'relations':(record(dict(left=text,predicate=enum(relation_values),right=text,
            relation_type=enum(['spatial','possession','part_whole','attachment','other']))),6 if relation_values else 0),
        'changes':(record(dict(entity=text,before=text,after=text)),2 if re.search(r'\bfrom\b.+\bto\b',source_text,re.I) else 0),
        'results':(record(dict(entity=text,predicate=enum(result_values),value=text)),2 if result_values else 0),
        'event_links':(record(dict(left_event=text,connector=text,right_event=text,link_type=enum(allowed_links))),2 if allowed_links else 0)}
    return record(dict(**{k:array(v,n) for k,(v,n) in definitions.items()},
        uncertain={'type':'boolean'}))


def role_guard(fact,text):
    valid,reason=binding_supported(fact,text)
    if fact['kind']=='action':
        manner=(r'(?:'+re.escape(fact['manner'])+r'\s+)?') if fact.get('manner') else ''
        pattern=r'(?<!\w)'+re.escape(fact['actor'])+r'\s+(?:(?:is|are|was|were|has|have|had)\s+)?'+manner+re.escape(fact['verb'])+r'(?!\w)'
        for match in re.finditer(pattern,text,re.I):
            end=len(text)
            boundary=re.search(r'\b(?:then|and|while|before|after|because)\s+(?='+re.escape(fact['actor'])+r'\s)',text[match.end():],re.I)
            if boundary:end=match.end()+boundary.start()
            passed,local_reason=binding_supported(fact,text[match.start():end])
            if passed:valid,reason=True,None;break
            valid,reason=False,local_reason
    if not valid:return valid,reason
    if fact['kind']=='attribute' and (fact['value'].casefold().split()[0] in FUNCTION_WORDS
            or fact['value'].casefold() in TEMPORARY_VALUES):
        return False,'function_word_is_not_attribute'
    if fact['kind']=='relation':
        left=span(fact['left'],text);predicate=span(fact['predicate'],text)
        if not re.fullmatch(r'\s*(?:(?:is|are|was|were)\s*)?',text[left[1]:predicate[0]],re.I):
            return False,'relation_left_not_attached_to_predicate'
    if fact['kind']=='action':
        if fact.get('patient','').casefold() in {'around','away','back','up','down','out','off','here','there','along'}:
            return False,'particle_is_not_patient'
        if re.search(r'\b(hands?|fingers?|arms?|feet|foot|legs?)\b',fact.get('instrument',''),re.I):
            return False,'body_part_is_not_instrument'
        material=fact.get('material')
        if material and any(material.casefold()==fact.get(k,'').casefold() for k in ['source_location','destination','surface','instrument']):
            return False,'material_conflicts_with_another_role'
        if material and fact.get('instrument') and material.casefold() in fact['instrument'].casefold():
            return False,'material_is_part_of_tool'
    return True,None


def accept(unit,report):
    text=clean_unit(unit)['text'];validate_output(report,output_schema(dict(unit=dict(text=text))))
    facts=[];rejected=[]
    action_words={a['verb'].casefold() for a in report['actions']}
    for kind,category in COLLECTIONS.items():
        for candidate in report[category]:
            if kind=='action':
                roles={r['role']:r['value'] for r in candidate['roles']}
                if len(roles)!=len(candidate['roles']):
                    rejected.append(dict(record=dict(kind=kind,**candidate),reason='duplicate_action_roles'));continue
                candidate=dict(actor=candidate['actor'],verb=candidate['verb'],**roles)
            fact=dict(kind=kind,source_id=unit['id'],**candidate)
            valid,reason=role_guard(fact,text)
            if kind in ['attribute','state'] and fact['value'].casefold() in action_words:
                valid,reason=False,'action_word_is_not_entity_property'
            if valid:facts.append(fact)
            else:rejected.append(dict(record=fact,reason=reason))
    mentions={}
    for fact in facts:
        for role,label in fact.items():
            if role not in ENTITY_ROLES:continue
            words=list(re.finditer(r"\b[\w'-]+\b",label))
            heads=[w for w in words if w.group().casefold() not in {'a','an','the','his','her','their','its'}]
            for index,w in enumerate(heads):
                if w.group().casefold() in {'of','in','on','with','at','from','to'}:
                    heads=heads[:index];break
            head=heads[-1].group() if heads else label
            kind='person' if label=='C' or head.casefold() in {'man','woman','person','boy','girl','child'} else 'body_part' if role=='body_part' else 'object'
            mentions[label]=dict(label=label,head=head,entity_type=kind)
    return dict(facts=facts,mentions=list(mentions.values()),rejected=rejected,
        uncertain=bool(report['uncertain'] or NEGATION.search(text) or UNCERTAINTY.search(text)))


def complete_simple_source(unit,accepted):
    """Only remove raw text when an explicit grammar covers the entire clause."""
    text=clean_unit(unit)['text'].strip().rstrip('.!?')
    facts=accepted['facts']
    if accepted['uncertain'] or accepted['rejected'] or not facts:return False
    actions=[f for f in facts if f['kind']=='action']
    if len(actions)==1 and all(f['kind'] in ['action','attribute','state','relation'] for f in facts):
        f=actions[0]
        expression=re.escape(f['actor'])+r'\s+(?:(?:is|are|was|were)\s+)?'
        if 'manner' in f:return False
        expression+=re.escape(f['verb'])
        if 'patient' in f:expression+=r'\s+(?:(?:a|an|the|his|her|their|its|my|your)\s+)?'+re.escape(f['patient'])
        adjuncts=[]
        for role,connectors in PREPOSITIONS.items():
            if role in f:
                occurrence=span(f[role],text)
                adjuncts.append((occurrence[0],r'\s+(?:'+'|'.join(re.escape(c) for c in connectors)+
                    r')\s+(?:(?:a|an|the|his|her|their|its|my|your|both)\s+)?'+re.escape(f[role])))
        expression+=''.join(pattern for _,pattern in sorted(adjuncts))
        return re.fullmatch(expression,text,re.I) is not None
    if len(facts)==1 and facts[0]['kind'] in ['attribute','state']:
        f=facts[0]
        return re.fullmatch(re.escape(f['entity'])+r'\s+(?:is|are|was|were)\s+'+re.escape(f['value']),text,re.I) is not None
    return False


def head_key(label,head=None):
    words=re.findall(r"[A-Za-z0-9]+",label.casefold())
    words=[w for w in words if w not in {'a','an','the','his','her','their','its','my','your'}]
    word=(re.findall(r'[A-Za-z0-9]+',head.casefold())[-1] if head else (words[-1] if words else label.casefold()))
    return PLURALS.get(word,word)


def qualifiers(label):
    words=set(re.findall(r'[a-z0-9]+',label.casefold()));result={}
    for name,allowed in [('color',COLORS),('side',{'left','right'}),
                         ('material',{'wooden','metal','plastic','steel','iron','glass','paper'})]:
        values=words&allowed
        if len(values)==1:result[name]=next(iter(values)).replace('grey','gray')
    result['plural']=any(w in PLURALS for w in words)
    return result


def modifiers(label,head):
    return set(re.findall(r'[a-z0-9]+',label.casefold()))-{head,'a','an','the','his','her','their','its','my','your'}


class Resolver:
    def __init__(self,max_gap=8):
        self.entities=[];self.max_gap=max_gap;self.decisions=[]

    def resolve(self,label,time,mention=None):
        head=head_key(label,mention.get('head') if mention else None)
        features=qualifiers(label);kind=mention.get('entity_type','unknown') if mention else 'unknown'
        pronoun=label.casefold() in PRONOUNS
        if label=='C':kind='person';head='c'
        candidates=[]
        for entity in self.entities:
            if label!='C' and time-entity['last_observed_end']>self.max_gap:continue
            if pronoun:
                if label.casefold() not in {'it','its','this','that'} or entity['entity_type'] in ['person','body_part','unknown']:continue
                if time-entity['last_observed_end']>2:continue
            elif entity['head']!=head:continue
            if kind!='unknown' and entity['entity_type'] not in ['unknown',kind]:continue
            if any(k in entity['qualifiers'] and entity['qualifiers'][k]!=v for k,v in features.items() if not pronoun):continue
            if not pronoun:
                before=modifiers(entity['label'],head);after=modifiers(label,head)
                if before and after and not (before<=after or after<=before):continue
            candidates.append(entity)
        exact=[e for e in candidates if label in e['aliases']]
        possible=candidates if not modifiers(label,head) or pronoun else (exact if exact else candidates)
        if len(possible)==1:
            entity=possible[0];basis='local_pronoun_inference' if pronoun else ('same_local_label' if exact else 'compatible_local_noun_head')
        else:
            entity=dict(id=f'E{len(self.entities)}',label=label,head=head,aliases=[],entity_type=kind,
                qualifiers=features,first_observed_at=time,last_observed_end=time)
            self.entities.append(entity);basis='ambiguous_new_entity' if possible else 'new_local_entity'
        if label not in entity['aliases']:entity['aliases'].append(label)
        entity['last_observed_end']=max(entity['last_observed_end'],time)
        if entity['entity_type']=='unknown' and kind!='unknown':entity['entity_type']=kind
        self.decisions.append(dict(label=label,entity_id=entity['id'],basis=basis,time=time))
        return entity['id']


def materialize(observations,source_results,max_identity_gap=8,max_state_gap=2):
    resolver=Resolver(max_identity_gap);facts=[];lookup={};events=[];samples=[];residuals=[];tracks={};state_segments=[]
    previous_time=None;seen=set()
    for row in observations:
        source=source_results[row['source_id']];accepted=source['accepted'];time=row['sample_interval'][0]
        if row['caption_id'] in seen or (previous_time is not None and time<previous_time):
            raise ValueError('Duplicate or unsorted E observations')
        seen.add(row['caption_id']);previous_time=time
        mentions={m['label']:m for m in accepted['mentions']};bindings={};ids=[]
        for fact in accepted['facts']:
            for role,label in fact.items():
                if role in ENTITY_ROLES and label not in bindings:
                    bindings[label]=resolver.resolve(label,time,mentions.get(label))
        current={}
        for raw in accepted['facts']:
            fields={k:v for k,v in raw.items() if k not in ['kind','source_id']}
            refs={k:bindings[v] for k,v in fields.items() if k in ENTITY_ROLES}
            canonical={k:(refs[k] if k in refs else v.casefold() if k in ['predicate','value'] else v) for k,v in fields.items()}
            if raw['kind']=='relation':canonical['predicate']=re.sub(r'^(?:is|are|was|were)\s+','',canonical['predicate'])
            key=json.dumps([raw['kind'],canonical],sort_keys=True,ensure_ascii=False)
            if key not in lookup:
                lookup[key]=len(facts);facts.append(dict(id=f'F{len(facts)}',kind=raw['kind'],fields=fields,
                    entity_refs=refs,source_ids=[],observation_indices=[]))
            f=facts[lookup[key]];ids.append(f['id'])
            if row['source_id'] not in f['source_ids']:f['source_ids'].append(row['source_id'])
            f['observation_indices'].append(len(samples))
            if raw['kind']=='action':
                events.append(dict(id=f'A{len(events)}',fact_id=f['id'],observation_index=len(samples),
                    anchor_entity=refs.get('patient',refs.get('surface',refs.get('actor'))),entity_refs=refs))
            if raw['kind'] in ['attribute','state','relation']:
                entity=refs.get('entity',refs.get('left'))
                field=raw.get('property',raw.get('dimension',raw.get('relation_type','')+':'+canonical.get('predicate','')))
                current.setdefault((raw['kind'],entity,field),set()).add(f['id'])
        samples.append(dict(caption_id=row['caption_id'],frame_index=row['frame_index'],source_id=row['source_id'],
            sample_interval=deepcopy(row['sample_interval']),fact_ids=ids))
        for family,values in current.items():
            previous=tracks.get(family)
            if previous is not None:
                s=state_segments[previous]
                gap=time-s['interval'][1]
                if s['fact_id'] in values and len(values)==1 and 0<=gap<=max_state_gap:
                    s['interval'][1]=row['sample_interval'][1];s['observation_indices'].append(len(samples)-1)
                    s['continuity']='inferred';continue
            for fid in sorted(values):
                state_segments.append(dict(id=f'T{len(state_segments)}',fact_id=fid,entity_id=family[1],
                    interval=deepcopy(row['sample_interval']),observation_indices=[len(samples)-1],continuity='single_sample'))
            if len(values)==1:tracks[family]=len(state_segments)-1
            else:tracks.pop(family,None)
        # A mentioned entity without the prior condition is a boundary for its
        # temporary states/relations; stable attributes remain independently dated.
        touched=set(bindings.values())
        for family in list(tracks):
            if family not in current and family[0]!='attribute' and family[1] in touched:tracks.pop(family)
        if not source['complete_simple']:
            residuals.append(dict(observation_index=len(samples)-1,text=row['original_text']))
    memory=dict(schema=VERSION,entities=resolver.entities,facts=facts,observations=samples,
        action_observations=events,state_segments=state_segments,residuals=residuals,
        identity_decisions=resolver.decisions,max_identity_gap=max_identity_gap,max_state_gap=max_state_gap,
        policy=dict(identity_is_text_inference=True,continuous_states_are_inferred=True,event_counts_inferred=False))
    audit=validate_memory(memory,observations)
    return memory,audit


def validate_memory(memory,source_observations=None):
    entities={e['id'] for e in memory['entities']};facts={f['id']:f for f in memory['facts']}
    if len(facts)!=len(memory['facts']):raise ValueError('Duplicate E facts')
    for f in facts.values():
        if not set(f['entity_refs'].values())<=entities:raise ValueError('Unknown E entity reference')
    residual={r['observation_index'] for r in memory['residuals']}
    for i,row in enumerate(memory['observations']):
        if not row['fact_ids'] and i not in residual:raise ValueError('E dropped a source observation')
        if not set(row['fact_ids'])<=set(facts):raise ValueError('Unknown E fact reference')
    if source_observations is not None:
        if len(memory['observations'])!=len(source_observations):raise ValueError('E observation count changed')
        for old,new in zip(source_observations,memory['observations']):
            for key in ['caption_id','frame_index','source_id','sample_interval']:
                if old[key]!=new[key]:raise ValueError('E source provenance changed')
    return dict(source_rows=len(memory['observations']),entities=len(entities),unique_facts=len(facts),
        alias_links=sum(d['basis'] in ['compatible_local_noun_head','local_pronoun_inference'] for d in memory['identity_decisions']),
        ambiguous_mentions=sum(d['basis']=='ambiguous_new_entity' for d in memory['identity_decisions']),
        merged_state_segments=sum(len(s['observation_indices'])>1 for s in memory['state_segments']),
        original_clauses_retained=len(memory['residuals']),original_clauses_replaced=len(memory['observations'])-len(memory['residuals']))


def bounds(interval):return '['+','.join(json.dumps(t) for t in interval)+')'


def render(memory):
    validate_memory(memory)
    facts={f['id']:f for f in memory['facts']};entities={e['id']:e for e in memory['entities']}
    sections=defaultdict(list);covered=set();source_map=[]
    def name(fid,role):
        f=facts[fid];ref=f['entity_refs'].get(role)
        return f'{ref}({entities[ref]["label"]})' if ref else f['fields'].get(role,'')
    for segment in memory['state_segments']:
        f=facts[segment['fact_id']];fields=f['fields'];entity=segment['entity_id']
        if f['kind']=='relation':body=fields['predicate']+' '+name(f['id'],'right')
        else:body=fields.get('property',fields.get('dimension'))+'='+fields['value']
        sections[entity].append((segment['interval'][0],bounds(segment['interval'])+': '+body))
        for i in segment['observation_indices']:covered.add((i,f['id']))
    grouped={}
    for event in memory['action_observations']:
        grouped.setdefault((event['anchor_entity'],event['fact_id']),[]).append(event['observation_index'])
    for (anchor,fid),indices in grouped.items():
        f=facts[fid];fields=f['fields']
        times=', '.join(bounds(memory['observations'][i]['sample_interval']) for i in indices)
        body=name(fid,'actor')+' '+fields['verb']
        if 'patient' in fields and f['entity_refs'].get('patient')!=anchor:body+=' '+name(fid,'patient')
        roles=[role+'='+name(fid,role) for role in ROLES if role in fields and role!='patient']
        if roles:body+='; '+'; '.join(roles)
        sections[anchor].append((memory['observations'][indices[0]]['sample_interval'][0],times+': '+body))
        covered.update((i,fid) for i in indices)
    for i,row in enumerate(memory['observations']):
        for fid in row['fact_ids']:
            if (i,fid) in covered:continue
            f=facts[fid];anchor=f['entity_refs'].get('entity',f['entity_refs'].get('left'))
            body=f['kind']+': '+'; '.join(k+'='+name(fid,k) if k in f['entity_refs'] else k+'='+v for k,v in f['fields'].items())
            sections[anchor].append((row['sample_interval'][0],bounds(row['sample_interval'])+': '+body));covered.add((i,fid))
    lines=[]
    for entity,entries in sections.items():
        if entity:
            obj=entities[entity];aliases=[a for a in obj['aliases'] if a!=obj['label']]
            lines.append(entity+': '+obj['label']+(' (also '+', '.join(aliases)+')' if aliases else ''))
        for _,body in sorted(entries,key=lambda x:x[0]):lines.append('  '+body if entity else body)
    if memory['residuals']:
        lines.append('Original observations:')
        for residual in memory['residuals']:
            row=memory['observations'][residual['observation_index']]
            lines.append('Frame '+bounds(row['sample_interval'])+': '+residual['text'])
    expected={(i,fid) for i,row in enumerate(memory['observations']) for fid in row['fact_ids']}
    if covered!=expected:raise ValueError('E rendered view dropped a fact observation')
    return '\n'.join(lines),dict(rendered_fact_observations=len(covered),source_rows=len(memory['observations']),all_fact_observations_rendered=True)
