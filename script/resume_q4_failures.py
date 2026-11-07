"""Frozen continuation of Q4 queues, preserving published answers and fitted LoRA."""
import argparse
import copy
import fcntl
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback


def read(p):
    return json.loads(Path(p).read_text())


def write(p, value):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)


def sha(p):
    import hashlib
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def now():
    from datetime import datetime,timezone,timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def boot(out,control):
    sys.path.insert(0,str(out/'runtime'));sys.path.insert(0,str(control))


def tree(root):
    return {str(p.relative_to(root)):sha(p) for p in root.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts}


def verify(control):
    from script.scale_live_parallel_pool import verify_manifest
    verify_manifest(control)


def recover_optional_plan(out,control,uid):
    """Resume AFTER an unusable optional plan, without replaying any model call."""
    from script_core.config import digest
    from script_core.adaptive_transition import memory_context,validate_memory
    from script_core.temporal_pipeline import qa_context_inputs
    from script_core.budget import BudgetLedger
    source=out/'source_attempts'/uid/'run';target=source/'partial_search'
    result=read(target/'result.json');error=result.get('error',{})
    if error.get('type')!='ToolFailure' or not error.get('message','').startswith('Invalid sparse_visual_plan JSON:'):
        raise ValueError('Continuation requires the exact optional-plan failure')
    if (source/'result.json').exists() or (source/'partial_temporal').exists() or list(source.rglob('*.pending.json')):
        raise ValueError('Unexpected work after failed optional plan')
    packet=read(out/'source_inputs'/f'{uid}.json');cfg=read(out/'source_config.json')
    if read(source/'query.json')['id']!=uid:raise ValueError('Question mismatch')
    directory=read(source/'directory.json');memory=read(target/'network.json')
    validate_memory(memory,uid,packet['captions'],memory['extraction_hash'])
    if memory!=read(source/'partial_memory.json'):raise ValueError('Checkpoint memory differs')
    calls=[json.loads(l) for l in (target/'calls.jsonl').read_text().splitlines()]
    plans=[r for r in calls if r['operation']=='sparse_visual_plan']
    if len(plans)!=1:raise ValueError('Expected exactly one optional plan')
    plan=plans[0];raw=read(plan['output_ref'])
    if digest(raw['inputs'])!=plan['input_hash'] or digest(raw['output'])!=plan['output_hash']:
        raise ValueError('Failed plan raw/hash mismatch')
    if raw['error'] or not isinstance(raw['output']['data'],str):raise ValueError('Unexpected failed output')
    try:json.loads(raw['output']['data'],strict=False)
    except json.JSONDecodeError:pass
    else:raise ValueError('Plan is valid JSON; continuation not authorized')
    if any(not r['parameters'].get('in_process') for r in calls[calls.index(plan)+1:]):
        raise ValueError('A model call followed the unusable plan')
    if result['stop_reason'] not in ('step_limit','candidate_exhausted','scene_limit','no_useful_expansion','sufficient','exploration_budget','empty_extraction_scope'):
        raise ValueError('Navigation did not reach a terminal checkpoint')
    scenes={s['id']:s for s in directory['scenes']};opened=result['opened_scenes']
    if opened!=result['retrieval']['candidate_order'][:len(opened)]:raise ValueError('Candidate prefix changed')
    selected=sorted(i for sid in opened for i in scenes[sid]['chronological_order'])
    snapshot=control/'failed_source_snapshot'
    shutil.copytree(source,snapshot)
    write(control/'failed_source_hashes.json',tree(snapshot))
    began=time.monotonic();cpu=time.process_time()
    diagnostic=read(target/'sparse_visual.json')
    if diagnostic['observations'] or diagnostic['frame_ids'] or diagnostic['triggered']:
        raise ValueError('Unusable visual plan must have no visual observations')
    diagnostic.update(errors=[error['message']],skip_reason='Rejected malformed optional visual plan; retained summary evidence',
                      rejected_plan_call_id=plan['call_id'])
    ledger=BudgetLedger(cfg['budget']['online_cap'],answer_reserve=cfg['budget']['final_answer_reserve'],calls_path=target/'calls.jsonl')
    old=result['cost_summary']
    if abs(sum(r['actual_cost'] for r in calls)-old['online'])>1e-6 or old['shared_dependency_seconds']:
        raise ValueError('Historical search cost cannot be reconstructed exactly')
    ledger.calls=calls;ledger.actual=old['online'];ledger.budget_violation=old['budget_violation']
    query=dict(packet['question'],id=uid)
    bundle=dict(schema_version=1,packet_hash=digest(packet),memory=copy.deepcopy(memory),opened_scenes=opened,
                selected_seconds=selected,supplemental_visual=diagnostic,source_incomplete=result['incomplete'],
                qa_input=qa_context_inputs(query,packet['captions'],selected,memory_context(memory),visual_observations=[]))
    ledger.record('optional_visual_plan_recovery',dict(in_process=True),ledger.remaining,began,
                  output=dict(usage=dict(cpu_seconds=time.process_time()-cpu,gpu_resident_seconds=0.)))
    result.update(error=None,evidence_only=True,selected_seconds=selected,supplemental_visual=diagnostic,
                  cost_summary=ledger.summary(),recovery=dict(control=str(control),rejected_plan_call_id=plan['call_id'],
                  model_calls_repeated=0,historical_plan_seconds=plan['actual_cost']))
    if result['cost_summary']['budget_violation']:raise ValueError('Recovered search budget exceeded')
    bundle['preparation_seconds']=result['cost_summary']['budgeted_total']
    write(target/'sparse_visual.json',diagnostic);write(target/'evidence_bundle.json',bundle);write(target/'result.json',result)
    write(control/'source_recovery.json',dict(question_id=uid,original_error=error,original_cost=old,
          resumed_cost=result['cost_summary'],model_calls_repeated=0,original_plan=plan,at=now()))


