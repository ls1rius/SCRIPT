"""Continue the single OOM navigation call under its original paid budget."""
import argparse
import copy
import fcntl
import inspect
import json
import os
from pathlib import Path
import shutil
import sys
import time


def read(p):return json.loads(Path(p).read_text())


def write(p,v):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)


def sha(p):
    import hashlib
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def pending_remaining(out):
    claims=read(out/'claims.json')
    return sum(u not in claims for u in read(out/'cohort.json'))+sum(bool(r.get('recovery_pending')) for r in claims.values())


def recovery_claim(out,wid):
    from script import extend_q4_500 as ext
    from script.scale_live_parallel_pool import now
    with ext.locked(out):
        if (out/'failure.json').exists():return None
        claims=read(out/'claims.json')
        for uid,row in claims.items():
            if row.get('recovery_pending'):
                if (out/'attempts'/uid).exists():raise ValueError('Q4 attempt must not be retried')
                row.update(recovery_pending=False,worker_id=wid,pid=os.getpid(),resumed_at=now())
                write(out/'claims.json',claims);return row['index'],uid
    return ext.claim(out,wid)


def generated_search(original):
    before='from .budget import BudgetLedger, Meter, ResourceExhausted, ToolFailure, exploration_exhausted'
    if original.count(before)!=1:raise ValueError('Search import layout changed')
    original=original.replace(before,before.replace(', Meter',''),1)
    before="    meter = Meter(backend, ledger, cfg, target/'raw_calls')"
    original=original.replace(before,"    restore_ledger(ledger, RESUME_STATE)\n"+before,1)
    start=original.index('    try:\n        if m_enabled:')
    end=original.index('        for step in (range(10) if opened else []):',start)
    resume='''    try:
        if not m_enabled:raise ValueError('Resume requires frozen N navigation')
        result=deepcopy(RESUME_STATE['result']);result['error']=None
        opened=deepcopy(RESUME_STATE['failed']['parameters']['opened_scenes'])
        reviews=deepcopy(RESUME_STATE['result']['reviews'])
        incomplete=deepcopy(RESUME_STATE['result']['incomplete'])
        ranked=deepcopy(result['retrieval']['candidate_order'])
        scope=dict(scope='question_driven',reason='Concrete semantic gaps; no temporal coverage gate')
        navigation_events=deepcopy(RESUME_STATE['events'])
        sufficiency_feedback=deepcopy(RESUME_STATE['failed']['parameters'].get('sufficiency_feedback',[]))
        local_scope_exhausted=RESUME_STATE['failed']['parameters']['local_scope_exhausted']
        previous_rejections=len(memory['rejected_fields'])-result['rejected_field_count']
        for step in range(len(reviews),10):'''
    original=original[:start]+resume+original[end+len('        for step in (range(10) if opened else []):'):]
    compile(original,'oom-search-resume','exec');return original


def restore_ledger(ledger,state):
    cost=state['result']['cost_summary']
    if ledger.total_cap!=cost['total_cap'] or cost['initialization'] or cost['shared_dependency_seconds'] or cost['budget_violation']:
        raise ValueError('Original search budget cannot be restored')
    if abs(sum(c['actual_cost'] for c in state['calls'])-cost['online'])>1e-6:raise ValueError('Historical call cost mismatch')
    ledger.actual=cost['online'];ledger.calls=copy.deepcopy(state['calls'])


