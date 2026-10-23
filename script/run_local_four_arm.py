"""Run the four dev-branch schemes on real EgoSchema inputs without PBS.

Uses the production experiment runner/gate and the ordinary scheme-3/4 CLI.
It prepares a small frozen input set, not the historical 300-question cohort.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from script.run_temporal_entity import ARMS, checked_packet, execute, gate, sha, work, write
from script_core.config import digest, load_config
from script_core.temporal_entity import SETTINGS


def prepare(output, count):
    marker = output / 'local_inputs.json'
    if marker.exists():
        if json.loads(marker.read_text())['count'] != count:
            raise ValueError('Existing input count differs; select a new --output directory')
        frozen=json.loads((output/'source_hashes.json').read_text())
        changed=any(sha(ROOT/name)!=expected for name,expected in frozen.items())
        if not changed:
            return
        if any(output.rglob('calls.jsonl')):
            raise ValueError('Frozen source changed after inference; select a new --output directory')
        # Input preparation has not incurred model calls, so it can be refreshed.
        marker.unlink()
    data = ROOT / 'data/EgoSchema/egoschema'
    annotations = json.loads((data / 'subset_anno.json').read_text())
    captions = json.loads((data / 'lavila_subset.json').read_text())
    ids = list(annotations)[:count]
    if len(ids) != count or count < 1:
        raise ValueError('Invalid number of public EgoSchema examples')
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / 'data_extraction/extract_images.py'),
                    '--video-ids', *ids], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT / 'data_extraction/extract_features.py'),
                    '--video-ids', *ids], cwd=ROOT, check=True)
    cfg = load_config(ROOT / 'configs/script.yaml')
    questions = []
    evaluation = {}
    from PIL import Image
    for uid in ids:
        row = annotations[uid]
        question = dict(question_id=uid, video_id=uid, question=row['question'],
                        options=[row[f'option {i}'] for i in range(5)])
        records = [dict(id=f'C{i}', interval=[i, i+1], text=text)
                   for i, text in enumerate(captions[uid])]
        feature = (ROOT / 'data/EgoSchema/egoschema_features' / f'{uid}.pt').resolve()
        meta = json.loads(feature.with_suffix('.json').read_text())
        if (meta['fps'] != 1 or meta['shape'] != [len(records), 1280]
                or meta['frame_indices'] != list(range(len(records)))
                or Path(meta['model']).resolve() != Path(cfg['models']['visual_encoder']).resolve()):
            raise ValueError(f'Feature grid or encoder provenance differs: {uid}')
        frames = []
        for i in range(len(records)):
            path = (ROOT / 'data/EgoSchema/egoschema_frames' / uid / f'{i}.jpg').resolve()
            with Image.open(path) as image:
                width, height = image.size
            frames.append(dict(frame_id=f'F{i}', pts=float(i), path=str(path),
                               width=width, height=height, sha256=sha(path)))
        packet = dict(question=question, captions=records, frames=frames,
                      caption_records_hash=digest(records), feature_uri=str(feature),
                      feature_hash=sha(feature), feature_metadata=meta)
        write(output / 'inputs' / f'{uid}.json', packet)
        questions.append(question)
        evaluation[uid] = {'truth': row['truth']}
    write(output / 'config.json', cfg)
    write(output / 'settings.json', SETTINGS)
    write(output / 'smoke/ids.json', ids)
    write(output / 'input_hashes.json', {
        str(p.relative_to(output)): sha(p) for p in (output / 'inputs').glob('*.json')})
    write(output / 'evaluation_only.json', evaluation)
    (output / 'questions.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in questions))
    names = subprocess.check_output(['git', 'ls-files', '*.py', 'configs/*.yaml'], cwd=ROOT, text=True).splitlines()
    names.append(str(Path(__file__).resolve().relative_to(ROOT)))
    write(output / 'source_hashes.json', {name: sha(ROOT/name) for name in sorted(set(names))})
    write(output / 'control_hashes.json', {name: sha(output/name) for name in
        ('config.json', 'settings.json', 'smoke/ids.json')})
    write(marker, dict(count=count, video_ids=ids, source_commit=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        scope='Small real-model functional validation; not the historical 300-question experiment'))
    print('PREPARED', count, 'real EgoSchema videos', flush=True)


def ordinary_query(output, scheme4=False):
    from script_qa import main as query_main
    pipeline='scheme4' if scheme4 else 'scheme3'
    args=['query', '--base', str(output), '--questions', str(output/'questions.jsonl'),
          '--config', str(ROOT/'configs/script.yaml'),
          '--out', str(output/pipeline), '--resume']
    if scheme4:args.append('--scheme4')
    return query_main(args)


def ordinary_scheme4(output):
    return ordinary_query(output, scheme4=True)


def worker(output, arm):
    from script_core.backend import preflight
    from script_core.budget import BudgetLedger, Meter, ModelWorker
    for name, expected in json.loads((output/'source_hashes.json').read_text()).items():
        if sha(ROOT/name) != expected:
            raise ValueError(f'Frozen source changed: {name}; use a new output directory')
    cfg, artifacts = preflight(load_config(output/'config.json'))
    write(output/'smoke/setup'/f'{arm}-artifacts.json', artifacts)
    ids = json.loads((output/'smoke/ids.json').read_text())
    profiles = ['summary_deep', 'temporal_cross'] if arm == 'summary_pair' else [arm]
    backend = ModelWorker(cfg)
    failed = False
    try:
        for uid in ids:
            for profile in profiles:
                target = output/'smoke/arms'/profile/'runs'/uid
                if not (target/'result.json').exists() and any(target.rglob('calls.jsonl')):
                    raise ValueError(f'Interrupted run retains spent budget: {target}; use a new output directory')
                result = execute(output, profile, uid, backend, cfg, smoke=True)
                failed |= bool(result.get('error'))
                print('TEMPORAL', profile, uid, 'answer=', (result.get('answer') or {}).get('selected_option'),
                      'error=', result.get('error'), flush=True)
        if arm == 'cluster_qa':
            from script_core.sparse_visual import probe
            target = output/'verification/sparse-visual-probe'
            if not (target/'status.json').exists():
                ledger = BudgetLedger(60., answer_reserve=0, calls_path=target/'calls.jsonl')
                meter = Meter(backend, ledger, cfg, target/'raw_calls')
                try:
                    value = probe(checked_packet(output, ids[0], check_images=False), meter, target)
                    value['cost_summary'] = ledger.summary()
                    write(target/'status.json', value)
                    failed |= ledger.budget_violation
                finally:
                    meter.close()
    finally:
        backend.close()
    return int(failed)


def parallel(output, count, gpu_ids, ordinary_arm='scheme3'):
    if not gpu_ids or len(set(gpu_ids)) != len(gpu_ids):
        raise ValueError('--gpus requires distinct idle GPU indices')
    def inventory():
        observed = subprocess.check_output(['nvidia-smi', '--query-gpu=index,uuid,memory.used,utilization.gpu',
            '--format=csv,noheader,nounits'], text=True)
        return {parts[0].strip(): (parts[1].strip(), int(parts[2]), int(parts[3]))
                for line in observed.splitlines() if len(parts := line.split(',')) == 4}
    def idle(gpu):
        available=inventory()
        return gpu in available and available[gpu][1] <= 500 and available[gpu][2] <= 5
    for gpu in gpu_ids:
        if not idle(gpu):
            raise ValueError(f'GPU {gpu} is occupied; select idle GPUs without interrupting other jobs')
    logs = output/'worker_logs'
    logs.mkdir(exist_ok=True)
    pending=['summary_pair','temporal_local','cluster_qa',ordinary_arm]
    jobs=[];statuses=[]
    def start(gpu,arm):
        # CUDA's default fastest-device ordering differs from nvidia-smi on
        # mixed A40/Ada hosts. UUIDs select the exact checked physical device.
        uuid=inventory()[gpu][0]
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=uuid, CUDA_DEVICE_ORDER='PCI_BUS_ID')
        with (logs/f'{arm}.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()),
                '--output', str(output), '--num-examples', str(count), '--arm', arm],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        jobs.append((arm, gpu, process))
        print('START', arm, 'GPU', gpu, uuid, 'PID', process.pid, flush=True)
    for gpu in gpu_ids:
        if pending:start(gpu,pending.pop(0))
    while jobs or pending:
        for job in jobs[:]:
            arm,gpu,process=job;code=process.poll()
            if code is None:continue
            jobs.remove(job)
            statuses.append(dict(arm=arm,gpu=gpu,exit_code=code))
            write(output/'worker_status.json',statuses)
            print('FINISH',arm,'GPU',gpu,'exit',code,flush=True)
        busy={gpu for _,gpu,_ in jobs}
        for gpu in gpu_ids:
            if pending and gpu not in busy and idle(gpu):start(gpu,pending.pop(0))
        if not jobs and pending:
            raise RuntimeError('Remaining GPUs became occupied; completed outputs are preserved')
        if jobs:time.sleep(1)
    write(output/'worker_status.json', statuses)
    if any(job['exit_code'] for job in statuses):
        return 1
    return 0 if gate(output) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'workdirs/dev-four-arm-smoke-r5')
    parser.add_argument('--num-examples', type=int, default=2)
    parser.add_argument('--arm', choices=['all', 'scheme3', 'scheme4', 'cluster_qa', 'temporal_local', 'summary_pair'], default='all')
    parser.add_argument('--scheme4', action='store_true', help='Enable scheme 4 for the ordinary query')
    parser.add_argument('--gpus', help='Idle GPU indices for parallel validation, e.g. 4,5,7')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.scheme4 and args.arm not in ('all','scheme3','scheme4'):
        parser.error('--scheme4 applies only to ordinary queries (--arm all/scheme3/scheme4)')
    cross_stage=args.scheme4 or args.arm=='scheme4'
    output = args.output.resolve()
    prepare(output, args.num_examples)
    if args.prepare_only:
        return 0
    if args.arm not in ('all', 'scheme3', 'scheme4'):
        return worker(output, args.arm)
    if args.arm == 'all' and args.gpus:
        return parallel(output, args.num_examples, args.gpus.split(','),
                        ordinary_arm='scheme4' if cross_stage else 'scheme3')
    if args.arm == 'all':
        work(output, 'summary_pair', 0, smoke=True)
    status = ordinary_query(output, scheme4=cross_stage)
    if status:
        return status
    if args.arm == 'all' and not gate(output):
        return 1
    print('VALIDATED', args.arm, output, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