def prepare(out,control,kind):
    if control.exists():raise ValueError('Recovery control already exists')
    old=out/'resident-20261106-r1';control.mkdir()
    boot(out,control)
    from script.scale_live_parallel_pool import identity,verify_manifest
    from script.extend_q4_500 import locked
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        q=read(out/'queue_status.json')
        if q.get('state')!='failed' or identity(q['controller_pid']) is not None:
            raise ValueError('Queue must be failed and its controller exited')
        for row in read(old/'workers_launch.json'):
            if identity(row['pid']) is not None:raise ValueError('Old worker still alive')
        if kind=='untrained':
            import script.extend_q4_500 as ext
            ext.verify(out)
        else:
            import script.train_q4_retention as trainer
            trainer.verify(out);verify_manifest(out/'evaluation')
            fitted=read(out/'training/result.json');adapter=read(out/'adapter/adapter_config.json')
            if not fitted['complete'] or adapter['weights_sha256']!=sha(out/'adapter/adapter.safetensors'):
                raise ValueError('Saved adapter failed validation')
            if len(read(out/'teacher_claims.json'))!=200 or any(r['state']!='complete' for r in read(out/'teacher_claims.json').values()):
                raise ValueError('Teacher work incomplete')
        write(control/'config.json',dict(kind=kind,out=str(out),control=str(control),old_control=str(old),
              workers=[16,17,18,19] if kind=='untrained' else [4,5,6,7],maximum_workers=8 if kind=='untrained' else 4,
              allowed_gpu_indices=['0','1','2','3'],at=now()))
        shutil.copy2(Path(__file__),control/'resume_q4_failures.py')
        write(control/'queue_before.json',q)
        write(control/'preserved_hashes.json',dict(published={str(p):sha(p) for p in (out/'runs').glob('*/result.json')},
              frozen={str(out/'frozen_hashes.json'):sha(out/'frozen_hashes.json')},
              training={str(p):sha(p) for name in ['training','adapter','teacher_runs'] for p in (out/name).rglob('*') if p.is_file()} if kind=='trained' else {}))
        for name in ['failure.json','report.json']:
            if (out/name).exists():shutil.copy2(out/name,control/('before_'+name))
        if kind=='untrained':
            for name in ['resident_q4_500.py','model_process.py']:
                shutil.copy2(old/name,control/name)
            code=(old/'question_worker.py').read_text()
            before='result=answer(packet,features,cfg,backend,target);'
            if code.count(before)!=1:raise ValueError('Source call layout changed')
            code=code.replace(before,'result=answer(packet,features,cfg,backend,target,resume=target.exists());')
            (control/'question_worker.py').write_text(code)
            original=(out/'runtime/script_core/sparse_visual.py').read_text()
            before='    except (ResourceExhausted,ToolFailure) as exc:\n        if not exploration_exhausted(exc):raise\n'
            patch='''    except (ResourceExhausted,ToolFailure) as exc:
        if isinstance(exc,ToolFailure) and str(exc).startswith('Invalid sparse_visual_plan JSON:'):
            diagnostic['errors'].append(str(exc))
            diagnostic['skip_reason']='Rejected malformed optional visual plan; retained summary evidence'
            return diagnostic
        if not exploration_exhausted(exc):raise
'''
            if original.count(before)!=1:raise ValueError('Visual fallback layout changed')
            (control/'sparse_visual.py').write_text(original.replace(before,patch,1))
            with locked(out):
                claims=read(out/'claims.json');write(control/'claims_before.json',claims)
                unfinished=[(u,r) for u,r in claims.items() if r['state']!='complete']
                if len(unfinished)!=3:raise ValueError('Unexpected unfinished source count')
                for wid,(uid,row) in zip([16,17,18],unfinished):
                    if (out/'attempts'/uid).exists():raise ValueError('Q4 attempt already started')
                    if not (out/'source_runs'/uid/'result.json').exists():recover_optional_plan(out,control,uid)
                    row.update(previous_owner=dict(worker_id=row['worker_id'],pid=row['pid']),worker_id=wid,pid=None,
                               recovery_control=str(control),handoff_at=now())
                write(out/'claims.json',claims)
        else:
            for name in ['resident_train_q4.py','status.py']:
                shutil.copy2(old/name,control/name)
            code=(old/'evaluation_worker.py').read_text()
            before='gpu,_=lease.__enter__();entered=True'
            if code.count(before)!=1:raise ValueError('Evaluation unpack layout changed')
            (control/'evaluation_worker.py').write_text(code.replace(before,'gpu=lease.__enter__();entered=True',1))
            target=out/'evaluation'
            if read(target/'claims.json') or list((target/'attempts').iterdir()) or list((target/'runs').iterdir()):
                raise ValueError('Evaluation has already advanced')
            for name in ['failure.json','queue_status.json','report.json']:
                if (target/name).exists():shutil.copy2(target/name,control/('evaluation_before_'+name))
            write(control/'training_reuse.json',dict(selected_epoch=fitted['selected_epoch'],adapter=adapter,
                  training_repeated=False,teacher_repeated=False,at=now()))
        # Clear active stop flags only after their complete records are archived.
        for folder in [out]+([out/'evaluation'] if kind=='trained' else []):
            if (folder/'failure.json').exists():(folder/'failure.json').unlink()
        write(control/'frozen_hashes.json',{p.name:sha(p) for p in control.iterdir() if p.is_file()})
        write(out/'queue_status.json',dict(state='prepared',stage='evaluation300' if kind=='trained' else 'resume',
              controller_pid=None,resident_control=str(control),allowed_gpu_indices=['0','1','2','3'],updated_at=now()))
    print('PREPARED',control,flush=True)


