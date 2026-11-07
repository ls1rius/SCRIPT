"""I: evaluate reversible merged presentation of a sealed H evidence snapshot."""
import argparse
from copy import deepcopy
import fcntl
import importlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import controlled_memory_io as io

ARM = 'I_merged_lazy_script'
PARENT_ARM = 'H_generalized_lazy'


def verify(out):
    policy = io.read(out/'experiment.json')
    if policy.get('parent_memory_variant') != 'H' or not policy.get('parent_H_run'):
        raise ValueError('Use the frozen I-v1 entry for legacy F-derived I runs; current I requires H')
    for name, expected in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name) != expected:
            raise ValueError('Frozen I artifact changed: '+name)


def load_core():
    # A frozen control directory carries both modules beside this entry point.
    path = Path(__file__).resolve().parent/'merged_lazy_memory.py'
    if path.exists():
        return importlib.import_module('merged_lazy_memory')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    return importlib.import_module('script_core.merged_lazy_memory')


def project(parent_input, payload):
    core = load_core()
    memory = core.materialize(payload)
    if core.restore(memory) != payload:
        raise ValueError('I failed to restore H observations')
    before, _ = core.parent_description(payload)
    prefix, original, suffix = io.parts(parent_input)
    if before != original:
        raise ValueError('Archived H observations differ from actual H final input')
    description, audit = core.render(memory)
    inputs = deepcopy(parent_input)
    inputs['prompts'][0] = prefix + description + suffix
    if io.fixed(inputs) != io.fixed(parent_input):
        raise ValueError('I changed QA content outside Description')
    return inputs, memory, audit


