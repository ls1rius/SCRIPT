"""CPU-only evidence projection, provenance checks and controlled-study reports."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

ARMS = ('A_original', 'B_lossless_layout', 'C_semantic')
COLLECTIONS = dict(action='actions', attribute='attributes', state='states', relation='relations',
                   change='changes', result='results', event_link='event_links')
ORDERS = (ARMS, (ARMS[0],ARMS[2],ARMS[1]), (ARMS[1],ARMS[0],ARMS[2]),
          (ARMS[1],ARMS[2],ARMS[0]), (ARMS[2],ARMS[0],ARMS[1]), tuple(reversed(ARMS)))
START = '  Description: '
END = '\n\n###\n\n Questions:'


def now(): return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()
def read(p): return json.loads(Path(p).read_text())
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def digest(x): return hashlib.sha256(json.dumps(x,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def write(p, value):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+'.'+str(os.getpid())+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)


def parts(inputs):
    if len(inputs['prompts'])!=1: raise ValueError('Expected one original prompt')
    s=inputs['prompts'][0]
    if s.count(START)!=1 or s.count(END)!=1:raise ValueError('Ambiguous Description boundary')
    a=s.index(START)+len(START);b=s.index(END,a)
    return s[:a],s[a:b],s[b:]


def fixed(inputs):
    a,_,b=parts(inputs)
    return {**{k:v for k,v in inputs.items() if k!='prompts'},'prefix':a,'suffix':b}


def quarantine_reason(kind,fact,text):
    """Reject known unsupported roles, without silently reassigning them."""
    if kind!='action':return None
    if str(fact.get('patient','')).lower().strip() in {
            'around','away','back','up','down','out','off','here','there','along'}:
        return 'adverb_or_particle_is_not_patient'
    if re.search(r'\b(hands?|fingers?|thumbs?|arms?|feet|foot|legs?|elbows?|knees?)\b',
                 str(fact.get('instrument','')),re.I):
        return 'body_part_is_not_instrument'
    material=fact.get('material')
    if material:
        if any(material.casefold()==str(fact.get(k,'')).casefold()
               for k in ('source_location','destination','surface','instrument')):
            return 'material_conflicts_with_another_role'
        tool=fact.get('instrument')
        if tool:
            m=re.search(r'(?<!\w)'+re.escape(material)+r'(?!\w)',text,re.I)
            t=re.search(r'(?<!\w)'+re.escape(tool)+r'(?!\w)',text,re.I)
            if m and t and t.start()<=m.start()<m.end()<=t.end():
                return 'material_is_only_part_of_tool_name'
    if fact.get('surface') and str(fact['verb']).lower() in ('watch','watches','watching'):
        return 'display_medium_is_not_verified_physical_surface'
    return None


def projected_evidence(memory,source):
    uid=source['id'];evidence={};rejected=[]
    for kind,collection in COLLECTIONS.items():
        accepted=[]
        for fact in memory[collection]:
            if fact['source_id']!=uid:continue
            reason=quarantine_reason(kind,fact,source['text'])
            if reason:rejected.append(dict(collection=collection,fact=deepcopy(fact),reason=reason))
            else:accepted.append({k:deepcopy(v) for k,v in fact.items() if k not in ('id','source_id','kind')})
        if accepted:evidence[collection]=accepted
    residual=next((r for r in memory['residuals'] if r['source_id']==uid),None)
    if residual or rejected or not evidence:
        tags=set(residual['information_types'] if residual else [])
        if not evidence:tags.add('unclassified')
        tags.update(k for k,c in COLLECTIONS.items() if c in evidence)
        if rejected:tags.add('unclassified')
        evidence['categorized_original']=dict(original_text=source.get('raw_text',source['text']),
                                               information_types=sorted(tags))
    return evidence,rejected


def semantic_input(original,lossless,memory,bundle):
    """Keep B's exact source rows and A's prompt; substitute evidence only."""
    if fixed(original)!=fixed(lossless):raise ValueError('A/B fixed regions differ')
    sources={u['id']:u for u in memory['sources']}
    if set(sources)!={u['id'] for u in bundle['units']}:raise ValueError('Memory source IDs changed')
    for collection in COLLECTIONS.values():
        if any(f['source_id'] not in sources for f in memory[collection]):
            raise ValueError('Fact cites a foreign source')
    for u in bundle['units']:
        saved=sources[u['id']]
        if saved.get('raw_text',saved['text']).strip()!=u['text'].strip():
            raise ValueError('Memory original text changed')
        for key in ('source_type','status','source_caption_ids','source_frame_ids','source_intervals'):
            if saved[key]!=u[key]:raise ValueError('Memory provenance changed: '+key)
    by_caption={}
    for source in sources.values():
        for cid in source['source_caption_ids']:
            if cid in by_caption:raise ValueError('Ambiguous caption-to-source mapping')
            by_caption[cid]=source
    payload=json.loads(parts(lossless)[1]);rows=payload['observations']
    if {r['caption_id'] for r in rows}!=set(bundle['read_caption_ids']):raise ValueError('Read evidence scope changed')
    output=deepcopy(payload);rejected=[];fact_counts=Counter();retained=0
    for old,new in zip(rows,output['observations']):
        source=by_caption[old['caption_id']]
        if old['sample_interval'] not in source['source_intervals']:raise ValueError('Sample time changed')
        if old['evidence']['original_text'].strip()!=source.get('raw_text',source['text']).strip():
            raise ValueError('Original caption text differs')
        evidence,bad=projected_evidence(memory,source)
        if 'categorized_original' in evidence:
            # Preserve even original whitespace and source markers in the fallback.
            evidence['categorized_original']['original_text']=old['evidence']['original_text']
            retained+=1
        new['evidence']=evidence
        for kind,collection in COLLECTIONS.items():fact_counts[kind]+=len(evidence.get(collection,[]))
        rejected.extend(dict(caption_id=old['caption_id'],**r) for r in bad)
        if {k:v for k,v in old.items() if k!='evidence'}!={k:v for k,v in new.items() if k!='evidence'}:
            raise ValueError('Non-evidence row changed')
    a,_,b=parts(original);result=deepcopy(original)
    result['prompts'][0]=a+json.dumps(output,ensure_ascii=False,separators=(',',':'))+b
    if fixed(result)!=fixed(original):raise ValueError('Original protocol changed')
    return result,dict(read_captions=len(rows),covered_captions=len(output['observations']),
        retained_original_rows=retained,facts_by_type=dict(fact_counts),quarantined_records=rejected,
        identity_merging=False,caption_rows_deduplicated=False)


def strict_audit(text,options):
    clean=text.replace('**','').replace('`','').strip()
    letters=set(re.findall(r'(?im)^\s*prediction\s*:\s*([A-E])\s*[.]?\s*$',clean))
    if re.fullmatch(r'[A-E][.]?',clean.split('\n',1)[0]):letters.add(clean[0])
    if len(letters)==1:return dict(option=str(ord(next(iter(letters)))-65),method='strict_letter')
    values=re.findall(r'(?im)^\s*prediction\s*:\s*(.*?)\s*$',clean)
    canonical=lambda s:re.sub(r'\s+',' ',s.replace('**','').replace('`','')).strip().casefold().rstrip('.!?')
    matches=[str(i) for i,v in enumerate(options) if len(values)==1 and canonical(v)==canonical(values[0])]
    return dict(option=matches[0] if len(matches)==1 else None,
                method='unique_exact_option_text' if len(matches)==1 else 'unresolved')


def report(out):
    ids=read(out/'cohort.json');truth=read(out/'evaluation_only.json');old=read(out/'historical_only.json')
    rows={a:{u:read(p) for u in ids if (p:=out/'runs'/a/u/'result.json').exists()} for a in ARMS}
    correct=lambda a,u:not rows[a][u].get('error') and str(rows[a][u]['selected_option'])==str(truth[u])
    common=[u for u in ids if all(u in rows[a] for a in ARMS)]
    stats={a:dict(completed=len(rs),planned=len(ids),pending=len(ids)-len(rs),
        correct=sum(correct(a,u) for u in rs),errors=sum(bool(r.get('error')) for r in rs.values()),
        accuracy=sum(correct(a,u) for u in rs)/len(ids) if len(rs)==len(ids) else None,
        accuracy_among_completed=sum(correct(a,u) for u in rs)/len(rs) if rs else None,
        mean_qa_seconds=sum(r.get('seconds',0) for r in rs.values())/len(rs) if rs else None,
        mean_input_tokens=sum(r.get('input_tokens',0) or 0 for r in rs.values())/len(rs) if rs else None,
        strict_audit_correct=sum(not r.get('error') and str(r.get('strict_audit',{}).get('option'))==str(truth[u]) for u,r in rs.items())) for a,rs in rows.items()}
    comparisons={}
    for control,treatment in ((ARMS[0],ARMS[2]),(ARMS[0],ARMS[1]),(ARMS[1],ARMS[2])):
        paired=[u for u in ids if u in rows[control] and u in rows[treatment]]
        fixed_ids=[u for u in paired if correct(treatment,u) and not correct(control,u)]
        regressed=[u for u in paired if correct(control,u) and not correct(treatment,u)]
        n=len(fixed_ids)+len(regressed)
        p=min(1.,2*sum(math.comb(n,k) for k in range(min(len(fixed_ids),len(regressed))+1))/2**n) if n else 1.
        comparisons[treatment+'_vs_'+control]=dict(completed=len(paired),corrected=fixed_ids,regressed=regressed,
            delta_percentage_points=100*(len(fixed_ids)-len(regressed))/len(paired) if paired else None,mcnemar_exact_p=p)
    reused=sum(read(p).get('measured_seconds',0) for p in (out/'materialized').glob('*/reuse.json'))
    functional=read(out/'validation/functional_reuse.json') if (out/'validation/functional_reuse.json').exists() else {}
    inherited_other=functional.get('measured_seconds',0)
    experiment=read(out/'experiment.json')
    inherited_other+=sum(experiment.get('reused_setup_seconds_by_source',{}).values())
    measured={}
    for stage in ('setup','functional','materialized','runs'):
        measured[stage]=sum(float(json.loads(line).get('actual_cost') or 0)
            for f in (out/stage).rglob('calls.jsonl') if not (f.parent/'reuse.json').exists()
            for line in f.read_text().splitlines())
    result=dict(updated_at=now(),planned=len(ids),complete=all(len(rows[a])==len(ids) for a in ARMS),
        common_completed=len(common),arms=stats,comparisons=comparisons,
        paired_common={a:dict(correct=sum(correct(a,u) for u in common),completed=len(common)) for a in ARMS},
        baseline_replication_mismatches=[u for u,r in rows[ARMS[0]].items() if r.get('error') or str(r['selected_option'])!=str(old[u]['selected_option'])],
        measured_seconds_by_stage=measured,reused_normalization_seconds=reused,
        reused_setup_and_functional_seconds=inherited_other,
        prepare_cpu_seconds=experiment.get('prepare_cpu_seconds',0),
        measured_seconds_total=sum(measured.values())+reused+inherited_other+experiment.get('prepare_cpu_seconds',0),
        primary='C_semantic versus A_original; original parser; errors counted wrong',development_cohort=True)
    write(out/'report.json',result);return result
