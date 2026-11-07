"""Isolated Q v3 experiments: fixed-source filtering and audited source expansion."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import run_q_experiment as base
from script.scale_live_parallel_pool import read, write, sha, identity, now, verify_manifest
from script_core import q_repair as r, q_evidence as q
from script_core.config import digest


def ask(meter, uid, operation, requests, batch_size=4):
    from script_core.o_batch_generation import validate_packet
    # Exact-input reuse is local to this one metered case. The original successful
    # call remains charged once; changed context, operation or question never hits.
    cache = getattr(meter, '_q3_cache', None)
    if cache is None:
        cache = meter._q3_cache = {}
        meter._q3_reuses = []
    keys = [(uid,operation,digest(req)) for req in requests]
    pending = [(key,req) for key,req in zip(keys,requests) if key not in cache]
    seen = set()
    pending = [(key,req) for key,req in pending if not (key in seen or seen.add(key))]
    new_requests = [req for key,req in pending]
    for key in keys:
        if key in cache:
            meter._q3_reuses.append(dict(question_id=uid,operation=operation,request_hash=key[2],output_ref=cache[key][1]))
    scope = dict(question_id=uid, condition='original_question_only' if operation=='q3_filter' else 'evidence_audit_with_options')
    for start in range(0, len(new_requests), batch_size):
        subset = new_requests[start:start+batch_size]
        payload = dict(scope=scope, operation=operation, items=[dict(source_id=f'W{start+i}',scope=dict(scope),request=x)
                                                               for i,x in enumerate(subset)])
        validate_packet(payload)
        response, call = meter.call(r.BATCH, payload)
        if len(response.get('items', [])) != len(subset):
            raise ValueError('Q3 batch row count mismatch')
        for item, expected in zip(response['items'], payload['items']):
            if (item['source_id'] != expected['source_id'] or item['scope'] != scope or
                item['request_hash'] != digest(expected['request']) or item.get('parse_error') or not item['ended_normally']):
                raise ValueError('Q3 row identity/schema/EOS failure')
            value=r.validate_verdict(operation, expected['request'], item['data'])
            cache[(uid,operation,digest(expected['request']))]=(value,call['output_ref'])
    return [cache[key][0] for key in keys]


def filter_sources(meter, uid, question, sources, folder):
    requests = r.windows(question['question'], sources)
    verdicts = ask(meter, uid, 'q3_filter', requests)
    decisions = {}
    for i,(request, verdict) in enumerate(zip(requests, verdicts)):
        write(folder/'windows'/f'{i}.json', dict(request=request, verdict=verdict))
        for sid, decision in verdict.items():
            if sid in decisions:
                raise ValueError('Duplicate filter target')
            decisions[sid] = dict(decision=r.decision(decision))
    if set(decisions) != {s['id'] for s in sources}:
        raise ValueError('Incomplete Q3 selection')
    return decisions


def audit(meter, uid, question, sources, regions, folder):
    observations = r.views(sources)
    requests = [dict(question=question['question'], options=question['options'], option_index=i, observations=observations)
                for i in range(len(question['options']))]
    checks = ask(meter, uid, 'q3_option_audit', requests, batch_size=2)
    request = dict(question=question['question'],options=question['options'],observations=observations,regions=regions)
    coverage = ask(meter,uid,'q3_coverage',[request],batch_size=1)[0]
    result = r.certificate(checks,coverage)
    write(folder/'certificate.json',result)
    return result


def reconcile(meter, uid, question, sources, decisions, before, after, folder):
    missing = r.witness_ids(before) - {sid for sid,v in decisions.items() if v['decision']=='keep'}
    # Revisit only identified lost witnesses, not the entire uncertain archive.
    if not missing:
        return decisions, False
    allobs = r.views(sources)
    targets = [o for o in allobs if o['source_id'] in missing]
    requests = []
    for start in range(0,len(targets),r.WINDOW):
        group = targets[start:start+r.WINDOW]
        ids = {o['source_id'] for o in group}
        requests.append(dict(question=question['question'],options=question['options'],observations=group,
            context=[o for o in allobs if o['source_id'] not in ids], witness_ids=sorted(missing)))
    verdicts = ask(meter,uid,'q3_reconsider',requests,batch_size=1)
    changed = dict(decisions)
    for i,(request, verdict) in enumerate(zip(requests,verdicts)):
        write(folder/f'{i}.json',dict(request=request,verdict=verdict))
        for sid,decision in verdict.items():
            changed[sid] = dict(decision=r.decision(decision))
    return changed, changed != decisions


def run_case(backend,cfg,question,sources,original,folder,archive=None,mode='filter_only',progress=None):
    from script_core.budget import BudgetLedger, Meter
    if folder.exists():
        raise ValueError('Q3 will not retry or overwrite an existing attempt')
    folder.mkdir(parents=True)
    uid = question['question_id']
    ledger = BudgetLedger(3600, answer_reserve=90, calls_path=folder/'calls.jsonl')
    # Conservatively charge the complete archived N case (including its earlier
    # failures recorded in budgeted_total) once, rather than hiding source work.
    inherited = float((archive or {}).get('inherited_N_budgeted_seconds',0))
    ledger.dependency_cost = inherited
    meter = Meter(backend,ledger,cfg,folder/'raw_calls')
    result = dict(question_id=uid,version=r.VERSION,plus_version=r.PLUS_VERSION,mode=mode,
                  complete=False,error=None,arms={},new_navigation=mode=='coverage_repair',
                  source_dependency_seconds=inherited,source_dependency_policy='conservative full archived N budgeted case, charged once',
                  selection_policy='keep_only; uncertain archived', extraction_called=False,composition_called=False)
    try:
        current = sources
        regions = r.temporal_regions(question['question'],archive['scope'],archive['scenes']) if archive else []
        retrievals = []
        final_audit = None
        for iteration in range(r.MAX_NEW_SCENES+1):
            stage = folder/'stages'/str(iteration)
            write(stage/'sources.json',current)
            if progress:progress('filter',iteration=iteration,observations=len(current))
            decisions = filter_sources(meter,uid,question,current,stage)
            write(stage/'selection_initial.json',decisions)
            if mode=='coverage_repair':
                if not archive:raise ValueError('Coverage mode requires source archive')
                if progress:progress('audit',iteration=iteration)
                before = audit(meter,uid,question,current,regions,stage/'before_filter')
                kept_sources = [s for s in current if decisions[s['id']]['decision']=='keep']
                after = audit(meter,uid,question,kept_sources,regions,stage/'after_filter')
                decisions,changed = reconcile(meter,uid,question,current,decisions,before,after,stage/'reconsider')
                if changed:
                    kept_sources = [s for s in current if decisions[s['id']]['decision']=='keep']
                    after = audit(meter,uid,question,kept_sources,regions,stage/'after_reconsider')
                final_audit = after
            write(stage/'selection_final.json',decisions)
            chosen = q.selected_observations(current,decisions)
            if mode=='filter_only':
                result['stop_reason']='fixed_source_ablation_no_sufficiency_claim'
                break
            if final_audit['sufficient']:
                result['stop_reason']='audited_sufficient'
                break
            available = r.available_scenes(archive,current)
            if not available or iteration==r.MAX_NEW_SCENES or ledger.remaining < 600:
                result['stop_reason']=('source_exhausted_insufficient' if not available else
                    'retrieval_limit_insufficient' if iteration==r.MAX_NEW_SCENES else 'budget_reserve_insufficient')
                break
            missing_regions = [z for z in regions if z['id'] in final_audit['missing_regions']]
            if missing_regions:
                candidates = [s for s in available if any(s['interval'][0]<z['interval'][1] and s['interval'][1]>z['interval'][0]
                                                         for z in missing_regions)]
                if candidates:available=candidates
            request=dict(question=question['question'],options=question['options'],assessments=final_audit,
                         missing_regions=missing_regions,scenes=available)
            selected=ask(meter,uid,'q3_navigate',[request],batch_size=1)[0]['scene_id']
            expanded=r.read_scene(archive,current,selected)
            retrievals.append(dict(scene_id=selected,added_observations=len(expanded)-len(current)))
            write(stage/'navigation.json',dict(request=request,selected_scene=selected,added_sources=expanded[len(current):]))
            current=expanded
        write(folder/'selection.json',decisions)
        write(folder/'sources.json',current)
        write(folder/'retained.json',chosen)
        result.update(source_observations=len(current),initial_source_observations=len(sources),retained_observations=len(chosen),
            decisions={v:sum(d['decision']==v for d in decisions.values()) for v in ('keep','uncertain','drop')},
            retained_evidence_hash=digest(chosen),retrievals=retrievals,final_certificate=final_audit,regions=regions)
        # The auditor's proposed answer/reasons never enter the final reader.
        for arm,operation,compact in [('Q','q_answer',False),('Qplus','qplus_answer',True)]:
            final=q.final_input(original,chosen,compact)
            write(folder/arm/'final_input.json',final)
            if progress:progress('answer',arm=arm)
            answer,call=meter.call(operation,dict(inputs=final,options=question['options']),final=True)
            option=str(answer.get('data',{}).get('selected_option'))
            if answer.get('parse_error') or option not in {str(i) for i in range(len(question['options']))}:
                raise ValueError('Invalid strict Q3 answer')
            write(folder/arm/'answer.json',answer)
            result['arms'][arm]=dict(answer=answer['data'],input_tokens=call['input_tokens'],answer_seconds=call['actual_cost'],
                                     retained_evidence_hash=digest(chosen))
        result['complete']=True
    except BaseException as exc:
        result['error']=str(exc)
        raise
    finally:
        write(folder/'exact_request_reuse.json',getattr(meter,'_q3_reuses',[]))
        result.update(cost_summary=ledger.summary(),updated_at=now())
        write(folder/'result.json',result)
        meter.close()
    return result


def prepare(out,source,parent_n,workers,mode):
    base.prepare(out,source,workers)
    # This is still preparation, before launch or model calls. Final manifest is
    # frozen once all additional input artifacts and experiment settings exist.
    policy=read(out/'experiment.json')
    policy.update(version=r.VERSION,plus_version=r.PLUS_VERSION,mode=mode,
        original_question_only=mode=='filter_only',options_in_filter=False,
        options_in_post_filter_review=mode=='coverage_repair',
        context='time-ordered windows <=8 targets within 20 seconds plus <=4 neighbors; not asserted event identity',
        source_limitation='Frozen N caption archive; new gap-directed scene reads in coverage_repair; no new pixel verification.',
        source_dependency_policy='Full archived N budgeted case charged once per question; new processing reported separately',
        max_new_scenes=r.MAX_NEW_SCENES,parent_N=str(parent_n))
    policy['known_analysis_scope']='All 300 previously evaluated; subset tags inherited from Q v2 are historical only.'
    policy['contribution_labels']={'direct':'keep','context':'keep','irrelevant':'drop','uncertain':'uncertain'}
    write(out/'experiment.json',policy)
    def prepare_case(uid):
        parent=parent_n/'runs'/uid
        directory=read(parent/'directory.json');query=read(parent/'query.json');nr=read(parent/'result.json')
        question=read(out/'cases'/uid/'question.json')
        if query['id']!=uid or directory['video_id']!=question['video_id'] or query['question']!=question['question'] or query['options']!=question['options']:
            raise ValueError('Source archive question mismatch')
        archive=dict(question_id=uid,scope=query['scope'],captions=directory['captions'],
            scenes=[{k:s[k] for k in ('id','interval','caption_ids','description')} for s in directory['scenes']],
            inherited_N_budgeted_seconds=nr['cost_summary']['budgeted_total'],
            parent_files={str(parent/n):sha(parent/n) for n in ['directory.json','query.json','result.json']})
        if not 0<=archive['inherited_N_budgeted_seconds']<3000:
            raise ValueError('Insufficient remaining budget for new experiment')
        write(out/'cases'/uid/'archive.json',archive)
        binding=read(out/'cases'/uid/'source_binding.json')
        binding['files']['archive.json']=sha(out/'cases'/uid/'archive.json')
        write(out/'cases'/uid/'source_binding.json',binding)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(prepare_case,read(out/'cohort.json')))
    pre=read(out/'validation/source_preflight.json')
    pre['options_used_for_upstream_N_source_selection']=True
    pre['options_used_for_source_selection']=mode=='coverage_repair'
    write(out/'validation/source_preflight.json',pre)
    # Recopy source modules after preparation, and freeze these exact files.
    for name in ['script','script_core']:
        shutil.copytree(ROOT/name,out/'runtime'/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    capacity_preflight(out)
    write(out/'frozen_hashes.json',{str(p.relative_to(out)):sha(p) for p in out.rglob('*') if p.is_file()
        and p.name not in ('frozen_hashes.json','claims.json','report.json','queue_status.json')})
    verify_manifest(out)
    build_report(out)


def capacity_preflight(out):
    from transformers import AutoTokenizer
    from script_core.o_batch_generation import messages_for
    cfg=read(out/'qa_config.json')
    tokenizer=AutoTokenizer.from_pretrained(cfg['models']['vlm'],local_files_only=True)
    model_cfg=read(Path(cfg['models']['vlm'])/'config.json')
    native=model_cfg.get('max_position_embeddings') or model_cfg.get('text_config',{}).get('max_position_embeddings')
    if not native:raise ValueError('Unknown native capacity')
    maxima={};count=0
    def check(label,messages,uid):
        n=len(tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=True))
        if n>cfg['models']['max_input_tokens'] or n+1024>native:
            raise ValueError('Q3 full-source capacity exceeded: '+uid+' '+label+' '+str(n))
        if n>maxima.get(label,{}).get('tokens',0):maxima[label]=dict(tokens=n,question_id=uid)
    for uid in read(out/'cohort.json'):
        case=out/'cases'/uid;question=read(case/'question.json');archive=read(case/'archive.json');sources=read(case/'sentences.json')
        # Full closure is a conservative upper-bound input pool, not runtime
        # retrieval policy. Formal calls see only explicitly opened sources.
        for scene in list(r.available_scenes(archive,sources)):
            if scene['id'] in {s['id'] for s in r.available_scenes(archive,sources)}:
                sources=r.read_scene(archive,sources,scene['id'])
        for request in r.windows(question['question'],sources):check('filter',messages_for('q3_filter',request)[0],uid)
        obs=r.views(sources)
        request=dict(question=question['question'],options=question['options'],option_index=0,observations=obs)
        check('option_audit',messages_for('q3_option_audit',request)[0],uid)
        request=dict(question=question['question'],options=question['options'],observations=obs,
                     regions=r.temporal_regions(question['question'],archive['scope'],archive['scenes']))
        check('coverage',messages_for('q3_coverage',request)[0],uid)
        if obs:
            request=dict(question=question['question'],options=question['options'],observations=obs[:8],context=obs[8:],
                         witness_ids=[o['source_id'] for o in obs])
            check('reconsider',messages_for('q3_reconsider',request)[0],uid)
        chosen=[q.observation(s) for s in sources]
        for compact in [False,True]:
            final=q.final_input(read(case/'original_qa.json'),chosen,compact)
            check('reader_compact' if compact else 'reader',[
                dict(role='system',content=final['head']),dict(role='user',content=final['prompts'][0])],uid)
        count+=1
    write(out/'validation/capacity_preflight.json',dict(passed=True,cases=count,maxima=maxima,native=native,
        configured=cfg['models']['max_input_tokens'],output_reserve=1024,scope='full archive closure capacity only; no model generation or source selection'))


def build_report(out):
    policy=read(out/'experiment.json');ids=read(out/'cohort.json');gold=read(out/'evaluation_only.json')
    cases=[read(out/'runs'/uid/'result.json') for uid in ids if (out/'runs'/uid/'result.json').exists()]
    successful=[c for c in cases if c['complete'] and not c['error']]
    failed_paths=[p for p in (out/'attempts').glob('*/result.json') if read(p).get('error')]
    failed=[read(p) for p in failed_paths]
    setup=[read(p)['seconds'] for p in (out/'workers').glob('*/setup.json')]
    gates=[read(p)['cost_summary']['total'] for p in (out/'workers').glob('*/validation/functional.json')]
    reader_gates=[read(p)['cost_summary']['total'] for p in (out/'workers').glob('*/validation/reader_pair/result.json')]
    arms={}
    for arm in ['Q','Qplus']:
        n=len(successful)
        arms[arm]=dict(completed=n,correct=sum(str(c['arms'][arm]['answer']['selected_option'])==str(gold[c['question_id']]) for c in successful),
            mean_input_tokens=sum(c['arms'][arm]['input_tokens'] for c in successful)/n if n else None)
    result=dict(version=r.VERSION,plus_version=r.PLUS_VERSION,mode=policy['mode'],completed=len(successful),planned=len(ids),
        complete=len(successful)==len(ids),errors=sum(bool(c['error']) for c in cases)+len(failed),arms=arms,
        processing_seconds=sum(c['cost_summary']['total'] for c in cases),
        failed_attempt_processing_seconds=sum(c['cost_summary']['total'] for c in failed),
        failed_attempt_source_budgeted_seconds=sum(c['source_dependency_seconds'] for c in failed),
        setup_seconds=sum(setup),validation_processing_seconds=sum(gates)+sum(reader_gates),
        inherited_source_budgeted_seconds=sum(c['source_dependency_seconds'] for c in cases),
        scheduler_failure=read(out/'failure.json') if (out/'failure.json').exists() else None,
        failed_attempts=[str(p) for p in failed_paths],
        evaluation_scope=policy['evaluation_scope'],source_limitation=policy['source_limitation'],cases=cases,updated_at=now())
    write(out/'report.json',result)
    return result


def worker(out,worker_id):
    verify_manifest(out)
    from script.run_evidence_planner_experiment import verify_model
    from script_core.budget import ModelWorker
    from script.gpu_queue import acquire
    verify_model(out)
    folder=out/'workers'/str(worker_id)
    if folder.exists():raise ValueError('Worker already attempted')
    folder.mkdir(parents=True)
    def status(stage,**kwargs):write(folder/'status.json',dict(stage=stage,pid=os.getpid(),at=now(),**kwargs))
    cfg=read(out/'qa_config.json');policy=read(out/'experiment.json');backend=ModelWorker(cfg)
    def waiting(details):
        if (out/'failure.json').exists():raise RuntimeError('Another worker failed')
        status('waiting_for_gpu',**details)
    lease=acquire(policy['resource_policy'],waiting);entered=False
    try:
        gpu,_=lease.__enter__();entered=True
        os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid'];os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
        status('loading',gpu=gpu['index'])
        started=time.monotonic();details=backend.call('prepare_vlm',{},600)
        write(folder/'setup.json',dict(seconds=time.monotonic()-started,gpu=gpu,details=details))
        status('validation',gpu=gpu['index'])
        from script.q_repair_validation import validate_live
        validate_live(backend,cfg,folder/'validation')
        while True:
            item=base.claim(out,worker_id)
            if item is None:break
            index,uid=item;case=out/'cases'/uid
            for name,expected in read(case/'source_binding.json')['files'].items():
                if sha(case/name)!=expected:raise ValueError('Frozen case changed')
            def progress(phase,**detail):status('query',gpu=gpu['index'],index=index,question_id=uid,phase=phase,**detail)
            target=out/'attempts'/uid
            run_case(backend,cfg,read(case/'question.json'),read(case/'sentences.json'),read(case/'original_qa.json'),target,
                     read(case/'archive.json'),policy['mode'],progress)
            base.publish(out,uid,target)
            print('Q3 COMPLETE',worker_id,index,uid,flush=True)
        status('complete',gpu=gpu['index'])
    except BaseException as exc:
        base.fail(out,worker_id,exc);status('failed',error=str(exc));raise
    finally:
        backend.close(graceful=True)
        if entered:lease.__exit__(None,None,None)
        os.environ.pop('VIDEOAGENT_GPU_LEASE',None)


def supervise(out):
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);verify_manifest(out)
        if (out/'launch.json').exists():raise ValueError('Experiment already launched')
        processes=[];rows=[]
        for worker_id in range(read(out/'experiment.json')['workers']):
            command=[sys.executable,'-u',str(out/'runtime/script/run_q_repair_experiment.py'),'worker','--out',str(out),'--worker-id',str(worker_id)]
            with (out/f'worker-{worker_id}.log').open('w') as log:
                process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            processes.append(process);rows.append(dict(id=worker_id,pid=process.pid,identity=identity(process.pid),command=command))
        write(out/'launch.json',dict(controller_pid=os.getpid(),workers=rows,at=now()))
        while True:
            statuses={str(x['id']):read(out/'workers'/str(x['id'])/'status.json') for x in rows if (out/'workers'/str(x['id'])/'status.json').exists()}
            for process,row in zip(processes,rows):
                if process.poll() is not None and statuses.get(str(row['id']),{}).get('stage') not in ('complete','failed'):
                    base.fail(out,row['id'],RuntimeError('Q3 worker exited without terminal status'))
            with base.locked(out):report=build_report(out)
            alive=any(p.poll() is None for p in processes)
            state='running' if alive else 'complete' if report['complete'] and not report['scheduler_failure'] else 'failed'
            write(out/'queue_status.json',dict(state=state,controller_pid=os.getpid(),workers=statuses,effective_worker_count=len(rows),
                allowed_gpu_indices=['0','1'],updated_at=now(),error=report['scheduler_failure']))
            if not alive:return
            time.sleep(5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','worker','supervise'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--source',type=Path);parser.add_argument('--parent-n',type=Path)
    parser.add_argument('--workers',type=int,default=2);parser.add_argument('--worker-id',type=int)
    parser.add_argument('--mode',choices=['filter_only','coverage_repair'],default='filter_only')
    args=parser.parse_args();out=args.out.resolve()
    if args.action=='prepare':prepare(out,args.source.resolve(),args.parent_n.resolve(),args.workers,args.mode)
    elif args.action=='worker':worker(out,args.worker_id)
    else:supervise(out)