def prepare(reference, out, gpu_index='3'):
    if out.exists():
        raise ValueError('Use a fresh I experiment directory')
    if io.read(reference/'experiment.json').get('arm') != PARENT_ARM:
        raise ValueError('I must derive from a sealed H experiment')
    for name, expected in io.read(reference/'frozen_hashes.json').items():
        if io.sha(reference/name) != expected:
            raise ValueError('Frozen H artifact changed: '+name)
    if not (reference/'validation/functional.json').exists():
        raise ValueError('H model functional gate and sealed H answers must complete before preparing I')
    if not io.read(reference/'validation/functional.json')['passed']:
        raise ValueError('H functional gate did not pass')
    ids = io.read(reference/'cohort.json')
    if len(set(ids)) != len(ids) or not ids:
        raise ValueError('Invalid H cohort')
    # Preflight dependencies before creating output; never substitute F evidence.
    for uid in ids:
        if not all((reference/path).is_file() for path in [
                f'materialized/{uid}/sealed.json', f'final_inputs/{PARENT_ARM}/{uid}.json',
                f'runs/{PARENT_ARM}/{uid}/result.json']):
            raise ValueError('H sealed evidence and answer not ready for I: '+uid)
    out.mkdir(parents=True)
    for name in ['cohort.json', 'evaluation_only.json', 'historical_only.json',
                 'qa_config.json', 'qa_expected_artifacts.json']:
        shutil.copy2(reference/name, out/name)
    for name in ['packets', 'final_inputs/A_original']:
        shutil.copytree(reference/name, out/name)
    shutil.copytree(reference/'runtime/qa', out/'runtime/qa', symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    control = out/'control'
    control.mkdir()
    shutil.copy2(Path(__file__), control/Path(__file__).name)
    shutil.copy2(Path(__file__).parent/'gpu_queue.py', control/'gpu_queue.py')
    shutil.copy2(Path(load_core().__file__), control/'merged_lazy_memory.py')
    for name in ['compact_protocol.py', 'controlled_memory_io.py', 'qa_evidence_view.py']:
        shutil.copy2(reference/'control'/name, control/name)
    for uid in ids:
        source = reference/'materialized'/uid
        for name, expected in io.read(source/'sealed.json').items():
            if io.sha(source/name) != expected:
                raise ValueError('Sealed H evidence changed: '+uid+'/'+name)
        parent_file = reference/'final_inputs'/PARENT_ARM/f'{uid}.json'
        result_file = reference/'runs'/PARENT_ARM/uid/'result.json'
        parent_result = io.read(result_file)
        if parent_result['input_sha256'] != io.sha(parent_file):
            raise ValueError('H answer is not bound to this final input: '+uid)
        began = time.process_time()
        parent_input = io.read(parent_file)
        payload = io.read(source/'memory.json')
        inputs, memory, audit = project(parent_input, payload)
        audit['materialization_cpu_seconds'] = time.process_time()-began
        audit['retained_original_rows'] = io.read(source/'audit.json')['retained_original_rows']
        folder = out/'materialized'/uid
        io.write(folder/'memory.json', memory)
        io.write(folder/'audit.json', audit)
        io.write(folder/'H_field_plan.json', io.read(source/'plan.json'))
        io.write(out/'source_inputs'/PARENT_ARM/f'{uid}.json', parent_input)
        io.write(out/'reference_results'/PARENT_ARM/f'{uid}.json', parent_result)
        target = out/'final_inputs'/ARM/f'{uid}.json'
        io.write(target, inputs)
        io.write(out/'input_seals'/f'{uid}.json', dict(
            parent_input=str(parent_file), parent_input_sha256=io.sha(parent_file),
            parent_result_sha256=io.sha(result_file), parent_memory_sha256=io.sha(source/'memory.json'),
            storage={name: io.sha(folder/name) for name in ['memory.json','audit.json','H_field_plan.json']},
            inputs={ARM: io.sha(target)}, parent_evidence_unchanged=True))
    parent_report = io.read(reference/'report.json')
    io.write(out/'experiment.json', dict(created_at=io.now(), arm=ARM, planned=len(ids),
        parent_H_run=str(reference), parent_H_report_sha256=io.sha(reference/'report.json'),
        version=load_core().VERSION, render_version=load_core().RENDER_VERSION, parent_memory_variant='H',
        state_representation='entity_description', fixed_state_dimensions=False,
        fixed='Only Description presentation changes; same H fields, captions, old memory, accepted visuals, QA and parser.',
        grouping='Exact identical visible assertions across all selected samples, ordered by first observation.',
        interval_presentation='Coalesce exactly adjacent intervals of the same assertion; retain gaps and original source intervals.',
        source_policy='Every caption ID, frame index and discrete sample interval is restorable; no new source or hidden truncation.',
        semantic_policy='No synonym merging, identity inference, continuous duration or event count inference.',
        inherited_construction_seconds=parent_report.get('measured_seconds_by_stage',{}).get('materialized',0),
        inherited_construction_totals=parent_report.get('totals',{}),
        inherited_construction_scope='All parent H task planning/extraction/materialization; reused evidence is not free.',
        new_extraction_model_calls=0, answers_reused=False, development_cohort=True,
        resource_policy=dict(gpu_index=str(gpu_index), minimum_free_mib=32768, maximum_utilization=10, stable_observations=3)))
    io.write(out/'validation/input_contract.json', dict(passed=True, checked=len(ids),
        source_payload_round_trip=True, original_H_render_reproduced=True,
        fixed_QA_regions_unchanged=True, all_source_occurrences_preserved=True,
        model_calls=0, updated_at=io.now()))
    # Runtime files and all prepared inputs are immutable; report/state are not.
    io.write(out/'frozen_hashes.json', {str(p.relative_to(out)): io.sha(p)
        for p in out.rglob('*') if p.is_file() and not p.is_symlink()})
    verify(out)
    report(out)
    io.write(out/'queue_status.json', dict(state='prepared', updated_at=io.now()))
    return out


def stats(rows, ids, truth, planned):
    correct = sum(not rows[u].get('error') and str(rows[u]['selected_option'])==str(truth[u]) for u in ids)
    tokens = [rows[u]['input_tokens'] for u in ids if rows[u].get('input_tokens') is not None]
    seconds = [rows[u]['seconds'] for u in ids if rows[u].get('seconds') is not None]
    return dict(completed=len(ids), planned=planned, pending=planned-len(ids), correct=correct,
        errors=sum(bool(rows[u].get('error')) for u in ids),
        accuracy=correct/planned if len(ids)==planned else None,
        accuracy_among_completed=correct/len(ids) if ids else None,
        mean_input_tokens=statistics.mean(tokens) if tokens else None,
        mean_qa_seconds=statistics.mean(seconds) if seconds else None)


def report(out):
    ids = io.read(out/'cohort.json')
    truth = io.read(out/'evaluation_only.json')
    policy = io.read(out/'experiment.json')
    rows = {u: io.read(p) for u in ids if (p:=out/'runs'/ARM/u/'result.json').exists()}
    parent = {u: io.read(out/'reference_results'/PARENT_ARM/f'{u}.json') for u in ids}
    paired = [u for u in ids if u in rows]
    good = lambda rs,u: not rs[u].get('error') and str(rs[u]['selected_option'])==str(truth[u])
    audits = [io.read(out/'materialized'/u/'audit.json') for u in ids]
    keys = ['source_rows','covered_source_rows','rendered_rows','merged_groups',
            'duplicate_assertion_rows_removed','original_description_characters',
            'merged_description_characters','materialization_cpu_seconds']
    costs = {stage: sum(float(json.loads(line).get('actual_cost') or 0)
        for path in (out/stage).rglob('calls.jsonl') for line in path.read_text().splitlines())
        for stage in ['setup','runs']}
    summary = dict(updated_at=io.now(), planned=len(ids), complete=len(rows)==len(ids),
        arms={ARM: stats(rows, list(rows), truth, len(ids))},
        version=policy['version'], parent_memory_variant='H',
        state_representation='entity_description', fixed_state_dimensions=False,
        primary_comparison='I_vs_H_generalized_lazy', paired_completed=len(paired),
        paired={PARENT_ARM: stats(parent,paired,truth,len(ids)), ARM: stats(rows,paired,truth,len(ids))},
        comparisons=dict(I_vs_H_generalized_lazy=dict(paired=len(paired),
            corrected=[u for u in paired if good(rows,u) and not good(parent,u)],
            regressed=[u for u in paired if good(parent,u) and not good(rows,u)])),
        prepared_inputs=len(audits), projection={k:sum(a[k] for a in audits) for k in keys},
        questions_with_merging=sum(a['source_rows_merged'] for a in audits),
        inherited_construction_seconds=policy['inherited_construction_seconds'],
        inherited_construction_scope=policy['inherited_construction_scope'],
        new_extraction_model_calls=0, new_measured_seconds_by_stage=costs,
        input_contract_passed=io.read(out/'validation/input_contract.json')['passed'],
        answers_reused=False, development_cohort=True,
        timing_policy='Observed shared-GPU seconds; not a controlled speed comparison.')
    io.write(out/'report.json', summary)
    return summary


def worker(out, start=0, end=None):
    verify(out)
    if not io.read(out/'validation/input_contract.json')['passed']:
        raise ValueError('I input contract has not passed')
    ids = io.read(out/'cohort.json')
    end = len(ids) if end is None else end
    if not 0 <= start <= end <= len(ids):
        raise ValueError('Invalid I question range')
    # Refuse any partially spent or changed artifact; no answer retry/reuse.
    for uid in ids[start:end]:
        seal = io.read(out/'input_seals'/f'{uid}.json')
        for name, expected in seal['storage'].items():
            if io.sha(out/'materialized'/uid/name) != expected:
                raise ValueError('I storage seal changed')
        if io.sha(out/'final_inputs'/ARM/f'{uid}.json') != seal['inputs'][ARM]:
            raise ValueError('I final input seal changed')
    sys.path.insert(0, str(out/'control'))
    protocol = importlib.import_module('compact_protocol')
    protocol.io.ARMS = (ARM,)
    protocol.io.ORDERS = ((ARM,),)*6
    try:
        return protocol.qa_worker(out, start, end, 'I_merged_qa')
    finally:
        report(out)


def queue(out):
    verify(out)
    policy = io.read(out/'experiment.json')['resource_policy']
    with (out/'controller.lock').open('a') as controller:
        fcntl.flock(controller, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():
            raise ValueError('I worker already attempted; inspect the retained run')
        from gpu_queue import acquire
        def waiting(details):
            io.write(out/'queue_status.json',dict(state='waiting_for_gpu',
                controller_pid=os.getpid(),updated_at=io.now(),**details))
        with acquire(policy, waiting) as (device, lease_fds):
            gpu=device['index'];uuid=device['uuid']
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=uuid, CUDA_DEVICE_ORDER='PCI_BUS_ID',
                HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
            command = [sys.executable, '-u', str(Path(__file__).resolve()), 'worker', '--out', str(out)]
            (out/'logs').mkdir(exist_ok=True)
            with (out/'logs/worker.log').open('a') as log:
                proc = subprocess.Popen(command, cwd=out, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, pass_fds=lease_fds)
            launch = dict(controller_pid=os.getpid(), worker_pid=proc.pid, gpu_index=gpu, gpu_uuid=uuid, command=command, started_at=io.now())
            io.write(out/'launch.json', launch)
            while proc.poll() is None:
                report(out)
                io.write(out/'queue_status.json', dict(launch,state='running',updated_at=io.now()))
                time.sleep(5)
            summary = report(out)
            state = 'complete' if not proc.returncode and summary['complete'] else 'failed'
            io.write(out/'queue_status.json', dict(launch,state=state,returncode=proc.returncode,updated_at=io.now()))
            return proc.returncode or (0 if summary['complete'] else 2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare','worker','queue','report'])
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--gpu-index', default='3')
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--end', type=int)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if args.mode=='prepare':
        if args.reference is None:
            parser.error('prepare requires --reference pointing to frozen H')
        prepare(args.reference.resolve(), out, args.gpu_index)
    elif args.mode=='worker':
        return worker(out, args.start, args.end)
    elif args.mode=='queue':
        return queue(out)
    else:
        verify(out)
        print(json.dumps(report(out), ensure_ascii=False, indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
