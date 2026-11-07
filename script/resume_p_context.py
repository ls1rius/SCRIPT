"""Explicit P continuation after a context preflight failure; no automatic retries."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script_core import p_sentence_filter as p,o_sentence_filter as o
from script_core.config import digest
from script_core.o_batch_generation import messages_for


def hashes(root):
    files=sorted(f for f in root.rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values=list(pool.map(io.sha,files))
    return {str(f.relative_to(root)):v for f,v in zip(files,values)}


def validated_selection(folder,question,sources):
    decisions=io.read(folder/'selection.json')
    if set(decisions)!={s['id'] for s in sources}:raise ValueError('Selection source identities differ')
    def validate(source):
        sid=source['id'];sf=folder/'sentences'/sid;d=decisions[sid]
        if d['source']!=source or io.read(sf/'selection.json')!=d:raise ValueError('Selection source changed')
        extraction=io.read(sf/'extraction.json');req=p.extraction_input(source);row=extraction['output']
        if extraction['request']!=req or row['request_hash']!=digest(req) or row.get('parse_error'):
            raise ValueError('Extraction replay mismatch')
        if row['scope']!={'question_id':question['question_id'],'condition':'source_only'} or row['source_id']!=sid:
            raise ValueError('Extraction replay scope mismatch')
        if row['prompt']!=messages_for(p.EXTRACT,req)[0]:raise ValueError('Extraction prompt changed')
        if row['data']!=p.normalize_extraction(row['wire_data']):raise ValueError('Extraction normalization changed')
        if d['candidates']!=o.candidates(dict(source,text=req['sentence']),row['data']):raise ValueError('Candidates changed')
        req=p.filter_input(question,source,d['candidates']);audit=io.read(sf/'filter_stage_audit.json')
        if io.read(sf/'filter_decision_input.json')!=req or audit['request_hash']!=digest(req):raise ValueError('Filter input changed')
        if audit['scope']!={'question_id':question['question_id'],'condition':'original_question_only'} or audit['source_id']!=sid:
            raise ValueError('Filter replay scope mismatch')
        for unit in d['candidates']:
            ground=p.ground_input(req,unit);g=audit['source_grounding'][unit['id']]
            if g['request_hash']!=digest(ground) or g['prompt']!=messages_for(p.GROUND,ground)[0] or g.get('parse_error'):
                raise ValueError('Grounding replay changed')
            if g['data']!={'faithful':p.ground_assessment(ground,g['wire_data'])['faithful']}:
                raise ValueError('Grounding projection changed')
        retained=[u for u in d['candidates'] if audit['source_grounding'][u['id']]['data']['faithful']]
        if retained:
            rel=dict(req,candidates=retained);r=audit['question_relevance']
            if r['request_hash']!=digest(rel) or r['prompt']!=messages_for(p.RELEVANCE,rel)[0] or r.get('parse_error'):
                raise ValueError('Relevance replay changed')
        judgments,labels,selected=p.selection(req,audit['data'])
        if (judgments,labels,selected)!=(d['model_decisions'],d['labels'],d['selected']):raise ValueError('Selection projection changed')
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(validate,sources))
    if p.review_input(p.present(sources,decisions),question)!=io.read(folder/'reader_review_input.json'):
        raise ValueError('Review input changed')
    return decisions


def verify_inherited_gate(out):
    policy=io.read(out/'experiment.json');info=policy['inherited_generic_validation'];parent=Path(info['parent'])
    for name,sha in info['files'].items():
        if io.sha(parent/name)!=sha:raise ValueError('Inherited generic gate changed: '+name)
    gate=io.read(parent/'validation/repair_functional.json')
    if not gate['passed'] or not all(c['passed'] for c in gate['checks']):raise ValueError('Inherited generic gate not passed')
    io.write(out/'validation/inherited_generic_gate.json',dict(passed=True,new_calls=False,checks=len(gate['checks']),source=info))


def prepare(out,parent):
    from script import run_p_sentence_experiment as single,run_p_full_experiment as full
    if out.exists():raise ValueError('Use a new P resume directory')
    single.verify(parent)
    if io.read(parent/'queue_status.json')['state']!='failed':raise ValueError('Only explicit failed runs can be resumed')
    before=hashes(parent);policy=io.read(parent/'experiment.json');old=io.read(parent/'report.json')
    if policy['version']!=p.VERSION:raise ValueError('P semantic method must remain unchanged')
    for name in ['script_core/p_sentence_filter.py','script_core/o_batch_generation.py','script/p_repair_validation.py']:
        if io.sha(ROOT/name)!=io.sha(parent/'runtime'/name):raise ValueError('Semantic implementation changed: '+name)
    failed=[r for r in old['cases'] if r['error']];success=[r['question_id'] for r in old['cases'] if r['complete'] and not r['error']]
    if len(failed)!=1 or 'context overflow' not in failed[0]['error']['message']:raise ValueError('Expected one context failure')
    uid=failed[0]['question_id'];case=parent/'cases'/uid;attempt=parent/'runs'/uid
    validated_selection(attempt,io.read(case/'question.json'),io.read(case/'sentences.json'))
    calls=[__import__('json').loads(x) for x in (attempt/'calls.jsonl').read_text().splitlines()]
    bad=[c for c in calls if c.get('error')]
    if len(bad)!=1 or io.read(bad[0]['output_ref'])['inputs']['operation']!=p.REVIEW or bad[0]['output_tokens']!=0:
        raise ValueError('Resume supports only review preflight failure')
    for c in calls:
        raw=io.read(Path(c['output_ref']))
        if digest(raw['inputs'])!=c['input_hash']:raise ValueError('Raw call input mismatch')
        if not c.get('error') and (raw.get('error') or raw['output'] is None):raise ValueError('Successful call missing output')
    out.mkdir(parents=True)
    names=['cohort.json','evaluation_only.json','qa_config.json','qa_expected_artifacts.json','questions','cases','runtime']
    def copy(name):
        src=parent/name;dst=out/name
        if src.is_dir():shutil.copytree(src,dst,ignore=shutil.ignore_patterns('__pycache__'))
        else:shutil.copy2(src,dst)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(copy,names))
    def copy_success(uid):shutil.copytree(parent/'runs'/uid,out/'runs'/uid)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(copy_success,success))
    shutil.copytree(attempt,out/'inherited_preprocessing'/uid)
    for name in ['script/run_p_sentence_experiment.py','script/run_p_full_experiment.py','script/resume_p_context.py']:
        shutil.copy2(ROOT/name,out/'runtime'/name)
    cfg=io.read(out/'qa_config.json');cfg['models']['max_input_tokens']=32768;io.write(out/'qa_config.json',cfg)
    policy.update(resume_parent=str(parent),resumed_success_case_ids=success,
        selection_replays={uid:dict(folder=str(out/'inherited_preprocessing'/uid),parent=str(attempt),successful_seconds=sum(c['actual_cost'] for c in calls if not c.get('error')),successful_calls=len(calls)-1)},
        historical_failed_call_seconds=sum(c['actual_cost'] for c in bad),
        inherited_generic_validation=dict(parent=str(parent),files={'validation/repair_functional.json':io.sha(parent/'validation/repair_functional.json')}),
        continuation_policy='Explicit user-authorized continuation; completed stages reused exactly; original failure archived; no automatic retry.',
        created_at=io.now())
    io.write(out/'experiment.json',policy)
    io.write(out/'validation/parent_hashes.json',before)
    verify_inherited_gate(out)
    if hashes(parent)!=before:raise ValueError('Old P artifacts changed during preparation')
    io.write(out/'validation/resume_preflight.json',dict(passed=True,old_files_unchanged=len(before),successful_cases=len(success),source_rows=len(io.read(case/'sentences.json')),new_model_calls=0,context_input_limit_before=16384,context_input_limit_after=32768,semantic_method_changed=False,created_at=io.now()))
    io.write(out/'frozen_hashes.json',hashes(out));single.verify(out);full.report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    print('P CONTEXT RESUME PREPARED',out,flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);ap.add_argument('--parent',type=Path,required=True)
    args=ap.parse_args();prepare(args.out.resolve(),args.parent.resolve())