def resume_search(out,control,backend,cfg,target):
    from script_core import adaptive_transition as adaptive
    from script_core.budget import Meter
    from script_core.config import digest
    state=read(control/'oom_state.json');source=target.parent
    if read(target/'result.json')!=state['result'] or sha(target/'calls.jsonl')!=state['calls_sha256']:
        raise ValueError('Failed search checkpoint changed or already resumed')
    if list(source.rglob('*.pending.json')):raise ValueError('Pending call prohibits continuation')
    packet=read(out/'source_inputs'/f"{state['uid']}.json");directory=read(source/'directory.json')
    if digest(packet)!=state['packet_hash'] or digest(cfg)!=state['config_hash']:raise ValueError('Source packet/config changed')
    if sha(target/'network.json')!=state['memory_sha256'] or read(target/'network.json')!=read(source/'partial_memory.json'):
        raise ValueError('Navigation memory checkpoint changed')
    class ResumeMeter(Meter):
        def _call(self,operation,inputs,*args,**kwargs):
            if not getattr(self,'resume_checked',False):
                failed=state['failed']
                if operation!=failed['operation'] or digest(inputs)!=failed['input_hash']:
                    raise ValueError('First continued model request differs from failed frozen request')
                self.resume_checked=True
                write(target/'oom_first_call.json',dict(passed=True,operation=operation,input_hash=digest(inputs),
                    historical_seconds=self.ledger.actual,historical_failed_seconds=failed['actual_cost']))
            return super()._call(operation,inputs,*args,**kwargs)
    scope=dict(vars(adaptive),RESUME_STATE=state,restore_ledger=restore_ledger,Meter=ResumeMeter)
    exec(compile((control/'resume_search.py').read_text(),str(control/'resume_search.py'),'exec'),scope)
    result=scope['run'](packet,directory,packet['frames'],backend,cfg,target,dict(online_seconds=cfg['budget']['online_cap']),
                        memory_path=source/'partial_memory.json',evidence_only=True)
    if result.get('error'):raise RuntimeError('Continued search failed: '+str(result['error']))
    if not (target/'oom_first_call.json').exists():raise ValueError('Continuation never verified its first request')
    write(target/'oom_continuation.json',dict(control=str(control),historical_processing_seconds=state['result']['cost_summary']['total'],
        historical_failed_seconds=state['failed']['actual_cost'],successful_model_calls_repeated=0,
        model_calls_new=len([c for c in read_calls(target/'calls.jsonl')[len(state['calls']):] if not c['parameters'].get('in_process')]),
        total_budgeted_seconds=result['cost_summary']['budgeted_total']))
    return result


def read_calls(path):return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def source_answer(packet,features,cfg,backend,target,resume=False):
    from script_core.scheme4 import answer
    control=Path(__file__).resolve().parent;meta=read(control/'oom_state.json')
    if packet['question']['question_id']==meta['uid']:
        resume_search(Path(read(control/'config.json')['out']),control,backend,cfg,target/'partial_search')
    return answer(packet,features,cfg,backend,target,resume=resume)


