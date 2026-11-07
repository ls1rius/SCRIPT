"""Read-only Q4 retention accounting and provenance-bound upstream type proxy.

Q4 filters whole literal observations, not facts/entities. The seven upstream N
fact types are joined by question-local caption IDs for INITIAL sources only.
Counts below mean facts carried by a kept/skipped source, never field decisions.
Repeated copies of the same fact in one source are counted once. Added Q4
caption reads have no new type extraction and are reported separately.
"""
import argparse
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import sys
import time

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from script.extend_q4_500 import read,write,sha,now

CATEGORIES=('actions','attributes','states','relations','changes','results','event_links')
CN=dict(actions='动作/事件',attributes='实体属性',states='实体状态',relations='实体关系',changes='变化',results='结果',event_links='事件联系')


def probability(num,den):return num/den if den else None


def extract_facts(sources,memory):
    """Exact provenance join, with source-local semantic deduplication."""
    records={r['id']:r['evidence'] for r in memory['records']}
    by_caption={}
    for obs in memory['observations']:
        cid=obs['caption_id']
        if cid in by_caption:raise ValueError('Ambiguous upstream caption-to-record binding')
        by_caption[cid]=records[obs['record_id']]
    output={}
    for source in sources:
        if source.get('part')!=0:raise ValueError('Multipart sources need explicit fact-span alignment; refuse naive type attribution')
        facts=defaultdict(dict)
        if not source['source_caption_ids']:raise ValueError('Cannot annotate a source without caption provenance')
        for cid in source['source_caption_ids']:
            if cid not in by_caption:raise ValueError('Missing original N annotation for '+cid)
            for category in CATEGORIES:
                for fact in by_caption[cid].get(category,[]):
                    key=json.dumps(fact,sort_keys=True,ensure_ascii=False)
                    facts[category][key]=fact
        output[source['id']]={k:list(v.values()) for k,v in facts.items() if v}
    return output


