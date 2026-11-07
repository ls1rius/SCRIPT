"""Explicit continuation of stopped P reviews, preserving all earlier work/costs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def validate_reader(folder,backend,cfg):
    from script import controlled_memory_io as io
    from script.run_p_sentence_experiment import batch
    from script_core import p_sentence_filter as p
    from script_core.budget import BudgetLedger,Meter
    target=folder/'reader_repair_validation'
    if target.exists():
        raise ValueError('Reader gate already attempted; no retry')
    ledger=BudgetLedger(600,answer_reserve=0,calls_path=target/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,target/'raw_calls')
    request=p.review_input([dict(source_id=s,intervals=[[i,i+1]],information=[dict(kind='action',actor='Observer Z9',verb=v)])
        for i,(s,v) in enumerate([('S0','waits'),('S1','walks'),('S10','rests')])],
        dict(question='Which sequence is recorded?',options=['Waits, walks, then rests.','Paints a door.','Cuts a board.','Washes a dish.','Moves a parcel.']))
    result=dict(passed=False,scope='Fresh real format/local-reference uniqueness gate; not semantic QA validation')
    try:
        row=batch(meter,'P_reader_unique_reference_probe',p.REVIEW,[('reader',request)])[0]
        p.validate_review(request,row['data'])
        result.update(passed=True,request=request,output=row)
    except Exception as exc:
        result['error']=dict(type=type(exc).__name__,message=str(exc));raise
    finally:
        result['cost_summary']=ledger.summary();io.write(folder/'reader_repair_functional.json',result);meter.close()


def prepare(parent,out):
    from script import controlled_memory_io as io
    from script.parallel_artifacts import tree_hashes,copy_tree_verified
    from script.resume_p_context import validated_selection
    from script.parallel_scheme_pool import identity
    from script.scale_live_parallel_pool import verify_manifest
    from script_core import p_sentence_filter as p
    from script_core.config import digest
    if out.exists():raise ValueError('Fresh explicit resume directory required')
    status=io.read(parent/'queue_status.json')
    if status['state']!='failed':raise ValueError('Require stopped failed P pool')
    if any(identity(pid) for pid in [status['controller_pid'],*status['worker_pids']]):
        raise ValueError('Old P process is still alive')
    verify_manifest(parent)
    previous=Path(status['scheduler']);claims=io.read(previous/'claims.json')
    failed=[q for q,row in claims.items() if row['state']=='running']
    if len(failed)!=2:raise ValueError('Expected two retained failed reviews')
    replays={};failures=[];selection_hashes={}
    for uid in failed:
        attempt=previous/'attempts'/uid/'run';case=parent/'cases'/uid
        result=io.read(attempt/'result.json')
        if not result.get('error'):raise ValueError('No explicit failed result')
        validated_selection(attempt,io.read(case/'question.json'),io.read(case/'sentences.json'))
        calls=[__import__('json').loads(l) for l in (attempt/'calls.jsonl').read_text().splitlines() if l.strip()]
        if calls[-1]['parameters']['operation']!=p.REVIEW:
            raise ValueError('Only final review failures may reuse complete selection')
        for call in calls[:-1]:
            raw=io.read(call['output_ref'])
            if call.get('error') or raw.get('error') or raw.get('output') is None or digest(raw['inputs'])!=call['input_hash']:
                raise ValueError('Successful preprocessing call is not replayable')
        successful=sum(c['actual_cost'] for c in calls[:-1]);failed_cost=calls[-1]['actual_cost']
        if abs(successful+failed_cost-result['cost_summary']['total'])>1e-6:
            raise ValueError('Original attempt accounting does not reconcile')
        replays[uid]=dict(folder=str(out/'inherited_preprocessing'/uid),parent=str(attempt),
            successful_seconds=successful,successful_calls=len(calls)-1,prior_failed_seconds=failed_cost,
            previous_attempt_seconds=successful+failed_cost)
        failures.append(dict(question_id=uid,parent=str(attempt),processing_seconds=failed_cost,
            full_attempt_processing_seconds=successful+failed_cost,shared_success_dependency_seconds=successful,
            reason=result['error'],scope='Only failed review cost; successful earlier calls are reused dependencies, counted once'))
        selection_hashes[uid]=tree_hashes(attempt)
    successful_ids=[]
    for f in (parent/'runs').glob('*/result.json'):
        row=io.read(f)
        if not row.get('complete') or row.get('error'):raise ValueError('Published P output is not successful')
        successful_ids.append(f.parent.name)
    inherited_hashes=tree_hashes(parent/'runs')
    out.mkdir()
    names=['runtime','questions','cases','runs','validation']
    def copy(name):return copy_tree_verified(parent/name,out/name,workers=2)
    with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(copy,names))
    for name in ['cohort.json','evaluation_only.json','qa_config.json','qa_expected_artifacts.json']:
        shutil.copy2(parent/name,out/name)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda q:copy_tree_verified(previous/'attempts'/q/'run',out/'inherited_preprocessing'/q,
            snapshot=selection_hashes[q],workers=2),failed))
    patches=['script_core/p_sentence_filter.py','script_core/o_generation.py','script_core/o_batch_generation.py',
        'script_core/json_unique_array_guard.py','script/run_p_sentence_experiment.py','script/run_p_reader_repair.py']
    for name in patches:shutil.copy2(ROOT/name,out/'runtime'/name)
    cfg=io.read(out/'qa_config.json');cfg['models']['cuda_allocator_limit_mib']=30720
    io.write(out/'qa_config.json',cfg)
    policy=io.read(parent/'experiment.json')
    policy.update(version=p.VERSION,resume_parent=str(parent),resumed_success_case_ids=successful_ids,
        selection_replays=replays,historical_failed_attempts=policy.get('historical_failed_attempts',[])+failures,
        created_at=io.now(),scope=policy['scope']+' v17 constrains unique review references; the two stopped selections are reused exactly, with failed review costs retained in each original budget.',
        repair_scope='Review prompt/schema uniqueness and decoder guard; allocator21->30GiB, admission23->32GiB; extraction/filter/reader facts unchanged')
    policy['resource_policy'].update(minimum_free_mib=32768,allocator_limit_mib=30720)
    io.write(out/'experiment.json',policy)
    io.write(out/'validation/reader_repair_preflight.json',dict(passed=True,successful_cases=len(successful_ids),
        selection_replays=replays,historical_review_failures=failures,method_patches=patches,
        inherited_results_hashes=inherited_hashes,at=io.now()))
    if tree_hashes(parent/'runs')!=inherited_hashes or tree_hashes(out/'runs')!=inherited_hashes:
        raise ValueError('Inherited successful runs changed')
    for uid in failed:
        if tree_hashes(previous/'attempts'/uid/'run')!=selection_hashes[uid]:raise ValueError('Old failed attempt changed')
    io.write(out/'frozen_hashes.json',tree_hashes(out))
    verify_manifest(out)
    control=out/'parallel-20261104-r1';control.mkdir()
    shutil.copy2(ROOT/'script/parallel_scheme_pool.py',control/'runner.py')
    shutil.copy2(ROOT/'script/scale_live_parallel_pool.py',control/'scale_live_parallel_pool.py')
    io.write(control/'config.json',dict(out=str(out),scheme='P',workers=2,resume_failed_run=True,
        reader_repair_validation=True,resource_policy=policy['resource_policy'],at=io.now(),
        original_controller=dict(pid=status['controller_pid'],identity=None),
        original_worker=dict(pid=status['worker_pids'][0],identity=None),
        gates=dict(inherited=True,files=['repair_functional.json','role_contract_functional.json'],
            scope='Extraction/fidelity/relevance gates inherited; fresh reader uniqueness gates on both new workers')))
    io.write(control/'boundary.json',dict(passed=True,at=io.now(),pending_model_calls=0,
        scope='Old pool already failed and stopped; both original attempts retained'))
    io.write(control/'frozen_hashes.json',tree_hashes(control))
    from script.run_p_full_experiment import report
    report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    print('P REVIEW REPAIR PREPARED',out,'successes',len(successful_ids),'replays',len(replays),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--parent',type=Path,required=True);ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args();prepare(args.parent.resolve(),args.out.resolve())