def prepare(out,control):
    sys.path.insert(0,str(out/'runtime'))
    from script import extend_q4_500 as ext
    from script.scale_live_parallel_pool import identity,now,verify_manifest
    from script_core import adaptive_transition as adaptive
    from script_core.config import digest
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        queue=read(out/'queue_status.json');old=Path(queue['resident_control'])
        if queue['state']!='failed' or identity(queue['controller_pid']) is not None:raise ValueError('Controller must be stopped')
        for row in read(old/'workers_launch.json'):
            if identity(row['pid']) is not None:raise ValueError('Old worker still running')
        ext.verify(out);verify_manifest(old);control.mkdir()
        for n in ['queue_status.json','report.json','claims.json','failure.json']:shutil.copy2(out/n,control/('before_'+n))
        published={str(p):sha(p) for p in (out/'runs').glob('*/result.json')}
        if len(published)!=466:raise ValueError('Expected 466 immutable published results')
        write(control/'preserved_hashes.json',dict(published=published,frozen={str(out/'frozen_hashes.json'):sha(out/'frozen_hashes.json')},training={}))
        config=dict(kind='untrained',out=str(out),control=str(control),old_control=str(old),workers=[24,25,26,27],
                    maximum_workers=8,allowed_gpu_indices=['0','1','2','3'],at=now())
        write(control/'config.json',config)
        for n in ['model_process.py','sparse_visual.py']:shutil.copy2(old/n,control/n)
        code=(old/'resident_q4_500.py').read_text()
        code=code.replace("    return sum(uid not in claims for uid in read(out/'cohort.json'))", "    return sum(uid not in claims for uid in read(out/'cohort.json')) + sum(bool(r.get('recovery_pending')) for r in claims.values())",1)
        (control/'resident_q4_500.py').write_text(code)
        code=(old/'question_worker.py').read_text();before='result=answer(packet,features,cfg,backend,target,resume=target.exists());'
        if code.count(before)!=1:raise ValueError('Worker source call changed')
        (control/'question_worker.py').write_text(code.replace(before,before.replace('result=answer(','result=source_answer('),1))
        code=(old/'resume_q4_failures.py').read_text()
        before='        scope=dict(vars(ext),ResidentSession=resident.ResidentSession,QueueDrained=resident.QueueDrained,'
        after='        from oom_support import source_answer, recovery_claim\n        scope=dict(vars(ext),source_answer=source_answer,ResidentSession=resident.ResidentSession,QueueDrained=resident.QueueDrained,'
        if code.count(before)!=1:raise ValueError('Worker scope layout changed')
        code=code.replace(before,after,1).replace('launch(16+len(rows))','launch(24+len(rows))')
        (control/'resume_q4_failures.py').write_text(code)
        shutil.copy2(Path(__file__),control/'oom_support.py')
        (control/'resume_search.py').write_text(generated_search(inspect.getsource(adaptive.run)))
        with ext.locked(out):
            claims=read(out/'claims.json');unfinished=[(u,r) for u,r in claims.items() if r['state']!='complete']
            if len(unfinished)!=4:raise ValueError('Unexpected pending question count')
            damaged=[u for u,r in unfinished if not (out/'source_runs'/u/'result.json').exists()]
            if len(damaged)!=1:raise ValueError('Expected exactly one incomplete source')
            uid=damaged[0];source=out/'source_attempts'/uid/'run';target=source/'partial_search';calls=read_calls(target/'calls.jsonl')
            failures=[c for c in calls if c['error']]
            if len(failures)!=1 or failures[0]['operation']!='m_transition_next' or 'OutOfMemoryError' not in failures[0]['error']:
                raise ValueError('Unexpected failed navigation call')
            failed=failures[0];raw=read(failed['output_ref']);result=read(target/'result.json')
            if digest(raw['inputs'])!=failed['input_hash'] or not raw['error']:raise ValueError('Failed raw request mismatch')
            if result['reviews']!=failed['parameters']['previous_reviews'] or len(result['reviews'])>=10:raise ValueError('Unusable navigation checkpoint')
            if list(source.rglob('*.pending.json')) or (source/'partial_temporal').exists():raise ValueError('Unexpected pending or downstream work')
            if any(not c['parameters'].get('in_process') for c in calls[calls.index(failed)+1:]):raise ValueError('Model work followed the failure')
            shutil.copytree(source,control/'failed_source_snapshot')
            trace=read(target/'navigation_trace.json');events=trace['events']
            if events[-1]['event']!='stopped':raise ValueError('Missing terminal checkpoint')
            state=dict(uid=uid,result=result,calls=calls,calls_sha256=sha(target/'calls.jsonl'),failed=failed,events=events[:-1],
                memory_sha256=sha(target/'network.json'),packet_hash=digest(read(out/'source_inputs'/f'{uid}.json')),
                config_hash=digest(read(out/'source_config.json')))
            write(control/'oom_state.json',state)
            write(control/'preserved_source_hashes.json',{str(p):sha(p) for u,r in unfinished for p in (out/'source_runs'/u).rglob('*') if p.is_file()})
            for u,row in unfinished:
                if (out/'attempts'/u).exists():raise ValueError('Q4 attempt already exists')
                row.update(previous_owner=dict(worker_id=row['worker_id'],pid=row['pid']),worker_id=-1,pid=None,
                           recovery_pending=True,recovery_control=str(control),handoff_at=now())
            write(out/'claims.json',claims)
        write(control/'frozen_hashes.json',{p.name:sha(p) for p in control.iterdir() if p.is_file()})
        (out/'failure.json').unlink()
        write(out/'queue_status.json',dict(state='prepared',controller_pid=None,resident_control=str(control),allowed_gpu_indices=['0','1','2','3'],updated_at=now()))
        print('OOM_RECOVERY_PREPARED',control,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--control',type=Path,required=True)
    a=p.parse_args();prepare(a.out.resolve(),a.control.resolve())
