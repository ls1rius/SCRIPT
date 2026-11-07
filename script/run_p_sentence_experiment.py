"""Independent P experiment. Prepare explicit O-source cases; run only on command."""
import argparse
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script_core import o_sentence_filter as o
from script_core import p_sentence_filter as p
from script_core.budget import BudgetLedger,Meter
from script_core.config import digest
from script.o_batched_matrix import packet_for,extraction_decision


def batch(meter,uid,operation,requests):
    if operation==p.FILTER:return filter_batch(meter,uid,requests)
    condition='answer_with_options' if operation==p.REVIEW else 'original_question_only' if operation==p.RELEVANCE else 'source_only'
    packet=packet_for(uid,condition,operation,requests)
    result,_=meter.call(p.BATCH if operation in (p.FILTER,p.EXTRACT,p.GROUND,p.RELEVANCE,p.REVIEW) else 'o_independent_batch',packet)
    rows=result.get('items',[])
    if len(rows)!=len(requests):raise ValueError('P batch size mismatch')
    for (sid,req),row in zip(requests,rows):
        if row.get('source_id')!=sid or row.get('scope')!=packet['scope'] or row.get('request_hash')!=digest(req):
            raise ValueError('P batch identity mismatch')
        if operation in (p.GROUND,p.RELEVANCE):
            if row.get('parse_error') or row.get('data') is None:raise ValueError('P filter decode failed')
            o.validate_output(row['data'],p.ground_schema(req) if operation==p.GROUND else p.relevance_schema(req))
        if operation==p.REVIEW:
            if row.get('parse_error') or row.get('data') is None:raise ValueError('P reader review decode failed')
            p.validate_review(req,row['data'])
    return rows


def filter_batch(meter,uid,requests):
    # Enforce question isolation BEFORE flattening candidates for source-only checks.
    outer=packet_for(uid,'original_question_only',p.FILTER,requests)
    checks=[];mapping={}
    for sid,req in requests:
        for unit in req['candidates']:
            key=sid+'::'+unit['id']
            checks.append((key,p.ground_input(req,unit)));mapping[key]=(sid,unit['id'])
    grounded={sid:{} for sid,_ in requests}
    for start in range(0,len(checks),4):
        for row in batch(meter,uid,p.GROUND,checks[start:start+4]):
            sid,unit_id=mapping[row['source_id']];grounded[sid][unit_id]=row
    relevant=[]
    for sid,req in requests:
        candidates=[u for u in req['candidates'] if grounded[sid][u['id']]['data']['faithful']]
        if candidates:relevant.append((sid,dict(req,candidates=candidates)))
    relevance={}
    if relevant:relevance={row['source_id']:row for row in batch(meter,uid,p.RELEVANCE,relevant)}
    results=[]
    for sid,req in requests:
        decisions={}
        for unit in req['candidates']:
            unit_id=unit['id'];faithful=grounded[sid][unit_id]['data']['faithful']
            decisions[unit_id]=dict(fidelity='faithful' if faithful else 'unfaithful',
                decision=relevance[sid]['data']['decisions'][unit_id] if faithful else 'drop_unfaithful')
        data=dict(decisions=decisions);o.validate_output(data,p.schema(req))
        results.append(dict(source_id=sid,scope=outer['scope'],request_hash=digest(req),data=data,
            source_grounding=grounded[sid],question_relevance=relevance.get(sid),
            decision_origin='blind source-role witness comparison for action fields; source-only model review for other records; then original-question relevance; no field restoration'))
    return results


