"""Read-only queued finalizer: paired Q4-trained vs Q4, complete v3 and L.

This process never imports the training backend, touches a GPU, or writes frozen
runtime files. Its baseline answers are isolated from teacher/training inputs.
"""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from script.extend_q4_500 import read,write,sha,now,pid_identity


def paired(predictions,baseline,gold):
    if set(predictions)!=set(baseline) or set(predictions)!=set(gold):
        raise ValueError('Paired comparison requires exactly identical question IDs')
    correct=lambda p,u:str(p[u])==str(gold[u])
    fixed=[u for u in gold if correct(predictions,u) and not correct(baseline,u)]
    regressed=[u for u in gold if correct(baseline,u) and not correct(predictions,u)]
    return dict(correct=sum(correct(predictions,u) for u in gold),baseline_correct=sum(correct(baseline,u) for u in gold),
        fixed=fixed,regressed=regressed,net=len(fixed)-len(regressed),changed=[u for u in gold if str(predictions[u])!=str(baseline[u])])


def prepare(out,repo):
    folder=out/'analysis';folder.mkdir(exist_ok=False)
    ids=read(out/'test_ids.json');gold=read(Path(read(out/'experiment.json')['parent'])/'evaluation_only.json')
    q4=Path(read(out/'experiment.json')['parent']);v3=repo/'workdirs/Q3-coverage-repair-300-20261105-r2';ll=repo/'workdirs/evidence-planner-L-I-300-20261102-r1'
    expected=read(repo/'docs/history/Q_V3_V4_Completed_Performance_2026-11-05.json')
    if set(ids)!=set(read(v3/'cohort.json')) or set(ids)!=set(read(ll/'cohort.json')):
        raise ValueError('Baseline question IDs differ')
    for other in [v3,ll]:
        if {u:str(x) for u,x in read(other/'evaluation_only.json').items()}!={u:str(x) for u,x in gold.items()}:
            raise ValueError('Baseline gold differs')
    arms={n:{} for n in ['Q4','Q4plus','v3','v3plus','L']};hashes={}
    for uid in ids:
        question=read(q4/'cases'/uid/'question.json')
        if question!=read(v3/'cases'/uid/'question.json') or question!=read(ll/'question'/f'{uid}.json'):
            raise ValueError('Baseline original question/options differ')
        for root,names in [(q4,['Q4','Q4plus']),(v3,['v3','v3plus'])]:
            p=root/'runs'/uid/'result.json';r=read(p);hashes[str(p)]=sha(p)
            for n,arm in zip(names,['Q','Qplus']):arms[n][uid]=r['arms'][arm]['answer']['selected_option']
        p=ll/'runs'/uid/'L_evidence_planner/result.json';r=read(p);hashes[str(p)]=sha(p)
        if r['error']:raise ValueError('Baseline L reader failure')
        arms['L'][uid]=r['selected_option']
    checks={n:sum(str(pred[u])==str(gold[u]) for u in ids) for n,pred in arms.items()}
    if checks!=dict(Q4=193,Q4plus=193,v3=188,v3plus=192,L=189):
        raise ValueError('Baseline scores differ from preserved audit')
    write(folder/'baselines.json',dict(ids=ids,gold=gold,arms=arms,correct=checks,source_hashes=hashes,
        costs=dict(Q4=expected['summary']['v4'],v3=expected['summary']['v3'],L=read(ll/'report.json')),
        provenance='Question/options, gold and exact complete300 answer hashes verified. This file is for post-training evaluation only; training code never reads analysis/.',at=now()))
    shutil.copy2(repo/'script/analyze_q4_training.py',folder/'analyze_q4_training.py')
    write(folder/'frozen_hashes.json',{p.name:sha(p) for p in folder.iterdir() if p.is_file()})


def finish(out):
    base=read(out/'analysis/baselines.json');root=out/'evaluation';report=read(root/'report.json')
    if not report['complete'] or report['errors'] or report['scheduler_failure']:
        raise ValueError('Cannot finalize incomplete trained evaluation')
    ids=base['ids'];rows={u:read(root/'runs'/u/'result.json') for u in ids};comparisons={}
    for arm,old in [('Q',['Q4','v3','L']),('Qplus',['Q4plus','v3plus','L'])]:
        pred={u:r['arms'][arm]['answer']['selected_option'] for u,r in rows.items()}
        comparisons[arm]={name:paired(pred,base['arms'][name],base['gold']) for name in old}
    operations=defaultdict(lambda:dict(calls=0,input_tokens=0,output_tokens=0,seconds=0.))
    for uid in ids:
        for line in (root/'runs'/uid/'calls.jsonl').read_text().splitlines():
            c=json.loads(line)
            operation=c['parameters'].get('operation',c['operation'])
            row=operations[operation];row['calls']+=1;row['input_tokens']+=c['input_tokens'];row['output_tokens']+=c['output_tokens'];row['seconds']+=c['actual_cost']
    total={k:sum(v[k] for v in operations.values()) for k in ['calls','input_tokens','output_tokens','seconds']}
    training=read(out/'report.json')
    write(out/'comparison_extended.json',dict(complete=True,questions=300,paired=comparisons,
        trained_evaluation_calls=total,trained_evaluation_per_question={k:v/300 for k,v in total.items()},operations=dict(operations),
        inherited_N_budget_dependency_seconds=report['inherited_source_budgeted_seconds'],evaluation_model_setup_seconds=report['setup_seconds'],
        evaluation_validation_seconds=report['validation_processing_seconds'],
        teacher=dict(processing_seconds=training['teacher_processing_seconds'],input_tokens=training['teacher_input_tokens'],
            output_tokens=training['teacher_output_tokens'],setup_seconds=training['teacher_setup_seconds']),
        training=training['training'],gradient_gate=training['real_gradient_gate'],baseline_costs=base['costs'],
        videotree=read(Path(read(out/'experiment.json')['untrained500'])/'historical_videotree.json')['reports']['300'],
        limits=['Teacher/training one-time work and evaluation per-query processing are separate; no double counting N dependencies.',
            'Forward/backward training token passes are not generated output tokens.',
            'Baseline VideoTree tokens and per-ID answers are unavailable; no fabricated paired significance or token totals.',
            'Shared GPU timing and different retrieval pipelines are not a controlled speed benchmark.',
            read(out/'experiment.json')['evaluation_scope']],at=now()))


def wait(out):
    folder=out/'analysis'
    for n,h in read(folder/'frozen_hashes.json').items():
        if sha(folder/n)!=h:raise ValueError('Frozen finalizer artifact changed')
    while True:
        q=read(out/'queue_status.json')
        if q['state']=='complete':
            finish(out);write(folder/'status.json',dict(stage='complete',pid=os.getpid(),at=now()));return
        if q['state']=='failed':
            write(folder/'status.json',dict(stage='stopped_after_experiment_failure',pid=os.getpid(),at=now(),error=q.get('error')));return
        if q.get('controller_pid') and pid_identity(q['controller_pid']) is None:
            write(folder/'status.json',dict(stage='controller_disappeared',pid=os.getpid(),at=now()));return
        write(folder/'status.json',dict(stage='waiting_for_training_and_evaluation',pid=os.getpid(),training_stage=q.get('stage'),at=now()))
        time.sleep(30)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','wait','finish']);p.add_argument('--out',type=Path,required=True);p.add_argument('--repo',type=Path);a=p.parse_args()
    if a.action=='prepare':prepare(a.out.resolve(),a.repo.resolve())
    else:globals()[a.action](a.out.resolve())
