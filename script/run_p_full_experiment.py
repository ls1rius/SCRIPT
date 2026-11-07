"""Continue a frozen P diagnostic across the original 300 N-source questions."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import controlled_memory_io as io
from script import run_p_sentence_experiment as single
from script import run_o_full_experiment as source
from script_core import p_sentence_filter as p


def report(out):
    policy = io.read(out/'experiment.json')
    gold = io.read(out/'evaluation_only.json')
    inherited = set(policy['inherited_case_ids'])
    resumed = set(policy.get('resumed_success_case_ids', []))
    carried = inherited | resumed
    rows = [io.read(out/'runs'/q/'result.json') for q in policy['case_ids']
            if (out/'runs'/q/'result.json').exists()]
    success = [r for r in rows if r['complete'] and not r['error']]
    def scores(items):
        return dict(completed=len(items), correct=sum(
            str(r['answer']['selected_option']) == str(gold[r['question_id']]) for r in items))
    result = dict(version=policy['version'], planned=len(policy['case_ids']), **scores(success),
        complete=len(success) == len(policy['case_ids']), errors=sum(bool(r['error']) for r in rows),
        cases=rows, updated_at=io.now(), scope=policy['scope'],
        subgroups={name:scores([r for r in success if (r['question_id'] in inherited) == known])
                  for name,known in [('known_demos',True),('additional',False)]},
        new_processing_seconds=sum(r['cost_summary']['total'] for r in rows if r['question_id'] not in carried),
        inherited_successful_processing_seconds=sum(r['cost_summary']['total'] for r in rows if r['question_id'] in carried),
        version_subgroups={version:scores([r for r in success if r.get('version',policy['version'])==version])
                           for version in sorted({r.get('version',policy['version']) for r in success})},
        historical_failed_processing_seconds=sum(x['processing_seconds'] for x in policy.get('historical_failed_attempts',[])),
        inherited_selection_dependency_seconds=sum(r.get('inherited_selection_seconds',0.) for r in rows),
        historical_failed_call_seconds=policy.get('historical_failed_call_seconds',0.),
        reader_review_seconds=sum(r.get('reader_review_seconds',0) for r in success),
        reader_total_seconds=sum(r.get('reader_total_seconds',r.get('answer_seconds',0)) for r in success),
        mean_input_tokens=sum(r['input_tokens'] for r in success)/len(success) if success else None,
        mean_qa_seconds=sum(r['answer_seconds'] for r in success)/len(success) if success else None,
        task_brief_generated=False, composition_called=False, training=False)
    io.write(out/'report.json',result)
    return result


def prepare(out,parent,diagnostic):
    from script.run_m_scene_experiment import verify as verify_parent
    verify_parent(parent);single.verify(diagnostic)
    old=io.read(diagnostic/'report.json')
    if not old['complete'] or old['errors'] or old['version'] != p.VERSION:
        raise ValueError('Require completed diagnostic for current P version')
    # Freeze exactly the tested runtime, not incidental later workspace changes.
    for name in ['script_core/p_sentence_filter.py','script_core/o_batch_generation.py',
                 'script/run_p_sentence_experiment.py','script/p_repair_validation.py',
                 'script/run_o_full_experiment.py','script/gpu_shared_queue.py']:
        if io.sha(ROOT/name) != io.sha(diagnostic/'runtime'/name):
            raise ValueError('Tested runtime mismatch: '+name)
    ids=io.read(parent/'cohort.json');known=io.read(diagnostic/'cohort.json')
    if len(ids)!=300 or len(set(ids))!=300 or not set(known)<=set(ids):
        raise ValueError('Expected original 300-question cohort')
    if out.exists():raise ValueError('Use a new full P directory')
    before={str(f):io.sha(f) for f in diagnostic.rglob('*') if f.is_file()}
    out.mkdir(parents=True)
    for name in ['qa_config.json','qa_expected_artifacts.json']:
        shutil.copy2(diagnostic/name,out/name)
    cfg=io.read(out/'qa_config.json')
    if cfg['query']['tool_transient_retries'] or cfg['query']['json_repair_retries']:
        raise ValueError('No hidden retries allowed')
    for name in ['cohort.json','evaluation_only.json']:shutil.copy2(parent/name,out/name)
    for uid in ids:
        q=io.read(parent/'inputs'/f'{uid}.json')['question']
        if q['question_id']!=uid:raise ValueError('Question identity mismatch')
        io.write(out/'questions'/f'{uid}.json',q)
    policy=dict(version=p.VERSION,case_ids=ids,parent_N=str(parent),diagnostic=str(diagnostic),
        inherited_case_ids=known,planned=300,full_cohort=True,
        resource_policy=io.read(diagnostic/'experiment.json')['resource_policy'],
        scope='Fixed N-selected sources, same original 300 questions; three known P demos inherited and separately reported, remaining 297 newly extracted/filtered/answered. No independent navigation claim.',
        new_navigation=False,training=False,adapter=None,task_brief=False,composition=False,
        filter_options=False,answer_options=True,batch_size=4,no_automatic_retries=True,
        source_policy='Original order; atomically freeze completed N sources; release model and lease while waiting; stop on upstream failure.',
        created_at=io.now())
    io.write(out/'experiment.json',policy)
    ready=0
    for uid in ids:
        if not source.prepare_case(out,uid):break
        ready+=1
    for uid in known:
        if not source.prepare_case(out,uid):raise ValueError('Inherited source not ready')
        for name in ['question.json','sentences.json','original_qa.json']:
            if io.read(out/'cases'/uid/name)!=io.read(diagnostic/'cases'/uid/name):
                raise ValueError('Inherited P source differs: '+uid+'/'+name)
        if io.read(diagnostic/'evaluation_only.json')[uid]!=io.read(out/'evaluation_only.json')[uid]:
            raise ValueError('Inherited label mismatch')
        shutil.copytree(diagnostic/'runs'/uid,out/'runs'/uid)
    shutil.copytree(diagnostic/'runtime',out/'runtime',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(Path(__file__),out/'runtime/script/run_p_full_experiment.py')
    io.write(out/'validation/preflight.json',dict(passed=True,ready_source_cases=ready,
        inherited_cases=len(known),fresh_cases=300-len(known),diagnostic_hashes=before,
        fresh_generic_gate=True,model_loaded=False))
    if any(io.sha(Path(f))!=sha for f,sha in before.items()):raise ValueError('Diagnostic changed')
    verify_parent(parent)
    io.write(out/'frozen_hashes.json',{str(f.relative_to(out)):io.sha(f) for f in out.rglob('*') if f.is_file()})
    single.verify(out);report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    print('P FULL PREPARED',out,'ready',ready,'inherited',len(known),flush=True)


def run_live(out,backend,cfg):
    from script.p_repair_validation import run as validate
    policy=io.read(out/'experiment.json');lease=None;validated=False
    def release():
        nonlocal lease
        try:backend.close(graceful=True)
        finally:
            if lease:lease.__exit__(None,None,None);lease=None
            os.environ.pop('VIDEOAGENT_GPU_LEASE',None)
    try:
        for index,uid in enumerate(policy['case_ids'],1):
            if uid in set(policy['inherited_case_ids']) | set(policy.get('resumed_success_case_ids',[])):continue
            if (out/'runs'/uid).exists():raise ValueError('Case already attempted; no automatic retry')
            while not source.prepare_case(out,uid):
                release()
                source.assert_upstream_running(Path(policy['parent_N']))
                io.write(out/'progress.json',dict(stage='waiting_for_N_source',case=index,
                    question_id=uid,model_released=True,updated_at=io.now()))
                time.sleep(30)
            source.verify_case(out/'cases'/uid)
            if lease is None:lease=source.reload_after_source_wait(out,backend)
            if not validated:
                io.write(out/'progress.json',dict(stage='generic_validation',updated_at=io.now()))
                if policy.get('role_contract_validation'):
                    from script.p_role_contract_validation import run as validate_roles
                    validate_roles(out,backend,cfg)
                if policy.get('inherited_generic_validation'):
                    from script.resume_p_context import verify_inherited_gate
                    verify_inherited_gate(out)
                else:validate(out,backend,cfg)
                validated=True
            io.write(out/'progress.json',dict(stage='P',case=index,question_id=uid,updated_at=io.now()))
            case=out/'cases'/uid
            single.run_case(backend,cfg,io.read(case/'question.json'),io.read(case/'sentences.json'),
                io.read(case/'original_qa.json'),out/'runs'/uid,
                **({'selection_replay':policy['selection_replays'][uid]} if uid in policy.get('selection_replays',{}) else {}))
            report(out)
            print('P FULL CASE COMPLETE',index,uid,flush=True)
    finally:release()


def worker(out):
    from script_core.budget import ModelWorker
    from script.run_evidence_planner_experiment import verify_model
    single.verify(out);verify_model(out)
    if (out/'setup.json').exists():raise ValueError('Worker already attempted')
    io.write(out/'setup.json',dict(state='started',at=io.now()))
    cfg=io.read(out/'qa_config.json');backend=ModelWorker(cfg)
    try:run_live(out,backend,cfg)
    except Exception as exc:
        io.write(out/'failure.json',dict(type=type(exc).__name__,message=str(exc),at=io.now()));raise
    finally:backend.close(graceful=True);report(out)


def queue(out):
    single.verify(out)
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():raise ValueError('Already launched')
        def status(state,**kwargs):
            io.write(out/'queue_status.json',dict(state=state,controller_pid=os.getpid(),updated_at=io.now(),**kwargs))
        try:
            env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                     CUDA_DEVICE_ORDER='PCI_BUS_ID',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
            cmd=[sys.executable,'-u',str(out/'runtime/script/run_p_full_experiment.py'),'worker','--out',str(out)]
            with (out/'worker.log').open('a') as log:
                child=subprocess.Popen(cmd,cwd=out/'runtime',env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
            io.write(out/'launch.json',dict(worker_pid=child.pid,command=cmd,at=io.now()))
            while child.poll() is None:
                status('running',worker_pid=child.pid);time.sleep(5)
            if child.returncode or not report(out)['complete']:raise RuntimeError('P failed; retained without retry')
            status('complete',worker_pid=child.pid)
        except Exception as exc:status('failed',error=str(exc));raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--parent',type=Path)
    parser.add_argument('--diagnostic',type=Path);args=parser.parse_args();out=args.out.resolve()
    if args.mode=='prepare':prepare(out,args.parent.resolve(),args.diagnostic.resolve())
    else:globals()[args.mode](out)