def run_case(backend,cfg,question,sources,original,folder,*,frozen=None,inherited_extraction_seconds=0.,selection_replay=None):
    """Fresh extraction or exact O extraction reuse; never reuse O labels/answers."""
    if folder.exists():raise ValueError('P case already attempted; no automatic retry')
    folder.mkdir(parents=True)
    ledger=BudgetLedger(3600,answer_reserve=cfg['budget']['final_answer_reserve'],calls_path=folder/'calls.jsonl')
    if selection_replay is not None and frozen is not None:raise ValueError('Cannot combine selection and extraction replay')
    ledger.dependency_cost=inherited_extraction_seconds+((selection_replay['successful_seconds']+
        selection_replay.get('prior_failed_seconds',0.)) if selection_replay else 0.)
    meter=Meter(backend,ledger,cfg,folder/'raw_calls')
    result=dict(question_id=question['question_id'],version=p.VERSION,complete=False,error=None,
        extraction_reused=frozen is not None,inherited_extraction_seconds=inherited_extraction_seconds,
        task_brief_generated=False,composition_called=False)
    try:
        decisions={};uid=question['question_id']
        if selection_replay is not None:
            from script.resume_p_context import validated_selection
            decisions=validated_selection(Path(selection_replay['folder']),question,sources)
            shutil.copytree(Path(selection_replay['folder'])/'sentences',folder/'sentences')
            io.write(folder/'selection_replay.json',selection_replay)
            result.update(selection_reused=True,inherited_selection_seconds=selection_replay['successful_seconds'],
                prior_failed_processing_seconds=selection_replay.get('prior_failed_seconds',0.))
        else:
            for start in range(0,len(sources),4):
                chunk=sources[start:start+4]
                reqs=[(s['id'],p.extraction_input(s)) for s in chunk]
                if frozen is None:
                    outputs=batch(meter,uid,p.EXTRACT,reqs)
                else:
                    outputs=[]
                    for sid,req in reqs:
                        saved=frozen[sid]
                        if saved['request']!=req:raise ValueError('P inherited extraction source mismatch')
                        outputs.append(saved['output'])
                for source,(_,req),output in zip(chunk,reqs,outputs):
                    sf=folder/'sentences'/source['id']
                    io.write(sf/'source_view.json',p.source_view(source['text']))
                    # Persist the model output before rejecting a malformed extraction.
                    # A valid unavailable extraction may retain its intact content;
                    # that raw candidate must use the same metadata-free view too.
                    decision=extraction_decision(dict(source,text=req['sentence']),req,output,sf)
                    decision['source']=deepcopy(source)
                    if (decision.get('fallback') or '').startswith('extraction_format_failure:'):
                        raise ValueError('P extraction failed at '+source['id']+': '+decision['fallback'])
                    decisions[source['id']]=decision
            io.write(folder/'filter_question.json',dict(question=question['question']))
            for start in range(0,len(sources),4):
                chunk=sources[start:start+4]
                reqs=[(s['id'],p.filter_input(question,s,decisions[s['id']]['candidates'])) for s in chunk]
                outputs=batch(meter,uid,p.FILTER,reqs)
                for source,(_,req),output in zip(chunk,reqs,outputs):
                    sid=source['id'];d=decisions[sid]
                    judgments,labels,selected=p.selection(req,output['data'])
                    d.update(model_decisions=judgments,model_labels=labels,labels=deepcopy(labels),
                             label_origin='deterministic_projection_of_model_decision',program_overrides=[],selected=selected)
                    sf=folder/'sentences'/sid
                    io.write(sf/'filter_decision_input.json',req);io.write(sf/'filter_decision.json',output['data']);io.write(sf/'filter_stage_audit.json',output);io.write(sf/'selection.json',d)
        io.write(folder/'selection.json',decisions)
        rows=p.present(sources,decisions)
        review_seconds=0.
        review=None
        if rows:
            review_request=p.review_input(rows,question)
            io.write(folder/'reader_review_input.json',review_request)
            before=ledger.actual
            review_output=batch(meter,uid,p.REVIEW,[('reader',review_request)])[0]
            review_seconds=ledger.actual-before
            review=review_output['data']
            io.write(folder/'reader_review.json',review_output)
        final=p.final_input(original,rows,question,review=review)
        io.write(folder/'retained.json',rows);io.write(folder/'final_input.json',final)
        answer,call=meter.call(p.ANSWER,dict(inputs=final,options=question['options']),final=True)
        io.write(folder/'answer.json',answer)
        result.update(complete=True,answer=answer['data'],input_tokens=answer['usage']['input_tokens'],
            retained_sentences=len(rows),retained_units=sum(len(d['selected']) for d in decisions.values()),
            candidate_units=sum(len(d['candidates']) for d in decisions.values()),
            all_zero_sentences=sum(not d['selected'] for d in decisions.values()),answer_seconds=call['actual_cost'],
            reader_review_seconds=review_seconds,reader_total_seconds=review_seconds+call['actual_cost'],
            reader_review_called=review is not None)
        return result
    except Exception as exc:
        result['error']=dict(type=type(exc).__name__,message=str(exc));raise
    finally:
        result['cost_summary']=ledger.summary();result['updated_at']=io.now()
        io.write(folder/'result.json',result);meter.close()


