"""Replay exact durable model-call prefixes, then continue on GPUs 0/1 only."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script.scale_live_parallel_pool import read,write,sha,identity,now


class PrefixReplay:
    def __init__(self,original,calls,model_hash):
        self.original=original;self.calls=calls;self.position=0;self.model_hash=model_hash
        self.scope='explicit-gpu01-migration-call-prefix'

    def get(self,operation,inputs):
        from script_core.config import digest
        if self.position==len(self.calls):return self.original.get(operation,inputs)
        call=self.calls[self.position]
        if operation!=call['operation'] or digest(inputs)!=call['input_hash']:
            raise ValueError('Migration call prefix differs; refusing regeneration of prior successful work')
        if call.get('error') or call['model_hash']!=self.model_hash:
            raise ValueError('Migration call failure/model mismatch')
        raw=read(call['output_ref'])
        if raw.get('error') or raw.get('output') is None or digest(raw['inputs'])!=call['input_hash'] or digest(raw['output'])!=call['output_hash']:
            raise ValueError('Migration raw input/output integrity mismatch')
        self.position+=1
        return dict(key='gpu01-migration:'+call['call_id'],output=raw['output'],
            source_seconds=call['actual_cost'],origin=dict(call_id=call['call_id'],output_ref=call['output_ref'],
                model_hash=call['model_hash'],reason='Explicit user-requested GPU migration; exact successful call reuse'))

    def key(self,operation,inputs):return self.original.key(operation,inputs)
    def put(self,*args,**kwargs):return self.original.put(*args,**kwargs)


@contextmanager
def replay_meters(checkpoint,target,scheme,model_cfg):
    import script_core.budget as budget
    from script_core.config import digest
    original_init=budget.Meter.__init__;replayers=[];seen_regions=set();original=Path(checkpoint['original_attempt'])
    debt={}
    committed={c['call_id'] for rows in checkpoint['regions'].values() for c in rows}
    for pending in checkpoint['pending']:
        if pending['call_id'] in committed:continue
        region=str(Path(pending['path']).parent.parent.relative_to(original))
        debt[region]=debt.get(region,0.)+pending['reservation']
    def initialize(meter,backend,ledger,cfg,raw_directory):
        original_init(meter,backend,ledger,cfg,raw_directory)
        region=str(Path(raw_directory).parent.relative_to(target))
        seen_regions.add(region)
        cache=PrefixReplay(meter.text_cache,checkpoint['regions'].get(region,[]),digest(model_cfg['models']))
        meter.text_cache=cache;replayers.append(cache)
        ledger.dependency_cost+=debt.get(region,0.)
        old_summary=ledger.summary
        def summary():
            value=old_summary()
            replayed=[c for c in ledger.calls if c.get('tool_cache',{}).get('key','').startswith('gpu01-migration:')]
            seconds=sum(c['tool_cache']['budget_dependency_seconds'] for c in replayed)
            value.update(migration_successful_call_seconds=seconds,migration_interrupted_budget_seconds=debt.get(region,0.))
            if scheme=='O':
                # O passes extraction_cost to its next ledger using total. Keep
                # old successful stage work there exactly once, while retaining
                # interruptions as separate budget-only dependencies.
                value['total']+=seconds;value['online']+=seconds;value['shared_dependency_seconds']-=seconds
                for c in replayed:value['by_operation'][c['operation']]+=c['tool_cache']['budget_dependency_seconds']
            return value
        ledger.summary=summary
    budget.Meter.__init__=initialize
    try:
        yield replayers
        if any(cache.position!=len(cache.calls) for cache in replayers):
            raise ValueError('Not every prior successful migration call was consumed')
        if any(rows and region not in seen_regions for region,rows in checkpoint['regions'].items()):
            raise ValueError('A prior migration call region was skipped')
    finally:budget.Meter.__init__=original_init


def load_pool(folder,scheme):
    migration=read(folder/'config.json')['schemes'][scheme]
    out=Path(migration['out']);control=Path(migration['control'])
    # This entry imports orchestration helpers from the workspace. Model-facing
    # script imports must nevertheless resolve to the original frozen runtime.
    sys.modules['script'].__path__=[str(out/'runtime/script')]
    spec=importlib.util.spec_from_file_location('gpu01_frozen_pool',control/'runner.py')
    pool=importlib.util.module_from_spec(spec);spec.loader.exec_module(pool)
    pool.frozen_modules(out)
    return pool,out,control


def preflight(folder,scheme):
    pool,out,control=load_pool(folder,scheme)
    cfg=read(out/'qa_config.json');results=[]
    from script_core.config import digest
    class StopBackend:
        def __init__(self,pending):self.pending=pending;self.observed=False
        def call(self,operation,inputs,timeout):
            if not any(p['operation']==operation and p['input_hash']==digest(inputs) for p in self.pending):
                raise AssertionError('First uncompleted call changed after exact prefix replay')
            self.observed=True
            raise RuntimeError('Expected CPU-only migration preflight boundary; no model invoked')
    for path in (folder/'checkpoints').glob(scheme+'-*.json'):
        cp=read(path);uid=cp['question_id']
        if not uid:continue
        backend=StopBackend(cp['pending']);case=out/'cases'/uid
        if scheme=='P':
            from script.run_p_sentence_experiment import run_case
            target=folder/'cpu_replay_preflight'/scheme/uid/'run'
            try:
                with replay_meters(cp,target,scheme,cfg):run_case(backend,cfg,read(case/'question.json'),read(case/'sentences.json'),read(case/'original_qa.json'),target)
            except Exception:
                if not backend.observed:raise
        else:
            from script.o_batched_matrix import run
            view=folder/'cpu_replay_preflight'/scheme/uid/'view';view.parent.mkdir(parents=True,exist_ok=True);pool.prepare_view(out,view);target=view/'runs'/uid
            try:
                with replay_meters(cp,target,scheme,cfg):run(view,backend,cfg,case_ids=[uid])
            except Exception:
                if not backend.observed:raise
        assert backend.observed
        results.append(dict(question_id=uid,passed=True,successful_calls=sum(map(len,cp['regions'].values())),successful_seconds=cp['successful_seconds'],interrupted_budget_seconds=cp['interrupted_budget_seconds']))
    write(folder/f'{scheme}_cpu_replay_preflight.json',dict(passed=True,at=now(),results=results,no_model_calls=True))
    print('CPU REPLAY PREFLIGHT',scheme,results,flush=True)


def main(folder,scheme,wid):
    pool,out,control=load_pool(folder,scheme)
    original_read=pool.read;original_claim=pool.claim;original_run=pool.run_question
    checkpoint_paths={}
    for path in (folder/'checkpoints').glob(scheme+'-*.json'):
        cp=read(path)
        if cp['question_id'] is not None:checkpoint_paths[cp['question_id']]=path
    def controlled_read(path):
        value=original_read(path)
        if Path(path)==control/'config.json':
            value=deepcopy(value);value['resource_policy']['preference']=['0','1']
        return value
    pool.read=controlled_read
    def claim(root,c,worker_id,kind):
        with pool.locked(c):
            if (c/'failure.json').exists():return None
            state=read(c/'claims.json')
            paused=[(q,row) for q,row in state.items() if row['state']=='migration_paused' and q in checkpoint_paths]
            if paused:
                q,row=min(paused,key=lambda pair:pair[1]['index'])
                row.update(state='running',worker_id=worker_id,pid=__import__('os').getpid(),at=now(),migration_recovery=True)
                write(c/'claims.json',state);return row['index'],q
        return original_claim(root,c,worker_id,kind)
    pool.claim=claim
    def run_question(root,c,cfg,worker_id,uid,backend,model_cfg):
        if uid not in checkpoint_paths:
            return original_run(root,c,cfg,worker_id,uid,backend,model_cfg)
        checkpoint=read(checkpoint_paths[uid]);case=root/'cases'/uid
        from script.run_o_full_experiment import prepare_case,verify_case
        if not prepare_case(root,uid):raise ValueError('Migration source unavailable')
        verify_case(case)
        if scheme=='P':
            from script.run_p_sentence_experiment import run_case
            target=folder/'recovery'/scheme/uid/'run'
            with replay_meters(checkpoint,target,scheme,model_cfg):
                result=run_case(backend,model_cfg,read(case/'question.json'),read(case/'sentences.json'),read(case/'original_qa.json'),target)
        else:
            from script.o_batched_matrix import run
            view=folder/'views'/f'O-{worker_id}'
            if not view.exists():view.parent.mkdir(parents=True,exist_ok=True);pool.prepare_view(root,view)
            if read(view/'experiment.json')['conditions']!=['question_only']:
                raise ValueError('Migration supports the frozen question-only condition only')
            target=view/'runs'/uid
            with replay_meters(checkpoint,target,scheme,model_cfg):run(view,backend,model_cfg,case_ids=[uid])
            result=read(target/'result.json')
        result.update(gpu_migration_checkpoint=str(checkpoint_paths[uid]),
            migration_successful_call_dependency_seconds=checkpoint['successful_seconds'],
            migration_interrupted_budget_seconds=checkpoint['interrupted_budget_seconds'],
            migration_interrupt_scope='Full original reservation charged conservatively; observed wall elapsed saved separately, not an exact GPU measurement')
        write(target/'result.json',result)
        pool.publish(root,c,uid,target,scheme)
        write(folder/'recovery_results'/f'{scheme}-{uid}.json',dict(at=now(),result=str(root/'runs'/uid/'result.json'),
            replayed_success_seconds=checkpoint['successful_seconds'],interrupted_budget_seconds=checkpoint['interrupted_budget_seconds']))
    pool.run_question=run_question
    pool.worker(control,wid)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--folder',type=Path,required=True);parser.add_argument('--scheme',choices=['P','O'],required=True);parser.add_argument('--worker-id',type=int);parser.add_argument('--preflight',action='store_true')
    args=parser.parse_args()
    if args.preflight:preflight(args.folder.resolve(),args.scheme)
    else:main(args.folder.resolve(),args.scheme,args.worker_id)