def recovery_claim(out,wid):
    from script import extend_q4_500 as ext
    with ext.locked(out):
        if (out/'failure.json').exists():return None
        claims=read(out/'claims.json')
        for uid,row in claims.items():
            if row['state']=='running' and row['worker_id']==wid:
                if (out/'attempts'/uid).exists():raise ValueError('Refusing Q4 attempt retry')
                row['pid']=os.getpid();write(out/'claims.json',claims)
                return row['index'],uid
    return ext.claim(out,wid)


def worker(control,wid):
    cfg=read(control/'config.json');out=Path(cfg['out']);boot(out,control);verify(control)
    if cfg['kind']=='untrained':
        from script import extend_q4_500 as ext
        import resident_q4_500 as resident
        from script_core import sparse_visual,budget
        exec(compile((control/'sparse_visual.py').read_text(),str(control/'sparse_visual.py'),'exec'),sparse_visual.__dict__)
        budget._worker=resident.resident_process
        scope=dict(vars(ext),ResidentSession=resident.ResidentSession,QueueDrained=resident.QueueDrained,
                   work_remaining=resident.remaining,owned=resident.owned,resident_claim=recovery_claim)
        exec(compile((control/'question_worker.py').read_text(),str(control/'question_worker.py'),'exec'),scope)
        scope['worker'](out,wid)
    else:
        import resident_train_q4 as resident
        from script import train_q4_retention as trainer
        from script.gpu_queue import acquire
        from script_core.budget import ModelWorker
        target=out/'evaluation';qmod=trainer.runtime(out);backend=None
        folder=control/'workers'/str(wid)
        def waiting(d):
            if (out/'failure.json').exists() or (target/'failure.json').exists():raise RuntimeError('Evaluation pool halted')
            if len(read(target/'claims.json'))==300:raise resident.PipelineDrained()
            write(folder/'status.json',dict(stage='waiting_for_gpu',pid=os.getpid(),at=now(),**d))
        try:
            with acquire(read(target/'experiment.json')['resource_policy'],waiting) as (gpu,_):
                os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid'];os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
                backend=ModelWorker(read(target/'qa_config.json'))
                def held(policy,on_wait):return resident.borrowed(gpu,policy['minimum_free_mib'])
                scope=dict(vars(qmod),SHARED_BACKEND=backend,borrowed_evaluation_lease=held)
                exec(compile((control/'evaluation_worker.py').read_text(),str(control/'evaluation_worker.py'),'exec'),scope)
                scope['worker'](target,wid)
        except resident.PipelineDrained:
            write(folder/'status.json',dict(stage='complete',reason='queue_drained_before_admission',pid=os.getpid(),at=now()))
        except BaseException as exc:
            qmod.base.fail(target,wid,exc);raise
        finally:
            if backend is not None:backend.close(graceful=True)
            os.environ.pop('VIDEOAGENT_GPU_LEASE',None)


