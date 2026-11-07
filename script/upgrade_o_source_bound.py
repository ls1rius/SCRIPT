"""Explicit O+ version migration: preserve O/extract/filter, regenerate all O+."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script_core import o_sentence_filter as core
from script_core.config import digest


def copy_file(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)


def recover_rows(old,folder,uid):
    """Finish derived writes from durable rows only; no model retries."""
    from script.o_batched_matrix import extraction_decision,decode
    sources={s['id']:s for s in io.read(old/'cases'/uid/'sentences.json')}
    recovered=[]
    for area in ['shared_extraction','question_only']:
        target=folder/area
        calls=target/'calls.jsonl'
        if not calls.exists():continue
        for line in calls.read_text().splitlines():
            call=json.loads(line)
            if call.get('error'):raise ValueError('Cannot inherit failed call')
            if call['operation']!='o_independent_batch':continue
            raw=io.read(target/'raw_calls'/f"{call['call_id']}-{call['attempt']}.json")
            packet=raw['inputs'];op=packet['operation']
            if op not in [core.EXTRACT,core.FILTER]:continue
            if packet['scope']['question_id']!=uid:raise ValueError('Foreign inherited batch')
            rows=raw['output']['items']
            if len(rows)!=len(packet['items']):raise ValueError('Incomplete inherited batch')
            for request,row in zip(packet['items'],rows):
                sid=request['source_id'];sf=target/'sentences'/sid;req=request['request']
                if row['source_id']!=sid or row['scope']!=packet['scope'] or row['request_hash']!=digest(req):
                    raise ValueError('Inherited row mismatch')
                name='extraction.json' if op==core.EXTRACT else 'selection.json'
                if (sf/name).exists():continue
                if op==core.EXTRACT:
                    if req!=core.extraction_input(sources[sid]):raise ValueError('Inherited source mismatch')
                    extraction_decision(sources[sid],req,row,sf)
                else:
                    if req['sentence']!=sources[sid]['text']:raise ValueError('Inherited filter source mismatch')
                    data=io.read(folder/'shared_extraction/sentences'/sid/'extraction.json')
                    d=extraction_decision(sources[sid],data['request'],data['output'],folder/'shared_extraction/sentences'/sid)
                    if d['candidates']!=req['candidates']:raise ValueError('Inherited candidates mismatch')
                    labels=decode(op,req,row);final,overrides=core.preserve_action_core(d['candidates'],labels)
                    d.update(model_labels=labels,labels=final,program_overrides=overrides,selected=core.select(d['candidates'],final))
                    io.write(sf/'filter_decision_input.json',req);io.write(sf/'filter_decision.json',row['data']);io.write(sf/name,d)
                recovered.append(dict(area=area,source_id=sid,call_id=call['call_id']))
    return recovered


def prepare(old,out):
    from script.run_o_sentence_experiment import verify
    from script.o_composition_context_eval import report
    verify(old)
    if out.exists():raise ValueError('Use a fresh O+ migration directory')
    status=io.read(old/'queue_status.json')
    for pid in [status.get('worker_pid'),status.get('controller_pid')]:
        if pid and Path(f'/proc/{pid}').exists():raise ValueError('Parent still active')
    if list((old/'runs').rglob('*.pending.json')):raise ValueError('Parent has pending calls')
    before={str(p.relative_to(old)):io.sha(p) for p in old.rglob('*') if p.is_file()}
    out.mkdir(parents=True)
    for name in ['qa_config.json','qa_expected_artifacts.json','evaluation_only.json','cohort.json','cohost_queue.py','cohost_authorization.json']:
        copy_file(old/name,out/name)
    for name in ['questions','cases']:
        shutil.copytree(old/name,out/name)
    policy=io.read(old/'experiment.json')
    policy.update(created_at=io.now(),plus_version=core.PLUS_VERSION,composition_source_bound=True,
        previous_run=str(old),answers_reused='O only; all O+ regenerated',
        scope='Full 300-case N-source evaluation; unchanged O and selection; source-bound O+ v15; old O+ archived separately.',
        known_semantic_limitations='Source-bound composition prevents additions to selected fields, not upstream extraction/filter errors; historical partial_filter failure remains.')
    io.write(out/'experiment.json',policy)
    copy_file(old/'validation/functional.json',out/'validation/functional.json')
    audit=dict(previous_run=str(old),new_run=str(out),created_at=io.now(),preserved_O=[],recovered_rows={},
               legacy_composition_and_answer_seconds=0.,inherited_active_calls_seconds=0.,parent_hashes=before)
    for parent in sorted((old/'runs').iterdir()):
        if not parent.is_dir():continue
        uid=parent.name;folder=out/'runs'/uid
        shutil.copytree(parent,folder)
        audit['recovered_rows'][uid]=recover_rows(old,folder,uid)
        target=folder/'question_only';calls=target/'calls.jsonl'
        if calls.exists():
            keep=[]
            for line in calls.read_text().splitlines():
                call=json.loads(line);op=call['operation']
                if op=='o_independent_batch':op=call['parameters']['operation']
                if op in [core.COMPOSE,'oplus_answer']:
                    audit['legacy_composition_and_answer_seconds']+=call['actual_cost']
                    raw=target/'raw_calls'/f"{call['call_id']}-{call['attempt']}.json"
                    if raw.exists():raw.unlink()
                else:
                    keep.append(line);audit['inherited_active_calls_seconds']+=call['actual_cost']
            calls.write_text('\n'.join(keep)+ ('\n' if keep else ''))
        for name in ['composition.json','composition_input.json']:
            for p in target.rglob(name):p.unlink()
        for name in ['cost_summary.json']:
            p=target/name
            if p.exists():p.unlink()
        plus=folder/'Oplus_question_only'
        if plus.exists():shutil.rmtree(plus)
        rp=folder/'result.json'
        result=io.read(rp) if rp.exists() else dict(question_id=uid,arms={},conditions={},error=None)
        if result.get('error'):raise ValueError('Parent case error; no automatic retries')
        result['arms'].pop('Oplus_question_only',None)
        if 'O_question_only' in result['arms']:audit['preserved_O'].append(uid)
        result.update(complete=False,conditions={},plus_version=core.PLUS_VERSION,source_bound_upgrade_from=str(parent))
        io.write(rp,result)
    for name in ['script','script_core']:
        shutil.copytree(ROOT/name,out/'runtime'/name,ignore=shutil.ignore_patterns('__pycache__'))
    for p in ROOT.glob('*.py'):copy_file(p,out/'runtime'/p.name)
    io.write(out/'upgrade_audit.json',audit)
    # Freeze reusable artifacts only; results/compositions/costs will be updated.
    def immutable(p):
        rel=p.relative_to(out)
        if rel.parts[0]!='runs':return rel.name not in ['report.json','queue_status.json']
        return p.name in ['extraction.json','selection.json','filter_decision.json','filter_decision_input.json'] or 'O_question_only' in rel.parts or ('raw_calls' in rel.parts and p.suffix=='.json')
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file() and immutable(p)})
    after={str(p.relative_to(old)):io.sha(p) for p in old.rglob('*') if p.is_file()}
    if before!=after:raise ValueError('Parent changed during migration')
    verify(out);report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    print(json.dumps(dict(prepared=str(out),preserved_O=len(audit['preserved_O']),legacy_seconds=audit['legacy_composition_and_answer_seconds'],parent_files=len(before))))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--old',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();prepare(a.old.resolve(),a.out.resolve())
