"""H: F-style on-demand storage with G open states and optional unknown actors.

The single states field caches all explicit source conditions, never a fixed
dimension or a question-conditioned subset. Observations preserve time/source.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from . import generalized_entity_memory as joint
from . import lazy_script_memory as parent
from .schemas import array, enum, record, validate_output

VERSION='generalized-lazy-memory-H-v1'
PLAN='h_plan_fields'
EXTRACT='h_extract_fields'
RELATIONS=('spatial','possession','part_whole','attachment','other')
FIELDS=('actions.base',)+tuple('actions.'+v for v in joint.ROLES if v!='patient')+tuple(
    'attributes.'+v for v in joint.PROPERTIES)+('states',)+tuple(
    'relations.'+v for v in RELATIONS)+('changes','results')+tuple('event_links.'+v for v in joint.LINKS)
PLAN_PROMPT = parent.PLAN_PROMPT.replace(
    'Temporary wet/dry/open etc are states, not attributes.',
    'For any explicit temporary condition request states, which stores OPEN entity/description records. '
    'There are no states.wetness, states.integrity or other dimension fields. '
    'Request states as a whole source category; do not supply state values or an entity filter.')
EXTRACT_PROMPT = parent.EXTRACT_PROMPT.replace(
    'Actions always include actor, verb and an explicitly present patient in roles.',
    'Actions include verb, an explicitly stated actor, and an explicitly present patient in roles. '
    'Use actor=null only when no executing actor is stated, such as an agentless passive or imperative. '
    'Never omit an explicit subject of an active action or invent an unknown actor.').replace(
    'All string values must copy literal spans from unit.text.',
    'All non-null string values must copy literal spans from unit.text.').replace(
    'Attribute properties and state dimensions must be requested. Stable material/color/shape differ from wet/dry/open/closed temporary states.',
    'Attribute properties must be requested. If states is requested, extract ALL explicit conditions in this source '
    'as {entity, description}, retaining condition qualifiers. Descriptions are OPEN source text, not a vocabulary. '
    'Never emit dimension/value for states. Stable material/color/shape differ from temporary conditions.').replace(
    'The book is wet, requested states.wetness -> states=[{"entity":"book","dimension":"wetness","value":"wet"}].',
    'The server is unreachable, requested states -> states=[{"entity":"server","description":"unreachable"}].')
EXTRACT_PROMPT += '''
When states is requested, do not restrict conditions to ones relevant to an imagined question.
Explicit relations must be represented when their relation field is requested; a state description does not replace a relation.
The box was moved with a crane, requested actions.instrument -> actions=[{"actor":null,"verb":"moved","roles":[{"role":"patient","value":"box"},{"role":"instrument","value":"crane"}]}].
'''



def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def output_schema(operation,inputs):
    if operation==PLAN:
        values=array(enum(FIELDS),11);values['minItems']=1
        return record(dict(fields=values))
    if operation != EXTRACT:
        raise ValueError('Unknown H operation')
    if set(inputs) != {'unit', 'requested_fields', 'source_type'} or inputs['source_type'] != 'caption':
        raise ValueError('H extraction requires source-only inputs')
    if set(inputs['unit']) != {'id', 'text'} or not inputs['unit']['text'].strip():
        raise ValueError('H requires one nonempty literal source')
    requested=set(inputs['requested_fields'])
    if not requested or not requested<=set(FIELDS):raise ValueError('Invalid H field request')
    schema=joint.output_schema(inputs)
    for category,spec in schema['properties'].items():
        if category=='uncertain':continue
        fields=[f for f in requested if f==category or f.startswith(category+'.')]
        if not fields:spec['maxItems']=0;continue
        props=spec['items']['properties']
        if category=='actions':
            allowed=['patient']+[f.split('.',1)[1] for f in fields if f!='actions.base']
            props['roles']['items']['properties']['role']=enum(allowed)
            props['roles']['maxItems']=len(allowed)
        elif category in ['attributes','relations','event_links']:
            key=dict(attributes='property',relations='relation_type',event_links='link_type')[category]
            # Intersect source-supported links with the request.
            allowed={f.split('.',1)[1] for f in fields}&set(props[key]['enum'])
            props[key]=enum(sorted(allowed))
            if not allowed:spec['maxItems']=0
    return schema


def project_fact(fact,field):
    category,_,name=field.partition('.')
    if joint.COLLECTIONS[fact['kind']]!=category:return None
    if category=='actions':
        base={k:v for k,v in fact.items() if k in ['kind','source_id','actor','verb','patient']}
        if name=='base':return base
        if name in fact:return dict(base,**{name:fact[name]})
        return None
    key=dict(attributes='property',relations='relation_type',event_links='link_type').get(category)
    return deepcopy(fact) if key is None or fact[key]==name else None


def combine(facts):
    """Join a role-only fragment only to an unambiguous, source-local action core."""
    actions={};others={}
    known={}
    for fact in facts:
        if fact['kind']=='action' and fact.get('patient'):
            key=tuple(fact.get(k) for k in ['source_id','actor','verb'])
            known.setdefault(key,set()).add(fact['patient'])
    for raw in facts:
        fact=deepcopy(raw)
        if fact['kind']!='action':others[digest(fact)]=fact;continue
        identity=tuple(fact.get(k) for k in ['source_id','actor','verb'])
        candidates=known.get(identity,set())
        if not fact.get('patient') and len(candidates)==1:fact['patient']=next(iter(candidates))
        key=identity+(fact.get('patient'),)
        current=actions.setdefault(key,deepcopy(fact))
        if any(k in current and current[k]!=v for k,v in fact.items()):others[digest(fact)]=deepcopy(fact)
        else:current.update(fact)
    return list(actions.values())+list(others.values())


def check_database(path):
    """Reject F or other databases before opening them for writes."""
    path = Path(path)
    if not path.exists():
        return
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return
        if 'memory_metadata' not in tables:
            raise ValueError('H requires a separate database; legacy F database cannot be reused')
        variant = connection.execute("SELECT value FROM memory_metadata WHERE key='variant'").fetchone()
        if variant != ('H',):
            raise ValueError('Database does not belong to H')


class ScriptDB:
    def __init__(self,path,extractor_version=VERSION):
        if not (extractor_version == VERSION or extractor_version.startswith(VERSION+':')):
            raise ValueError('H requires its own extractor version namespace')
        check_database(path)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.path=Path(path);self.version=extractor_version
        self.db=sqlite3.connect(path);self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS memory_metadata(key TEXT PRIMARY KEY, value TEXT);
        INSERT OR IGNORE INTO memory_metadata VALUES ('variant','H');
        CREATE TABLE IF NOT EXISTS sources(video TEXT, source_key TEXT, content_hash TEXT, payload TEXT,
          PRIMARY KEY(video,source_key));
        CREATE TABLE IF NOT EXISTS observations(video TEXT, caption TEXT, source_key TEXT, start REAL, end REAL,
          payload TEXT, PRIMARY KEY(video,caption));
        CREATE INDEX IF NOT EXISTS observation_time ON observations(video,start,end);
        CREATE TABLE IF NOT EXISTS field_cache(video TEXT, source_key TEXT, version TEXT, field TEXT,
          status TEXT CHECK(status IN ('value','checked_absent','uncertain','failed')), payload TEXT, updated REAL,
          PRIMARY KEY(video,source_key,version,field));
        CREATE TABLE IF NOT EXISTS field_history(id INTEGER PRIMARY KEY, video TEXT, source_key TEXT, version TEXT,
          field TEXT, status TEXT, payload TEXT, recorded REAL);
        CREATE TABLE IF NOT EXISTS plans(video TEXT, task_hash TEXT, version TEXT, payload TEXT,
          PRIMARY KEY(video,task_hash,version));
        CREATE TABLE IF NOT EXISTS views(video TEXT, task_hash TEXT, version TEXT, payload TEXT,
          PRIMARY KEY(video,task_hash,version));
        ''');self.db.commit()

    def close(self):self.db.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

    def register(self,video,units,rows):
        mapping={}
        for unit in units:
            # Extraction is source-text-only. Identical literal text shares fields;
            # its separate observed occurrences remain in observations.
            key=digest(unit['text'])
            mapping[unit['id']]=key
            payload=json.dumps(unit,ensure_ascii=False)
            self.db.execute('INSERT OR IGNORE INTO sources VALUES (?,?,?,?)',(video,key,digest(unit['text']),payload))
        for row in rows:
            key=mapping[row['source_id']];saved=self.db.execute('SELECT source_key,payload FROM observations WHERE video=? AND caption=?',
                (video,row['caption_id'])).fetchone()
            if saved and (saved[0]!=key or json.loads(saved[1])!=row):raise ValueError('Source occurrence changed; new video revision required')
            self.db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)',
                (video,row['caption_id'],key,*row['sample_interval'],json.dumps(row,ensure_ascii=False)))
        self.db.commit();return mapping

    def get(self,video,key,field):
        row=self.db.execute('SELECT status,payload FROM field_cache WHERE video=? AND source_key=? AND version=? AND field=?',
            (video,key,self.version,field)).fetchone()
        return dict(status=row[0],**json.loads(row[1])) if row else None

    def put(self,video,key,field,status,facts,error=None):
        if field not in FIELDS:raise ValueError('Unknown H field')
        if any(project_fact(fact,field) != fact for fact in facts):
            raise ValueError('H cache entry contains an unrequested field')
        for fact in facts:
            if fact['kind']=='state' and set(fact) != {'kind','source_id','entity','description'}:
                raise ValueError('H states require entity/description without dimension/value')
            if fact['kind']=='action' and 'actor' in fact and fact['actor'] is None:
                raise ValueError('Unknown H actors must be omitted in storage')
        payload=json.dumps(dict(facts=facts,error=error),ensure_ascii=False);now=time.time()
        self.db.execute('INSERT INTO field_history(video,source_key,version,field,status,payload,recorded) VALUES (?,?,?,?,?,?,?)',
            (video,key,self.version,field,status,payload,now))
        self.db.execute('INSERT OR REPLACE INTO field_cache VALUES (?,?,?,?,?,?,?)',(video,key,self.version,field,status,payload,now))
        self.db.commit()

    def plan(self,video,task,units,meter):
        inputs=dict(question=task['question'],options=task.get('options',[]),
            available_fields=list(FIELDS),source_type='caption')
        scope=[dict(id=u['id'],text=u['text'],intervals=u['source_intervals']) for u in units]
        task_hash=digest(dict(inputs=inputs,scope=scope))
        row=self.db.execute('SELECT payload FROM plans WHERE video=? AND task_hash=? AND version=?',(video,task_hash,self.version)).fetchone()
        if row:return json.loads(row[0]),True
        data,_=meter.json(PLAN,inputs,('fields',))
        validate_output(data,output_schema(PLAN,inputs))
        requested=sorted(set(data['fields']))
        fields=sorted(set(requested)|{'actions.base'})
        plan=dict(fields=fields,model_requested_fields=requested,
            added_support_fields=sorted(set(fields)-set(requested)),
            source_ids=[u['id'] for u in units],source_selection='unchanged_upstream_task_scope',
            evidence_policy='Requested fields plus complete source clauses whenever projection cannot preserve the whole statement')
        self.db.execute('INSERT INTO plans VALUES (?,?,?,?)',(video,task_hash,self.version,json.dumps(plan)));self.db.commit()
        return plan,False

    def ensure(self,video,units,rows,fields,extract,progress=None):
        if not fields or not set(fields)<=set(FIELDS):raise ValueError('Invalid fields')
        mapping=self.register(video,units,rows);stats=Counter();results={}
        for index,unit in enumerate(units):
            key=mapping[unit['id']];cached={f:self.get(video,key,f) for f in fields}
            missing=[f for f,v in cached.items() if v is None]
            stats['cache_hits']+=len(fields)-len(missing);stats['missing_fields']+=len(missing)
            if progress:progress(index,unit,missing)
            if missing:
                value=extract(unit,missing);stats['source_extractions']+=bool(value.get('model_called',not value.get('guarded_original',False)))
                accepted=value['accepted'];error=value.get('error')
                for field in missing:
                    facts=[p for f in accepted['facts'] if (p:=project_fact(f,field)) is not None]
                    status='failed' if error else 'uncertain' if accepted['uncertain'] or accepted['rejected'] else 'value' if facts else 'checked_absent'
                    self.put(video,key,field,status,facts,error)
                # Alignment core was necessarily extracted along with requested action slots.
                if 'actions.base' not in fields and any(f.startswith('actions.') for f in missing) and self.get(video,key,'actions.base') is None:
                    facts=[p for f in accepted['facts'] if (p:=project_fact(f,'actions.base')) is not None]
                    status='failed' if error else 'uncertain' if accepted['uncertain'] or accepted['rejected'] else 'value' if facts else 'checked_absent'
                    self.put(video,key,'actions.base',status,facts,error)
            cached={f:self.get(video,key,f) for f in fields};facts=[];unknown=False
            for field,entry in cached.items():
                stats['status_'+entry['status']]+=1;unknown|=entry['status'] in ['failed','uncertain']
                # Source IDs belong to a query and can change across queries.
                facts.extend(dict(f,source_id=unit['id']) for f in entry['facts'])
            base=self.get(video,key,'actions.base') if any(f.startswith('actions.') for f in fields) else None
            if base and base['status']=='value':
                for fact in facts:
                    if fact['kind']!='action' or fact.get('patient'):continue
                    candidates={f.get('patient') for f in base['facts'] if f.get('actor')==fact.get('actor') and f.get('verb')==fact.get('verb') and f.get('patient')}
                    if len(candidates)==1:fact['patient']=next(iter(candidates))
            facts=combine(facts)
            # Mentions are a derived index, never another extraction.
            mentions={}
            for fact in facts:
                for role,label in fact.items():
                    if role in joint.ENTITY_ROLES:
                        mentions[label]=dict(label=label,head=joint.base.head_key(label),entity_type='person' if label=='C' else 'body_part' if role=='body_part' else 'object')
            accepted=dict(facts=facts,mentions=list(mentions.values()),rejected=[],uncertain=unknown)
            results[unit['id']]=dict(accepted=accepted,
                complete_simple=joint.complete_simple_source(unit,accepted),role_review=False,error=None)
        return results,dict(stats)

    def view(self,video,task,rows,results,compact_description):
        # Reuse the exact C renderer after projecting only the current task's
        # fields. Absence of one requested field cannot discard observations
        # supporting an inferred goal. Only a whole-clause proof permits omitting
        # literal text, including when all cached field statuses are confident.
        if not rows:raise ValueError('H requires nonempty upstream source observations')
        projected=[];retained=0
        for row in rows:
            source=results[row['source_id']];evidence={}
            for fact in source['accepted']['facts']:
                category=joint.COLLECTIONS[fact['kind']]
                evidence.setdefault(category,[]).append({k:v for k,v in fact.items() if k not in ['kind','source_id']})
            complete=joint.complete_simple_source(dict(text=row['original_text']),source['accepted'])
            if not complete:
                evidence['original_text']=row['original_text'];retained+=1
            projected.append(dict(caption_id=row['caption_id'],frame_index=row['frame_index'],
                sample_interval=deepcopy(row['sample_interval']),evidence=evidence))
        payload=dict(observations=projected)
        text,audit=compact_description(payload)
        audit.update(task_field_projection=True,selected_source_rows=len(rows),visible_source_rows=len(projected))
        audit.update(variant='H',state_representation='entity_description',fixed_state_dimensions=False,
            open_state_facts=sum(len(r['evidence'].get('states',[])) for r in projected),
            actorless_action_facts=sum('actor' not in f for r in projected for f in r['evidence'].get('actions',[])),
            retained_original_rows=retained,source_coverage=1.,
            coverage_policy='Every upstream source remains visible as complete facts or its literal original',
            empty_description=False)
        if not text.strip() or len(projected)!=len(rows):
            raise ValueError('H projection lost upstream source evidence')
        self.db.execute('INSERT OR REPLACE INTO views VALUES (?,?,?,?)',(video,digest(task),self.version,json.dumps(payload,ensure_ascii=False)))
        self.db.commit();return text,payload,audit