def verify(out):
    for name,sha in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=sha:raise ValueError('P frozen artifact changed: '+name)


def report(out):
    policy=io.read(out/'experiment.json');gold=io.read(out/'evaluation_only.json');rows=[]
    for uid in policy['case_ids']:
        f=out/'runs'/uid/'result.json'
        if f.exists():rows.append(io.read(f))
    success=[r for r in rows if r['complete']]
    result=dict(version=p.VERSION,planned=len(policy['case_ids']),completed=len(success),errors=sum(bool(r['error']) for r in rows),
        correct=sum(str(r['answer']['selected_option'])==str(gold[r['question_id']]) for r in success),
        complete=len(success)==len(policy['case_ids']),cases=rows,task_brief_generated=False,composition_called=False,
        new_processing_seconds=sum(r['cost_summary']['total'] for r in rows),
        inherited_extraction_seconds=sum(r['inherited_extraction_seconds'] for r in rows),
        reader_review_seconds=sum(r.get('reader_review_seconds',0.) for r in rows),
        reader_total_seconds=sum(r.get('reader_total_seconds',r.get('answer_seconds',0.)) for r in rows),updated_at=io.now())
    result['matched_O']=dict(completed=len(success),correct=sum(
        io.read(out/'cases'/r['question_id']/'O_reference.json')['correct'] for r in success))
    io.write(out/'report.json',result);return result


def prepare(out,parent,ids,*,fresh_extraction=False):
    from script.run_o_sentence_experiment import verify as verify_o
    verify_o(parent)
    cohort=io.read(parent/'cohort.json')
    if not ids or len(set(ids))!=len(ids) or any(uid not in cohort for uid in ids):raise ValueError('Explicit unique O cohort IDs required')
    ids=sorted(ids,key=cohort.index)
    if out.exists():raise ValueError('Use a new P directory')
    # Check every source before creating a prepared experiment.
    paths=[]
    for uid in ids:
        r=io.read(parent/'runs'/uid/'result.json')
        if 'O_question_only' not in r['arms']:raise ValueError('O comparison answer not ready')
        for name in ['question.json','sentences.json','original_qa.json']:
            paths.append(parent/'cases'/uid/name)
        if not fresh_extraction:
            for s in io.read(parent/'cases'/uid/'sentences.json'):
                paths.append(parent/'runs'/uid/'shared_extraction/sentences'/s['id']/'extraction.json')
        paths.append(parent/'runs'/uid/'result.json')
    before={str(path):io.sha(path) for path in paths}
    out.mkdir(parents=True)
    for name in ['qa_config.json','qa_expected_artifacts.json']:
        shutil.copy2(parent/name,out/name)
    cfg=io.read(out/'qa_config.json')
    cfg['models']['cuda_allocator_limit_mib']=21504
    io.write(out/'qa_config.json',cfg)
    if cfg['query']['json_repair_retries'] or cfg['query']['tool_transient_retries']:raise ValueError('P requires no hidden retries')
    gold=io.read(parent/'evaluation_only.json');io.write(out/'evaluation_only.json',{uid:gold[uid] for uid in ids})
    io.write(out/'cohort.json',ids)
    for uid in ids:
        target=out/'cases'/uid;target.mkdir(parents=True)
        for name in ['question.json','sentences.json','original_qa.json']:shutil.copy2(parent/'cases'/uid/name,target/name)
        old=io.read(parent/'runs'/uid/'result.json')
        io.write(target/'O_reference.json',old['arms']['O_question_only'])
        if not fresh_extraction:
            frozen={s['id']:io.read(parent/'runs'/uid/'shared_extraction/sentences'/s['id']/'extraction.json') for s in io.read(target/'sentences.json')}
            io.write(target/'frozen_extraction.json',frozen)
        io.write(target/'source.json',dict(parent=str(parent),extraction_reused=not fresh_extraction,
            inherited_extraction_seconds=old['shared_extraction_seconds'] if not fresh_extraction else 0.))
    io.write(out/'experiment.json',dict(version=p.VERSION,parent_scheme='O',parent_run=str(parent),case_ids=ids,
        extraction='fresh P source-only' if fresh_extraction else 'frozen O extraction diagnostic',new_filtering=True,new_answers=True,
        decision_protocol='blind source-role witness comparison for action fields; source-only record checks; original-question relevance; no automatic restoration',
        reader_protocol='all-option source-reference review then neutral selection; review is not evidence',
        task_brief=False,composition=False,filter_options=False,answer_options=True,batch_size=4,
        new_navigation=False,training=False,adapter=None,created_at=io.now(),
        source_hashes=before,scope='Explicit fixed-source O/P comparison; no claim of independent full navigation.',
        resource_policy=dict(preference=['0','1','2','3'],minimum_free_mib=23552,maximum_utilization=100,stable_observations=3,selection='most_free_memory',admission_mode='memory_shared',allocator_limit_mib=21504)))
    for name in ['script','script_core']:shutil.copytree(ROOT/name,out/'runtime'/name,ignore=shutil.ignore_patterns('__pycache__'))
    for file in ROOT.glob('*.py'):shutil.copy2(file,out/'runtime'/file.name)
    if any(io.sha(Path(path))!=sha for path,sha in before.items()):raise ValueError('Source changed during P preparation')
    io.write(out/'frozen_hashes.json',{str(f.relative_to(out)):io.sha(f) for f in out.rglob('*') if f.is_file()})
    verify(out);report(out);io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))


