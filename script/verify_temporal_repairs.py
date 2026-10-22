"""GPU regressions on recorded failures; no answer labels enter the gate."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'script'))

from run_temporal_entity import read,write,sha,execute,checked_packet
from script_core.backend import preflight
from script_core.budget import BudgetLedger,Meter,ModelWorker
from script_core.compatibility import qa_inputs
from script_core.config import load_config,digest
from script_core.temporal_entity import observe_page,entity_context
from script_core.temporal_pipeline import compact_context,fit_qa_context


def check_snapshot(out):
    for name,expected in read(out/'source_hashes.json').items():
        if sha(ROOT/name)!=expected:raise ValueError('Frozen source changed: '+name)
    for name,expected in read(out/'control_hashes.json').items():
        if sha(out/name)!=expected:raise ValueError('Frozen control changed: '+name)


def valid_result(result):
    return (not result.get('error') and
        str((result.get('answer') or {}).get('selected_option')) in list('01234') and
        not result['cost_summary']['budget_violation'] and
        not result.get('directory_cost_summary',{}).get('budget_violation',False))


def timeout_replay(out,cfg,backend):
    cases=[]
    for name,run,arm,uid in [
        ('first_video_cross','script-temporal-entity-300-20261021-r5','temporal_cross','45fd8bca-c836-4601-b2c8-cc2a08cf8f86'),
        ('second_video_local','script-temporal-entity-300-20261021-r4','temporal_local','8345d177-0ed4-4965-8455-bd17d2b42399')]:
        old=out.parent/run/'smoke/arms'/arm/'runs'/uid
        call=next(c for c in map(json.loads,(old/'calls.jsonl').read_text().splitlines())
                  if c['operation']=='temporal_observe' and c.get('error') and 'Timeout' in c['error'])
        raw=read(Path(call['output_ref']));inp=raw['inputs'];net=read(old/'network.json')
        target=out/'verification/timeout_replays'/name
        ledger=BudgetLedger(1500,answer_reserve=45,calls_path=target/'calls.jsonl')
        meter=Meter(backend,ledger,cfg,target/'raw_calls')
        row=dict(name=name,source=str(old),source_input_hash=call['input_hash'])
        try:
            ids,report,accepted=observe_page(net,inp,meter,inp['entities'][0]['scene_ids'][-1],
                'original_timeout_regression',[e['id'] for e in inp['entities']])
            row.update(passed=True,ids=ids,accepted_call_id=accepted['call_id'],
                observations=sum(len(e['observations']) for e in report['entities']))
            write(target/'network.json',net)
        except Exception as exc:row.update(passed=False,error=f'{type(exc).__name__}: {exc}')
        finally:
            row['cost_summary']=ledger.summary();meter.close()
            row['passed']=row.get('passed',False) and not row['cost_summary']['budget_violation']
            write(target/'result.json',row)
        cases.append(row);print('TIMEOUT_REPLAY',json.dumps(row),flush=True)
    return cases


def qa_replay(out,previous,cfg,backend):
    uid='509e6545-5fff-4f73-ae0f-524dfa8b3c2c'
    old=previous/'arms/temporal_cross/runs'/uid;result=read(old/'result.json')
    packet=checked_packet(out,uid);net=read(old/'network.json');d=read(old/'directory.json')
    query=dict(packet['question'],id=uid)
    selected=sorted(i for sid in result['opened_scenes']
        for i in next(s for s in d['scenes'] if s['id']==sid)['chronological_order'])
    target=out/'verification/qa_replay'
    ledger=BudgetLedger(1500,initialization_cost=result['cost_summary']['total'],
                        answer_reserve=45,calls_path=target/'calls.jsonl')
    meter=Meter(backend,ledger,cfg,target/'raw_calls')
    value=dict(question_id=uid,source=str(old),network_hash=digest(net))
    try:
        inp,context,tokens=fit_qa_context(query,d['captions'],selected,
            compact_context(entity_context(net,query)),meter,final=True)
        base=qa_inputs(query,d['captions'],selected)
        unchanged=(inp['head']==base['head'] and
            inp['prompts'][0].split('\n\nTemporal entity memory')[0]==base['prompts'][0])
        if not unchanged:raise ValueError('Original captions or QA template changed')
        raw,_=meter.call('videotree_qa',inp,final=True)
        value.update(answer=raw['data'],input_tokens=tokens,context_selection=context['context_selection'],
            original_captions_and_template_unchanged=unchanged,
            passed=tokens<=cfg['models']['max_input_tokens'] and str(raw['data'].get('selected_option')) in list('01234'))
        write(target/'qa_context.json',dict(context=context,input_tokens=tokens,inputs=inp))
    except Exception as exc:value.update(passed=False,error=f'{type(exc).__name__}: {exc}')
    finally:
        value['cost_summary']=ledger.summary();meter.close()
        value['passed']=value.get('passed',False) and not value['cost_summary']['budget_violation']
        write(target/'result.json',value)
    print('QA_REPLAY',json.dumps(value),flush=True)
    return value


def temporal(out,previous):
    check_snapshot(out)
    cfg,artifacts=preflight(load_config(out/'config.json'))
    write(out/'verification/artifacts.json',artifacts)
    backend=ModelWorker(cfg);rows=[]
    try:
        replays=timeout_replay(out,cfg,backend)
        qa=qa_replay(out,previous,cfg,backend)
        # Exercise every arm plus the r6 total-budget failure end to end. Results
        # stay in smoke/, separate from the 300-question formal measurements.
        for arm,uid in [('cluster_qa','45fd8bca-c836-4601-b2c8-cc2a08cf8f86'),
                        ('temporal_local','8345d177-0ed4-4965-8455-bd17d2b42399'),
                        ('temporal_cross','edb5c7f4-636f-47e6-97f8-b27795aaeec5')]:
            result=execute(out,arm,uid,backend,cfg,smoke=True)
            passed=valid_result(result)
            if arm!='cluster_qa':passed=passed and result.get('entity_count',0)>0 and result.get('state_count',0)>0
            if arm=='temporal_cross':passed=passed and bool(result.get('reviews')) and result.get('identity_comparisons',0)>0
            row=dict(arm=arm,question_id=uid,passed=passed,error=result.get('error'),
                incomplete=result.get('incomplete'),input_tokens=result.get('qa_input_tokens'),
                total_measured_seconds=result.get('total_measured_seconds'))
            rows.append(row);write(out/'verification/progress.json',dict(cases=rows))
            print('END_TO_END',json.dumps(row),flush=True)
        value=dict(passed=all(r['passed'] for r in rows+replays+[qa]),
            end_to_end=rows,timeout_replays=replays,qa_replay=qa,accuracy_used_for_gate=False,
            scope='Recorded failures and all three execution paths; not a population accuracy estimate.')
    finally:backend.close()
    write(out/'verification/status.json',value)
    print(json.dumps(value),flush=True)
    return value['passed']


def aligned(out):
    from run_videotree_aligned import work
    # This gate checks original baseline prompts/selection and the shared flat /
    # structured evidence, including explicit partial graphs at the 600s limit.
    work(out,'paired',0,True)
    value=read(out/'smoke/status.json')
    write(out/'verification/status.json',value)
    return value['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True);parser.add_argument('--previous')
    parser.add_argument('--suite',choices=['temporal','aligned'],required=True)
    args=parser.parse_args();out=Path(args.out).resolve()
    try:
        passed=temporal(out,Path(args.previous).resolve()) if args.suite=='temporal' else aligned(out)
    except Exception as exc:
        write(out/'verification/status.json',dict(passed=False,error=f'{type(exc).__name__}: {exc}'))
        raise
    raise SystemExit(0 if passed else 1)
