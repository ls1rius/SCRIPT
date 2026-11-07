"""Frozen Q/Q+ experiment: same O sources, independent bounded question workers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.scale_live_parallel_pool import read, write, sha, identity, now, verify_manifest
from script_core import q_evidence as q
from script_core.config import digest


@contextmanager
def locked(out):
    with (out/'claims.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def fail(out, worker_id, exc):
    with locked(out):
        if not (out/'failure.json').exists():
            write(out/'failure.json', dict(worker_id=worker_id, type=type(exc).__name__,
                                          message=str(exc), at=now()))


def claim(out, worker_id):
    with locked(out):
        if (out/'failure.json').exists():
            return None
        claims = read(out/'claims.json')
        for index, uid in enumerate(read(out/'cohort.json'), 1):
            if uid in claims:
                continue
            if (out/'runs'/uid).exists() or (out/'attempts'/uid).exists():
                raise ValueError('Unclaimed preexisting Q attempt')
            claims[uid] = dict(state='running', worker_id=worker_id, pid=os.getpid(), index=index, at=now())
            write(out/'claims.json', claims)
            return index, uid
    return None


def publish(out, uid, target):
    result = read(target/'result.json')
    if not result['complete'] or result.get('error') or set(result['arms']) != {'Q', 'Qplus'}:
        raise ValueError('Incomplete Q publication')
    if list(target.rglob('*.pending.json')):
        raise ValueError('Q publication has pending calls')
    with locked(out):
        dest = out/'runs'/uid
        if dest.exists():
            raise ValueError('Duplicate Q publication')
        dest.parent.mkdir(parents=True, exist_ok=True)
        target.rename(dest)
        target.symlink_to(dest, target_is_directory=True)
        claims = read(out/'claims.json')
        claims[uid].update(state='complete', completed_at=now())
        write(out/'claims.json', claims)


def packet(uid, requests):
    scope = dict(question_id=uid, condition='original_question_only')
    return dict(scope=scope, operation=q.FILTER, items=[
        dict(source_id=r['current']['source_id'], scope=dict(scope), request=r) for r in requests])


def filter_batch(meter, uid, requests):
    payload = packet(uid, requests)
    from script_core.o_batch_generation import validate_packet
    validate_packet(payload)
    result, _ = meter.call(q.BATCH, payload)
    if len(result.get('items', [])) != len(requests):
        raise ValueError('Q filter row count mismatch')
    verdicts = []
    for row, req in zip(result['items'], requests):
        if (row['source_id'] != req['current']['source_id'] or row['scope'] != payload['scope']
                or row['request_hash'] != digest(req) or row.get('parse_error') or not row['ended_normally']):
            raise ValueError('Q filter identity/schema/EOS mismatch')
        q.validate_output(row['data'], q.schema())
        verdicts.append(row['data'])
    return verdicts


def run_case(backend, cfg, question, sources, original, folder, progress=None):
    from script_core.budget import BudgetLedger, Meter
    if folder.exists():
        raise ValueError('Q never silently resumes/retries an existing attempt')
    folder.mkdir(parents=True)
    uid = question['question_id']
    ledger = BudgetLedger(3600, answer_reserve=2*cfg['budget']['final_answer_reserve'],
                          calls_path=folder/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, folder/'raw_calls')
    result = dict(question_id=uid, version=q.VERSION, plus_version=q.PLUS_VERSION,
                  complete=False, error=None, arms={}, new_navigation=False,
                  extraction_called=False, task_plan_called=False, composition_called=False,
                  selection_policy='keep_only; uncertain archived, excluded from both readers')
    try:
        decisions = {}
        for start in range(0, len(sources), 4):
            requests = [q.request_for(question, sources, i) for i in range(start, min(start+4, len(sources)))]
            if progress:
                progress('filter', observation=start+1, total_observations=len(sources))
            verdicts = filter_batch(meter, uid, requests)
            for request, verdict in zip(requests, verdicts):
                sid = request['current']['source_id']
                decisions[sid] = verdict
                write(folder/'decisions'/f'{sid}.json', dict(request=request, verdict=verdict))
        chosen = q.selected_observations(sources, decisions)
        write(folder/'selection.json', decisions)
        write(folder/'retained.json', chosen)
        # Exact same retained observation bytes and intervals for both arms.
        result.update(source_observations=len(sources), retained_observations=len(chosen),
                      decisions={v:sum(d['decision']==v for d in decisions.values())
                                 for v in ('keep', 'uncertain', 'drop')},
                      retained_evidence_hash=digest(chosen))
        for arm, operation, compact in [('Q', 'q_answer', False), ('Qplus', 'qplus_answer', True)]:
            final = q.final_input(original, chosen, compact)
            write(folder/arm/'final_input.json', final)
            if progress:
                progress('answer', arm=arm)
            answer, call = meter.call(operation, dict(inputs=final, options=question['options']), final=True)
            # The shared strict reader raises on missing EOS or invalid letters.
            if answer.get('parse_error') or 'selected_option' not in answer.get('data', {}):
                raise ValueError('Q strict answer contract failed')
            selected = str(answer['data']['selected_option'])
            if selected not in {str(i) for i in range(len(question['options']))}:
                raise ValueError('Q answer outside option range')
            write(folder/arm/'answer.json', answer)
            result['arms'][arm] = dict(answer=answer['data'], input_tokens=call['input_tokens'],
                                      answer_seconds=call['actual_cost'], retained_evidence_hash=digest(chosen))
            write(folder/'result.json', result)
        result['complete'] = True
    except BaseException as exc:
        result['error'] = str(exc)
        raise
    finally:
        result.update(cost_summary=ledger.summary(), updated_at=now())
        write(folder/'result.json', result)
        meter.close()
    return result


def probes():
    # Synthetic examples only; no benchmark source, question, options or answer.
    return [
        ('recurring', 'Which actions recur during cleaning?', 'J wipes a shelf.', [], True),
        ('phases', 'Describe the stages of the work.', 'J opens a crate.', ['J removes a tool.'], True),
        ('adjustment', 'What adjustments are made to the device?', 'R tightens a valve.', [], True),
        ('purpose', 'What is the overall goal of these actions?', 'M sets a bowl on a desk.', ['M pours seeds into the bowl.'], True),
        ('counterevidence', 'Does J open the container?', 'J does not open the container.', [], True),
        ('modality', 'What happens to the lever?', 'J may rotate the lever.', [], True),
        ('participants', 'How do the people cooperate?', 'D hands a cup to E.', [], True),
        ('multiple', 'Describe the work process.', 'T opens a crate and removes a wrench.', [], True),
        ('unicode', 'What is being moved?', '繫 moves the "blue" box.', [], True),
        ('color', 'How is the room cleaned?', 'An unused clock is purple.', ['J wipes the table.'], False),
        ('unrelated', 'How is the table cleaned?', 'A distant bird flies over the roof.', ['J wipes the table.'], False),
        ('local', 'Compare preparation with later work.', 'V places tools on a bench.', ['V repairs a hinge.'], True),
    ]


def validate_live(backend, cfg, folder):
    from script_core.budget import BudgetLedger, Meter
    folder.mkdir(parents=True, exist_ok=True)
    ledger = BudgetLedger(600, answer_reserve=90, calls_path=folder/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, folder/'raw_calls')
    checks = []
    try:
        # Batch only rows with the same question, as in production. Different
        # synthetic probes never share a batch or context/cache scope.
        for name, question, text, neighbors, should_keep in probes():
            source = dict(id='S0', text=text, source_intervals=[[0, 1]])
            request = dict(question=question,
                           current=dict(source_id='S0', text=text, intervals=[[0, 1]]),
                           context=[dict(source_id=f'S{i+1}', text=t, intervals=[[i+1, i+2]]) for i,t in enumerate(neighbors)])
            verdict = filter_batch(meter, 'q-gate-'+name, [request])[0]
            kept = bool(q.selected_observations([source], {'S0':verdict}))
            allowed = ({'keep','uncertain'} if name in ('phases','purpose','local')
                       else {'keep'} if should_keep else {'drop'})
            checks.append(dict(name=name, request=request, verdict=verdict,
                               passed=verdict['decision'] in allowed, retained_for_reader=kept,
                               allowed_decisions=sorted(allowed)))
        # Four identical-question independent rows validate the actual batch path.
        sources = [dict(id=f'S{i}', text=f'J moves the box {i}.', source_intervals=[[i, i+1]]) for i in range(4)]
        requests = [q.request_for(dict(question='What is being moved?'), sources, i) for i in range(4)]
        verdicts = filter_batch(meter, 'q-gate-batch4', requests)
        checks.append(dict(name='batch4_isolated_rows', passed=all(v['decision']=='keep' for v in verdicts), verdicts=verdicts))
        if not all(c['passed'] for c in checks):
            raise ValueError('Q live relevance probe failed; no formal questions started')
    finally:
        write(folder/'functional.json', dict(passed=bool(checks) and len(checks)==13 and all(c['passed'] for c in checks),
            checks=checks, cost_summary=ledger.summary(), at=now(),
            scope='New synthetic real-model relevance and independent batch gates; not benchmark accuracy evidence'))
        meter.close()
    # A fresh full pair also checks final reader operations and lossless arms.
    source = [dict(id='S0', text='J places a cup on the desk.', source_intervals=[[0, 1]])]
    # Same original reader input convention as the benchmark, without dataset facts.
    original = dict(query_id='q-gate-reader', head='Choose the option supported by the observations.',
        prompts=['  Description: old evidence\n\n###\n\n Questions: Where is the cup? \n Options: \n A: On the desk. \n B: On the shelf. \n\n###\n\n The prediction is:'])
    question = dict(question_id='q-gate-reader', question='Where is the cup?', options=['On the desk.', 'On the shelf.'])
    result = run_case(backend, cfg, question, source, original, folder/'reader_pair')
    passed = result['complete'] and all(str(v['answer']['selected_option'])=='0' for v in result['arms'].values())
    write(folder/'reader_gate.json', dict(passed=passed, result=result, at=now()))
    if not passed:
        raise ValueError('Q/Q+ synthetic reader gate failed')


def prepare(out, source, workers):
    if out.exists() or not 1 <= workers <= 4:
        raise ValueError('Require fresh Q directory and 1..4 bounded workers')
    prior = read(source/'report.json')
    if not prior['complete'] or prior['errors']:
        raise ValueError('Require complete, successful O source archive')
    out.mkdir(parents=True)
    for name in ['cohort.json', 'evaluation_only.json', 'qa_expected_artifacts.json']:
        shutil.copy2(source/name, out/name)
    cfg = read(source/'qa_config.json')
    cfg['models']['cuda_allocator_limit_mib'] = 22528
    if cfg['query']['tool_transient_retries'] or cfg['query']['json_repair_retries']:
        raise ValueError('Q forbids automatic retries')
    write(out/'qa_config.json', cfg)
    ids = read(out/'cohort.json')
    def copy_case(uid):
        origin = source/'cases'/uid
        expected = read(origin/'source_hashes.json')
        destination = out/'cases'/uid
        destination.mkdir(parents=True)
        hashes = {}
        for name in ['question.json', 'sentences.json', 'original_qa.json']:
            if sha(origin/name) != expected[name]:
                raise ValueError('Original frozen O source changed')
            shutil.copy2(origin/name, destination/name)
            hashes[name] = sha(destination/name)
            if hashes[name] != expected[name]:
                raise ValueError('Q source copy mismatch')
        question = read(destination/'question.json')
        if question['question_id'] != uid:
            raise ValueError('Q question identity mismatch')
        sources = read(destination/'sentences.json')
        for i in range(len(sources)):
            q.request_for(question, sources, i)
        write(destination/'source_binding.json', dict(source=str(origin), files=hashes,
            original_source_manifest_sha256=sha(origin/'source_hashes.json'),
            original_source_audit=read(origin/'source_audit.json'), new_navigation=False))
        return uid, len(sources)
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = dict(pool.map(copy_case, ids))
    write(out/'experiment.json', dict(version=q.VERSION, plus_version=q.PLUS_VERSION,
        source_O=str(source), case_ids=ids, workers=workers, planned=len(ids),
        case_budget_seconds=3600, batch_size=4, original_question_only=True,
        options_in_filter=False, task_plan=False, extraction=False, model_composition=False,
        atomic_unit='literal whole-source observation; no generated roles',
        context='immediate previous/next source observations of this same question, at most two',
        uncertain_policy='archive_only; exclude from Q and Qplus final inputs',
        Qplus='fixed lossless compact rendering of identical keep-only Q evidence',
        known_analysis_case_ids=['97dc6bb7-7eed-45f7-bdb0-269ab8c2f639','16eb6914-00e7-4692-a7d7-d6ba36f61a5d',
            'b5da8ac2-01ae-4390-8e0b-ce777bb0c86b','00b9a0de-c59e-49cb-a127-6081e2fb8c8e',
            '97f80e1c-164d-4072-8ee9-980e366eec6c','a7b381f3-7f73-46fa-a99c-80a717a34556',
            'a4663dd4-73e2-4e2a-b66b-731527a10c2c','0f9d1d44-8a15-4135-a77e-d64064afc1e4'],
        evaluation_scope='Same 300 previously examined questions; post-analysis architecture evaluation, not an untouched holdout. No benchmark facts/examples or case-specific logic in model prompt.',
        source_limitation='Original N-selected O sources; upstream missing evidence not repaired in this fixed-source comparison.',
        resource_policy=dict(preference=['0','1'], minimum_free_mib=24576, maximum_utilization=100,
            stable_observations=3, admission_mode='memory_shared', allocator_limit_mib=22528),
        at=now(), training=False, adapter=None, no_automatic_retries=True))
    for name in ['script', 'script_core']:
        shutil.copytree(ROOT/name, out/'runtime'/name, ignore=shutil.ignore_patterns('__pycache__'))
    for path in ROOT.glob('*.py'):
        shutil.copy2(path, out/'runtime'/path.name)
    (out/'runs').mkdir()
    write(out/'claims.json', {})
    write(out/'validation/source_preflight.json', dict(passed=True, cases=len(counts),
        observations=sum(counts.values()), empty_source_cases=[uid for uid,n in counts.items() if not n],
        options_used_for_source_selection=False, at=now()))
    write(out/'frozen_hashes.json', {str(p.relative_to(out)):sha(p) for p in out.rglob('*') if p.is_file()
          and p.name!='claims.json'})
    verify_manifest(out)
    write(out/'queue_status.json', dict(state='prepared', updated_at=now()))
    build_report(out)


def build_report(out):
    ids = read(out/'cohort.json');gold = read(out/'evaluation_only.json');policy = read(out/'experiment.json')
    rows = [read(out/'runs'/uid/'result.json') for uid in ids if (out/'runs'/uid/'result.json').exists()]
    success = [r for r in rows if r['complete'] and not r.get('error')]
    groups = {'all':set(ids), 'analysis_known':set(policy['known_analysis_case_ids']),
              'other':set(ids)-set(policy['known_analysis_case_ids'])}
    def arm_stats(arm, allowed):
        selected = [r for r in success if r['question_id'] in allowed]
        n=len(selected)
        return dict(completed=n, correct=sum(str(r['arms'][arm]['answer']['selected_option'])==str(gold[r['question_id']]) for r in selected),
            mean_input_tokens=sum(r['arms'][arm]['input_tokens'] for r in selected)/n if n else None,
            mean_qa_seconds=sum(r['arms'][arm]['answer_seconds'] for r in selected)/n if n else None)
    result = dict(version=q.VERSION, plus_version=q.PLUS_VERSION, completed=len(success), planned=len(ids),
        complete=len(success)==len(ids), errors=sum(bool(r.get('error')) for r in rows),
        arms={a:arm_stats(a,set(ids)) for a in ['Q','Qplus']},
        subgroups={g:{a:arm_stats(a,allowed) for a in ['Q','Qplus']} for g,allowed in groups.items()},
        processing_seconds=sum(r['cost_summary']['total'] for r in rows),
        retained_observations=sum(r['retained_observations'] for r in success),
        source_observations=sum(r['source_observations'] for r in success),
        decisions={d:sum(r['decisions'][d] for r in success) for d in ('keep','uncertain','drop')},
        scheduler_failure=read(out/'failure.json') if (out/'failure.json').exists() else None,
        failed_attempts=[str(p) for p in (out/'attempts').glob('*/result.json') if read(p).get('error')],
        runtime='Q/Q+ pair shares keep-only filtering once; uncertain decisions archived but excluded from both readers; two new strict answers; no model extraction/plan/compose',
        source_limitation=policy['source_limitation'], evaluation_scope=policy['evaluation_scope'],
        cases=rows, updated_at=now())
    write(out/'report.json',result)
    return result


def worker(out, worker_id):
    verify_manifest(out)
    from script.run_evidence_planner_experiment import verify_model
    from script_core.budget import ModelWorker
    from script.gpu_queue import acquire
    verify_model(out)
    folder = out/'workers'/str(worker_id)
    if folder.exists():
        raise ValueError('Q worker already attempted')
    folder.mkdir(parents=True)
    def status(stage, **kwargs):
        write(folder/'status.json', dict(stage=stage, pid=os.getpid(), at=now(), **kwargs))
    cfg=read(out/'qa_config.json');policy=read(out/'experiment.json');backend=ModelWorker(cfg)
    def wait(details):
        if (out/'failure.json').exists():
            raise RuntimeError('Q stopped after another worker failure')
        status('waiting_for_gpu', **details)
    lease = acquire(policy['resource_policy'], wait)
    entered = False
    try:
        gpu,_ = lease.__enter__()
        entered = True
        os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid']
        os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
        status('loading',gpu=gpu['index'])
        started=time.monotonic();details=backend.call('prepare_vlm',{},600)
        write(folder/'setup.json',dict(seconds=time.monotonic()-started, gpu=gpu, details=details))
        status('validation',gpu=gpu['index'])
        validate_live(backend,cfg,folder/'validation')
        while True:
            item=claim(out,worker_id)
            if item is None:
                break
            index,uid=item;case=out/'cases'/uid
            for name,expected in read(case/'source_binding.json')['files'].items():
                if sha(case/name)!=expected:
                    raise ValueError('Q source changed after freeze')
            def progress(phase, **detail):
                status('query',gpu=gpu['index'],index=index,question_id=uid,phase=phase,**detail)
            target=out/'attempts'/uid
            run_case(backend,cfg,read(case/'question.json'),read(case/'sentences.json'),
                     read(case/'original_qa.json'),target,progress)
            publish(out,uid,target)
            print('Q PAIR COMPLETE',worker_id,index,uid,flush=True)
        status('complete',gpu=gpu['index'])
    except BaseException as exc:
        fail(out,worker_id,exc)
        status('failed',error=str(exc))
        raise
    finally:
        backend.close(graceful=True)
        if entered:
            lease.__exit__(None,None,None)
        os.environ.pop('VIDEOAGENT_GPU_LEASE',None)


def supervise(out):
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        verify_manifest(out)
        if (out/'launch.json').exists():
            raise ValueError('Q already launched; explicit recovery required')
        rows=[]
        for worker_id in range(read(out/'experiment.json')['workers']):
            log=(out/f'worker-{worker_id}.log').open('w')
            command=[sys.executable,'-u',str(out/'runtime/script/run_q_experiment.py'),'worker','--out',str(out),'--worker-id',str(worker_id)]
            process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True);log.close()
            rows.append(dict(id=worker_id,pid=process.pid,identity=identity(process.pid),command=command))
        write(out/'launch.json',dict(controller_pid=os.getpid(),workers=rows,at=now()))
        while True:
            statuses={str(r['id']):read(out/'workers'/str(r['id'])/'status.json') for r in rows
                      if (out/'workers'/str(r['id'])/'status.json').exists()}
            for row in rows:
                if identity(row['pid'])!=row['identity'] and statuses.get(str(row['id']),{}).get('stage') not in ('complete','failed'):
                    fail(out,row['id'],RuntimeError('Q worker exited without terminal status'))
            with locked(out):
                report=build_report(out)
            alive=any(identity(r['pid'])==r['identity'] for r in rows)
            state='running' if alive else 'complete' if report['complete'] and not report['scheduler_failure'] else 'failed'
            write(out/'queue_status.json',dict(state=state,controller_pid=os.getpid(),workers=statuses,
                effective_worker_count=len(rows),allowed_gpu_indices=['0','1'],updated_at=now(),
                error=report['scheduler_failure']))
            if not alive:
                return
            time.sleep(5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['prepare','supervise','worker'])
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--source',type=Path)
    parser.add_argument('--workers',type=int,default=4);parser.add_argument('--worker-id',type=int)
    args=parser.parse_args();out=args.out.resolve()
    if args.mode=='prepare':prepare(out,args.source.resolve(),args.workers)
    elif args.mode=='supervise':supervise(out)
    else:worker(out,args.worker_id)