def supervise(control):
    cfg=read(control/'config.json');out=Path(cfg['out']);boot(out,control);verify(control)
    from script.scale_live_parallel_pool import identity
    from script import extend_q4_500 as ext
    kind=cfg['kind'];target=out if kind=='untrained' else out/'evaluation'
    if kind=='untrained':ext.verify(out)
    else:
        from script import train_q4_retention as trainer
        trainer.verify(out);qmod=trainer.runtime(out)
        scope=dict(vars(trainer));exec((control/'status.py').read_text(),scope);training_status=scope['status']
    rows=[];children={}
    env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
             CUDA_DEVICE_ORDER='PCI_BUS_ID',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
    with (out/'controller.lock').open('a') as lock, (target/'recovery_controller.lock').open('a') as targetlock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(targetlock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (control/'launch.json').exists():raise ValueError('Recovery already launched')
        write(control/'launch.json',dict(pid=os.getpid(),identity=identity(os.getpid()),at=now()))
        def launch(wid):
            cmd=[sys.executable,'-u',str(control/'resume_q4_failures.py'),'worker','--control',str(control),'--worker-id',str(wid)]
            with (control/f'worker-{wid}.log').open('x') as log:
                p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True)
            row=dict(id=wid,pid=p.pid,identity=identity(p.pid),command=cmd);rows.append(row);children[p.pid]=p
            write(control/'workers_launch.json',rows)
        for wid in cfg['workers']:launch(wid)
        while True:
            statuses={}
            for row in rows:
                f=target/'workers'/str(row['id'])/'status.json'
                if not f.exists():f=control/'workers'/str(row['id'])/'status.json'
                if f.exists():statuses[str(row['id'])]=read(f)
                code=children[row['pid']].poll()
                if code is not None and (code!=0 or statuses.get(str(row['id']),{}).get('stage')!='complete'):
                    ext.fail(target,row['id'],RuntimeError('Recovery worker failed; see worker log'))
            with ext.locked(target):report=ext.build_report(out) if kind=='untrained' else qmod.build_report(target)
            report.update(resident_control=str(control),resident_models=True,parallel_worker_count=len(rows),historical_failure=str(control/'before_failure.json'))
            write(target/'report.json',report)
            live=any(p.poll() is None for p in children.values())
            state='running' if live else 'complete' if report['complete'] and not report.get('scheduler_failure') else 'failed'
            snapshot=dict(state=state,controller_pid=os.getpid(),workers=statuses,effective_worker_count=len(rows),
                          configured_maximum_worker_count=cfg['maximum_workers'],allowed_gpu_indices=['0','1','2','3'],
                          resident_control=str(control),autoscaler=str(control),resident_models=True,updated_at=now(),
                          error=read(target/'failure.json') if (target/'failure.json').exists() else None)
            write(target/'queue_status.json',snapshot)
            if kind=='trained':
                training_status(out,'complete' if state=='complete' else 'failed' if state=='failed' else 'evaluation300',
                                resident_control=str(control),workers=statuses)
                write(out/'queue_status.json',dict(read(out/'queue_status.json'),state=state))
                if state=='complete':
                    trainer.compare(out);r=read(out/'report.json');r.update(complete=True,comparison=read(out/'comparison.json'));write(out/'report.json',r)
            if not live:return
            # Initial workers listen to all four GPUs; add further residents only
            # after every current worker has admitted and another 32GiB fits.
            if kind=='untrained' and len(rows)<8 and not (out/'failure.json').exists():
                import resident_q4_500 as resident
                if resident.remaining(out) and len(statuses)==len(rows) and all(s['stage'] in ('query','loading','validation') for s in statuses.values()):
                    _,free,_=resident.gpu_snapshot()
                    if max(free.values(),default=0)>=32768:launch(16+len(rows))
            time.sleep(10)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','worker','supervise'])
    p.add_argument('--control',type=Path,required=True);p.add_argument('--out',type=Path)
    p.add_argument('--kind',choices=['untrained','trained']);p.add_argument('--worker-id',type=int)
    a=p.parse_args();control=a.control.resolve()
    if a.action=='prepare':prepare(a.out.resolve(),control,a.kind)
    elif a.action=='worker':worker(control,a.worker_id)
    else:supervise(control)