def analyze(root,out,repo=REPO):
    started=time.monotonic();ids=read(root/'cohort.json');gold=read(root/'evaluation_only.json')
    if len(ids)!=300 or not read(root/'report.json')['complete']:raise ValueError('Use the complete frozen original300 Q4 run')
    baseline=repo/'workdirs/evidence-planner-L-I-300-20261102-r1'
    totals=Counter();fact_counts={k:Counter() for k in CATEGORIES};carrier_counts={k:Counter() for k in CATEGORIES}
    conditionals={arm:{k:defaultdict(Counter) for k in CATEGORIES} for arm in ['Q','Qplus']}
    per_case=[];source_rows=[];provenance={};field_requests=Counter();raw_upstream_facts=Counter();final_contributions=Counter();first_decisions=Counter()
    for uid in ids:
        case=root/'cases'/uid;run=root/'runs'/uid;result=read(run/'result.json')
        if not result['complete'] or result['error']:raise ValueError('Incomplete Q4 case')
        original=read(case/'sentences.json');sources=read(run/'sources.json');decisions=read(run/'selection.json');retained=read(run/'retained.json')
        if {s['id'] for s in sources}!=set(decisions) or len(retained)!=sum(d['decision']=='keep' for d in decisions.values()):
            raise ValueError('Source/selection/retained cardinality mismatch')
        initial_ids={s['id'] for s in original};is_correct={arm:str(result['arms'][arm]['answer']['selected_option'])==str(gold[uid]) for arm in ['Q','Qplus']}
        if result['arms']['Q']['retained_evidence_hash']!=result['arms']['Qplus']['retained_evidence_hash']:raise ValueError('Two readers differ in evidence')
        parent=Path(read(case/'source_binding.json')['original_source_audit']['parent']);mp=parent/'partial_temporal/merged_lazy_memory.json'
        typed={}
        if original:
            memory=read(mp);typed=extract_facts(original,memory)
            expected=read(case/'source_binding.json')['original_source_audit']['source_files']['partial_temporal/merged_lazy_memory.json']
            if sha(mp)!=expected:raise ValueError('Upstream annotations changed')
            provenance[str(mp)]=expected
            for obs in memory['observations']:
                for k in CATEGORIES:raw_upstream_facts[k]+=len(next(r['evidence'] for r in memory['records'] if r['id']==obs['record_id']).get(k,[]))
            for k in set(f.split('.')[0] for f in read(parent/'partial_temporal/field_plan.json')['fields']):field_requests[k]+=1
        first=read(run/'stages/0/selection_initial.json');first_decisions.update(d['decision'] for d in first.values())
        last=run/'stages'/str(len(result['retrievals']));contributions={}
        for p in (last/'windows').glob('*.json'):contributions.update(read(p)['verdict'])
        for p in sorted((last/'reconsider').glob('*.json')):contributions.update(read(p)['verdict'])
        if set(contributions)!=set(decisions):raise ValueError('Final contribution labels do not cover final decisions')
        final_contributions.update(contributions.values())
        case_counts={k:Counter() for k in CATEGORIES};initial_counts=Counter();added_counts=Counter()
        for source in sources:
            sid=source['id'];decision=decisions[sid]['decision'];scope='initial' if sid in initial_ids else 'additional'
            totals['all_observations']+=1;totals['all_'+decision]+=1;totals[scope+'_observations']+=1;totals[scope+'_'+decision]+=1
            (initial_counts if scope=='initial' else added_counts)[decision]+=1
            facts=typed.get(sid,{})
            if scope=='initial':totals['initial_with_typed_fact' if facts else 'initial_without_typed_fact']+=1
            source_rows.append(dict(question_id=uid,source_id=sid,scope=scope,decision=decision,contribution=contributions[sid],
                upstream_fact_types=sorted(facts),upstream_fact_counts={k:len(v) for k,v in facts.items()},
                text=source['text'],caption_ids=source['source_caption_ids'],intervals=source['source_intervals']))
            for category,values in facts.items():
                n=len(values);fact_counts[category]['total']+=n;fact_counts[category][decision]+=n
                carrier_counts[category]['total']+=1;carrier_counts[category][decision]+=1
                case_counts[category]['total']+=n;case_counts[category][decision]+=n
        for arm in ['Q','Qplus']:
            for k,c in case_counts.items():
                if not c['total']:continue
                skipped=c['drop']+c['uncertain'];tag='any_skipped' if skipped else 'none_skipped'
                group=conditionals[arm][k][tag];group['questions']+=1;group['correct']+=is_correct[arm]
                group=conditionals[arm][k]['correct' if is_correct[arm] else 'wrong'];group['facts']+=c['total'];group['skipped']+=skipped
        l=read(baseline/'runs'/uid/'L_evidence_planner/result.json');lc=str(l['selected_option'])==str(gold[uid])
        per_case.append(dict(question_id=uid,question=read(case/'question.json'),gold=str(gold[uid]),
            correct=is_correct,L_correct=lc,answers={a:result['arms'][a]['answer']['selected_option'] for a in ['Q','Qplus']},
            initial_observations=len(original),final_observations=len(sources),initial_final_decisions=dict(initial_counts),
            additional_final_decisions=dict(added_counts),types={k:dict(v) for k,v in case_counts.items()},stop_reason=result['stop_reason']))
        for n in ['result.json','sources.json','selection.json','retained.json']:provenance[str(run/n)]=sha(run/n)
    table=[]
    for k in CATEGORIES:
        c=fact_counts[k];skip=c['drop']+c['uncertain'];associations={}
        for arm in ['Q','Qplus']:
            groups={name:dict(values) for name,values in conditionals[arm][k].items()}
            for name,values in groups.items():
                if 'questions' in values:values['accuracy']=probability(values['correct'],values['questions'])
                else:values['skip_rate']=probability(values['skipped'],values['facts'])
            a=groups.get('any_skipped',{}).get('accuracy');b=groups.get('none_skipped',{}).get('accuracy')
            associations[arm]=dict(groups=groups,accuracy_difference_any_minus_none=a-b if a is not None and b is not None else None)
        table.append(dict(category=k,name=CN[k],initial_extracted_distinct_facts=c['total'],carried_by_kept_sources=c['keep'],
            carried_by_skipped_sources=skip,drop=c['drop'],uncertain=c['uncertain'],skip_rate=probability(skip,c['total']),
            carrier_observations=dict(carrier_counts[k]),questions_with_category=sum(1 for row in per_case if row['types'][k].get('total',0)),
            associations=associations))
    for scope in ['initial','additional','all']:
        totals[scope+'_skipped']=totals[scope+'_drop']+totals[scope+'_uncertain']
    result=dict(at=now(),root=str(root),questions=300,correct={a:sum(c['correct'][a] for c in per_case) for a in ['Q','Qplus']},
        conceptual_categories=['实体（含属性、开放状态）','动作/事件','实体关系','变化与结果','事件联系'],
        implemented_upstream_fact_categories=list(CATEGORIES),Q4_filter_unit='whole immutable literal observation',
        totals=dict(totals),skip_rates={k:probability(totals[k+'_skipped'],totals[k+'_observations']) for k in ['initial','additional','all']},
        first_round_before_review_decisions=dict(first_decisions),final_contributions=dict(final_contributions),
        initial_typed_fact_totals=sum(c['total'] for c in fact_counts.values()),upstream_raw_per_caption_fact_counts=dict(raw_upstream_facts),
        upstream_field_category_requested_questions=dict(field_requests),types=table,cases=per_case,
        notes=['Five top-level design concepts differ from seven physical fact collections; entity IDs are not independently filtered in Q4.',
            'Category counts are source-carried upstream N facts for initial21362 observations only. They are not direct Q4 field-selection logs.',
            'Deduplicate equal fact records across repeated caption IDs of one question/source; never merge across different sources or questions.',
            'Additional gap-retrieved captions have no Q4 type extraction, so type-level statistics cannot cover them.',
            'No facts of a type means no usable extraction sample here, not that the video lacks that information or the type is unimportant.',
            'All percentages are empirical frequencies. Question-level accuracy associations are observational, confounded and not causal ablations.',
            'States and relations occur in only one question each; observations within that question are not independent QA samples.',
            'Read-only completed original300 development analysis; no model calls, no changes to frozen training/500 runtimes or parameters.'],
        analysis_seconds=time.monotonic()-started)
    out.mkdir(parents=True,exist_ok=False);write(out/'report.json',result);write(out/'source_rows.json',source_rows);write(out/'input_hashes.json',provenance)
    with (out/'types.csv').open('w',newline='') as f:
        fields=['category','name','initial_extracted_distinct_facts','carried_by_kept_sources','carried_by_skipped_sources','skip_rate','questions_with_category']
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(table)
    # Audit the read-only guarantee after analysis, including all300 successful results.
    with ThreadPoolExecutor(max_workers=4) as pool:
        valid=all(pool.map(lambda row:sha(row[0])==row[1],provenance.items()))
    if not valid:raise ValueError('Frozen inputs changed during read-only analysis')
    write(out/'validation.json',dict(passed=True,questions=300,hashes_verified=len(provenance),
        fact_mapping='Exact question-local caption IDs, no text heuristics or model classification',
        initial_sources_fully_joined=totals['initial_observations']==21362,source_count_reconciles=totals['all_keep']+totals['all_skipped']==totals['all_observations'],
        model_calls=0,existing_result_hashes_unchanged=True,at=now()))
    print(json.dumps(dict(totals=result['totals'],skip_rates=result['skip_rates'],types=table),ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();analyze(a.root.resolve(),a.out.resolve())