def worker(out):
    from script_core.budget import ModelWorker
    from script.run_evidence_planner_experiment import verify_model
    verify(out);verify_model(out)
    if (out/'setup.json').exists():raise ValueError('P already attempted; no automatic retry')
    cfg=io.read(out/'qa_config.json');backend=ModelWorker(cfg)
    io.write(out/'setup.json',dict(state='loading',at=io.now()))
    try:
        start=time.monotonic();details=backend.call('prepare_vlm',{},600)
        io.write(out/'setup.json',dict(state='ready',seconds=time.monotonic()-start,details=details))
        from script.p_repair_validation import run as validate_repair
        validate_repair(out,backend,cfg)
        for index,uid in enumerate(io.read(out/'cohort.json'),1):
            io.write(out/'progress.json',dict(stage='P',case=index,question_id=uid,updated_at=io.now()))
            case=out/'cases'/uid;source=io.read(case/'source.json')
            run_case(backend,cfg,io.read(case/'question.json'),io.read(case/'sentences.json'),io.read(case/'original_qa.json'),out/'runs'/uid,
                frozen=io.read(case/'frozen_extraction.json') if source['extraction_reused'] else None,
                inherited_extraction_seconds=source['inherited_extraction_seconds'])
            report(out)
    except Exception as exc:
        io.write(out/'failure.json',dict(type=type(exc).__name__,message=str(exc),at=io.now()));raise
    finally:backend.close(graceful=True);report(out)


def queue(out):
    from script.gpu_queue import acquire
    verify(out)
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('P already launched')
        def status(state,**extra):io.write(out/'queue_status.json',dict(state=state,controller_pid=os.getpid(),updated_at=io.now(),**extra))
        try:
            with acquire(io.read(out/'experiment.json')['resource_policy'],lambda info:status('waiting_for_gpu',**info)) as (gpu,fds):
                env=dict(os.environ,CUDA_VISIBLE_DEVICES=gpu['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
                cmd=[sys.executable,'-u',str(out/'runtime/script/run_p_sentence_experiment.py'),'worker','--out',str(out)]
                with (out/'worker.log').open('a') as log:
                    child=subprocess.Popen(cmd,cwd=out/'runtime',env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,pass_fds=fds)
                launch=dict(worker_pid=child.pid,gpu=gpu['index'],command=cmd,at=io.now());io.write(out/'launch.json',launch)
                while child.poll() is None:status('running',**launch);time.sleep(5)
                if child.returncode or not report(out)['complete']:raise RuntimeError('P failed; records retained')
                status('complete',**launch)
        except Exception as exc:status('failed',error=str(exc));raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['prepare','queue','worker','report']);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--parent',type=Path);parser.add_argument('--case-id',action='append');parser.add_argument('--fresh-extraction',action='store_true')
    args=parser.parse_args();out=args.out.resolve()
    if args.mode=='prepare':
        if not args.parent or not args.case_id:parser.error('prepare requires --parent and --case-id')
        prepare(out,args.parent.resolve(),args.case_id,fresh_extraction=args.fresh_extraction)
    elif args.mode=='queue':queue(out)
    elif args.mode=='worker':worker(out)
    else:report(out)
