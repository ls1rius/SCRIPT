"""Resume only final Q6 validation, using its immutable runtime and saved adapter."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback


def configure(control):
    cfg = json.loads((control/'config.json').read_text())
    if cfg['allowed_gpu_indices'] != ['0', '2'] or cfg['workers'] != 6:
        raise ValueError('Q6 validation requires the authorized GPU0/2 bounded pool')
    sys.path.insert(0, cfg['runtime'])
    return cfg


def audit_result(out, uid, target):
    from script.scale_live_parallel_pool import read
    from script_core.config import digest
    from script_core import q_evidence as q
    case = out/'evaluation/cases'/uid
    question = read(case/'question.json')
    result = read(target/'result.json')
    if result['question_id'] != uid or not result['complete'] or result['error']:
        raise ValueError('Incomplete or foreign saved validation result')
    if set(result['arms']) != {'Q', 'Qplus'} or list(target.rglob('*.pending.json')):
        raise ValueError('Missing reader or pending saved call')
    retained = read(target/'retained.json')
    if retained != q.selected_observations(read(target/'sources.json'), read(target/'selection.json')):
        raise ValueError('Saved keep-only evidence mismatch')
    evidence = digest(retained)
    if result['retained_evidence_hash'] != evidence or result['cost_summary']['budget_violation']:
        raise ValueError('Saved evidence or budget mismatch')
    for arm in ['Q', 'Qplus']:
        row = result['arms'][arm]
        if row['retained_evidence_hash'] != evidence:
            raise ValueError('Reader evidence mismatch')
        if str(row['answer']['selected_option']) not in {str(i) for i in range(len(question['options']))}:
            raise ValueError('Reader answer outside original options')
        if read(target/arm/'final_input.json') != q.final_input(read(case/'original_qa.json'), retained, arm == 'Qplus'):
            raise ValueError('Original reader input mismatch')
    calls = [json.loads(line) for line in (target/'calls.jsonl').read_text().splitlines()]
    if any(c['error'] or not Path(c['output_ref']).is_file() for c in calls):
        raise ValueError('Failed or missing saved raw call')
    if abs(sum(c['actual_cost'] for c in calls)-result['cost_summary']['total']) > 1e-6:
        raise ValueError('Saved call cost mismatch')
    return result


def publish(out, uid, target):
    # The original publisher and claim protocol remain frozen. Supply its missing parent.
    from script import run_q_experiment as base
    (out/'evaluation/runs').mkdir(parents=True, exist_ok=True)
    base.publish(out/'evaluation', uid, target)


def verify(control, cfg):
    from script.scale_live_parallel_pool import verify_manifest, read, sha
    verify_manifest(Path(cfg['out']))
    verify_manifest(control)
    for name, expected in read(control/'protected_hashes.json').items():
        if sha(Path(cfg['out'])/name) != expected:
            raise ValueError('Protected completed artifact changed: '+name)


def prepare(out, control):
    if control.exists():
        raise ValueError('New control directory required')
    sys.path.insert(0, str(out/'runtime'))
    from script.scale_live_parallel_pool import read, write, sha, now, identity, verify_manifest
    from script import run_q_experiment as base
    verify_manifest(out)
    training = read(out/'training/result.json')
    if not training['complete'] or training['epochs'] != 3 or training['completed_groups'] != 600 or training['rollouts'] != 1200:
        raise ValueError('Completed fixed3 training required; this entry never trains')
    if sha(out/'adapter/adapter.safetensors') != training['adapter']['weights_sha256']:
        raise ValueError('Final adapter changed')
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read(out/'queue_status.json')
        if identity(state['controller_pid']) is not None or any(identity(s['pid']) is not None for s in state['workers'].values()):
            raise ValueError('Old controller or worker still alive')
        if state['state'] != 'failed' or not (out/'failure.json').exists():
            raise ValueError('Expected stopped failed validation')
        claims = read(out/'evaluation/claims.json')
        pending = [uid for uid, row in claims.items() if row['state'] != 'complete']
        for uid in pending:
            audit_result(out, uid, out/'evaluation/attempts'/uid)
        control.mkdir()
        shutil.copy2(__file__, control/'runner.py')
        cfg = dict(out=str(out), runtime=str(out/'runtime'), allowed_gpu_indices=['0', '2'], workers=6,
                   minimum_free_mib=24576, physical_pool_mib=22528, stable_observations=3,
                   epochs=3, training_action='none; final adapter evaluation only')
        write(control/'config.json', cfg)
        for name in ['failure.json', 'queue_status.json', 'report.json', 'evaluation/claims.json']:
            dest = control/'history'/name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out/name, dest)
        protected = {}
        files = [out/'training/result.json', out/'training/status.json', out/'training/history.json', out/'adapter_ready.json']
        files += list((out/'adapter').iterdir()) + list((out/'training/groups').glob('*.json'))
        for uid in claims:
            files += [f for f in (out/'evaluation/attempts'/uid).rglob('*') if f.is_file()]
        for f in files:
            protected[str(f.relative_to(out))] = sha(f)
        write(control/'protected_hashes.json', protected)
        for uid in pending:
            publish(out, uid, out/'evaluation/attempts'/uid)
        # Keep the original exception and all original logs in history; clear only the stop sentinel.
        (out/'failure.json').unlink()
        write(control/'recovery.json', dict(republished=pending, model_calls_repeated=0, training_restarted=False, at=now()))
        write(control/'frozen_hashes.json', {str(f.relative_to(control)):sha(f) for f in control.rglob('*') if f.is_file()})
        verify(control, cfg)
        write(out/'queue_status.json', dict(state='prepared', stage='validation300', resource_controller=str(control),
              allowed_gpu_indices=['0', '2'], user_reserved_gpu_indices=['1', '3'], controller_pid=None, updated_at=now()))


class Drained(Exception):
    pass


def worker(control, wid):
    cfg = configure(control)
    from script.scale_live_parallel_pool import read, write, now, sha
    from script.gpu_queue import acquire
    from script_core import budget
    from script import run_q_experiment as base
    from script.q_v4_validation import validate_live
    from script.run_q_v4_experiment import run_case
    out = Path(cfg['out'])
    verify(control, cfg)
    folder = control/'workers'/str(wid)
    def status(stage, **detail):
        write(folder/'status.json', dict(stage=stage, pid=os.getpid(), at=now(), **detail))
    def waiting(detail):
        if (out/'failure.json').exists():
            raise RuntimeError('Validation halted')
        if len(read(out/'evaluation/claims.json')) == 300:
            raise Drained()
        status('waiting_for_gpu', **detail)
    backend = None
    try:
        policy = dict(preference=cfg['allowed_gpu_indices'], minimum_free_mib=cfg['minimum_free_mib'],
                      stable_observations=cfg['stable_observations'], admission_mode='memory_shared', maximum_utilization=100)
        while True:
            try:
                with acquire(policy, waiting) as (gpu, _):
                    os.environ['CUDA_VISIBLE_DEVICES'] = gpu['uuid']
                    os.environ['VIDEOAGENT_GPU_LEASE'] = gpu['reservation_id']
                    model_cfg = read(out/'evaluation/qa_config.json')
                    backend = budget.ModelWorker(model_cfg)
                    status('loading', gpu=gpu['index'])
                    try:
                        setup = backend.call('prepare_vlm', {}, 600)
                    except Exception as exc:
                        if 'out of memory' in str(exc) and 'videoagent_install_pool' in str(exc):
                            write(folder/'allocation_failures'/f'{time.time_ns()}.json', dict(error=str(exc), gpu=gpu, at=now(), formal_job_claimed=False))
                            backend.close(graceful=True)
                            backend = None
                            continue
                        raise
                    write(folder/'setup.json', dict(gpu=gpu, details=setup, final_adapter_sha256=sha(out/'adapter/adapter.safetensors'), at=now()))
                    status('evaluation_gate', gpu=gpu['index'], model_pid=backend.process.pid)
                    validate_live(backend, model_cfg, folder/'validation')
                    while not (out/'failure.json').exists():
                        item = base.claim(out/'evaluation', wid)
                        if item is None:
                            break
                        index, uid = item
                        case = out/'evaluation/cases'/uid
                        if any(sha(case/n) != h for n,h in read(case/'source_binding.json')['files'].items()):
                            raise ValueError('Frozen validation source changed')
                        def progress(phase, **detail):
                            status('query', gpu=gpu['index'], model_pid=backend.process.pid, index=index, question_id=uid, phase=phase, **detail)
                        target = out/'evaluation/attempts'/uid
                        run_case(backend, model_cfg, read(case/'question.json'), read(case/'sentences.json'),
                                 read(case/'original_qa.json'), target, read(case/'archive.json'), 'adaptive', progress)
                        audit_result(out, uid, target)
                        publish(out, uid, target)
                    status('complete', gpu=gpu['index'])
                    return
            finally:
                if backend is not None:
                    backend.close(graceful=True)
                    backend = None
    except Drained:
        status('complete', reason='queue_drained_before_admission')
    except BaseException as exc:
        from script.q6_parallel_rl import fail
        fail(out, wid, exc)
        write(folder/'exception.json', dict(error=str(exc), traceback=traceback.format_exc(), at=now()))
        status('failed', error=str(exc))
        raise


def supervise(control):
    cfg = configure(control)
    from script.scale_live_parallel_pool import read, write, now, identity
    from script.train_q6_rl import evaluation_report
    from script.q6_parallel_rl import fail
    out = Path(cfg['out'])
    verify(control, cfg)
    if os.environ.get('VIDEOAGENT_PHYSICAL_CUDA_POOL') != '1':
        raise ValueError('Physical CUDA pool required')
    children = {}
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (control/'launch.json').exists() or (out/'failure.json').exists():
            raise ValueError('Already launched or halted')
        write(control/'launch.json', dict(pid=os.getpid(), identity=identity(os.getpid()), at=now()))
        rows = []
        for wid in range(cfg['workers']):
            command = [sys.executable, '-u', str(control/'runner.py'), 'worker', '--control', str(control), '--worker-id', str(wid)]
            env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                       OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', CUDA_DEVICE_ORDER='PCI_BUS_ID')
            with (control/f'worker-{wid}.log').open('x') as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            children[wid] = child
            rows.append(dict(id=wid, pid=child.pid, identity=identity(child.pid)))
            write(control/'workers_launch.json', rows)
        while True:
            for wid, child in children.items():
                if child.poll() not in (None, 0) and not (out/'failure.json').exists():
                    fail(out, wid, RuntimeError('Validation worker exited '+str(child.returncode)))
            states = {str(w):read(control/'workers'/str(w)/'status.json') for w in children if (control/'workers'/str(w)/'status.json').exists()}
            error = read(out/'failure.json') if (out/'failure.json').exists() else None
            evaluation = evaluation_report(out)
            live = any(c.poll() is None for c in children.values())
            complete = evaluation['complete'] and not live and not error
            if not live and not complete and not error:
                fail(out, 'controller', RuntimeError('Validation exited before completion'))
                error = read(out/'failure.json')
            stage = 'failed' if error else 'complete' if complete else 'validation300'
            write(out/'queue_status.json', dict(state='failed' if error else 'complete' if complete else 'running', stage=stage,
                  controller_pid=os.getpid(), workers=states, allowed_gpu_indices=cfg['allowed_gpu_indices'], user_reserved_gpu_indices=['1', '3'],
                  maximum_model_workers=cfg['workers'], resource_controller=str(control), primary_arm='Qplus', updated_at=now()))
            write(out/'report.json', dict(stage=stage, complete=complete, primary_arm='Qplus', epochs=3,
                  training_progress=read(out/'training/status.json'), training=read(out/'training/result.json'), evaluation=evaluation,
                  failure=error, resource_controller=str(control), updated_at=now()))
            if not live:
                return
            time.sleep(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'supervise', 'worker'])
    parser.add_argument('--out', type=Path)
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--worker-id', type=int)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.out.resolve(), args.control.resolve())
    elif args.action == 'supervise':
        supervise(args.control.resolve())
    else:
        worker(args.control.resolve(), args.worker_id)
