"""Run the unchanged VideoTree baseline on nested 100/300/500 cohorts."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASELINE_FILES = ['demo.py', 'adaptive_breath_expansion.py', 'depth_expansion.py',
                  'main_qa.py', 'dataset.py', 'model.py', 'util.py', 'prompts.py', 'eval.py',
                  'data_extraction/extract_images.py', 'data_extraction/extract_features.py',
                  'kmeans_pytorch/__init__.py']


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8*1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def nested_cohorts(all_ids, original_ids, seed=0, sizes=(100, 300, 500)):
    if len(set(all_ids)) != len(all_ids) or len(set(original_ids)) != len(original_ids):
        raise ValueError('Duplicate cohort IDs')
    if not set(original_ids) <= set(all_ids) or len(original_ids) != sizes[0] or sizes[-1] != len(all_ids):
        raise ValueError('Cohort sizes or original sample do not match the available dataset')
    remaining = sorted(set(all_ids)-set(original_ids))
    random.Random(seed).shuffle(remaining)
    ordered = list(original_ids)+remaining
    return {str(size): ordered[:size] for size in sizes}


def prepare(output, reuse_from, data_root=None, shards=4, streams_per_gpu=2, seed=0):
    output, previous = Path(output).resolve(), Path(reuse_from).resolve()
    if output.exists():
        raise ValueError('Choose a new directory; prepared inputs are immutable')
    if shards < 1 or streams_per_gpu < 1 or shards % streams_per_gpu:
        raise ValueError('Shards must divide evenly into GPU jobs')
    data = Path(data_root or ROOT/'data/EgoSchema/egoschema').resolve()
    annotations, captions = read(data/'subset_anno.json'), read(data/'lavila_subset.json')
    if len(annotations) != 500 or any(type(a.get('truth')) is not int or a['truth'] not in range(5)
            or uid not in captions or not (data/'videos'/f'{uid}.mp4').is_file() for uid, a in annotations.items()):
        raise ValueError('All 500 public questions, labels, videos and captions are required')
    old = read(previous/'pilot_spec.json')
    old_hashes = read(previous/'source_hashes.json')
    if any(sha(ROOT/name) != old_hashes[name] for name in BASELINE_FILES):
        raise ValueError('Baseline code differs; cannot mix historical and new VideoTree results')
    if sha(data/'lavila_subset.json') != old['shared_caption_sha256']:
        raise ValueError('Released captions differ from the original baseline')
    cohorts = nested_cohorts(list(annotations), old['video_ids'], seed)
    ordered, new_ids = cohorts['500'], cohorts['500'][100:]
    clean = {uid: {key: annotations[uid][key] for key in ('question', *[f'option {i}' for i in range(5)])}
             for uid in ordered}
    # Validate all reusable predictions and question text before creating output.
    original_results = {path.parent.name: path for path in previous.glob('shards/*/videotree_runs/*/summary.json')}
    for uid in cohorts['100']:
        summary = read(original_results[uid])
        examples = summary.get('examples', [])
        if len(examples) != 1 or examples[0].get('video_id') != uid or examples[0].get('answer') not in tuple('ABCDE'):
            raise ValueError(f'Invalid reusable VideoTree answer: {uid}')
        if (examples[0]['question'] != clean[uid]['question'] or
                examples[0]['options'] != {chr(65+i): clean[uid][f'option {i}'] for i in range(5)} or
                summary['model'] != old['model']):
            raise ValueError(f'Reused VideoTree inputs/model differ: {uid}')
    write(output/'cohorts.json', cohorts)
    write(output/'evaluation_only.json', {uid: annotations[uid]['truth'] for uid in ordered})
    write(output/'videotree_input.json', clean)
    questions = [{'question_id': uid, 'video_id': uid, 'question': clean[uid]['question'],
                  'options': [clean[uid][f'option {i}'] for i in range(5)]} for uid in ordered]
    (output/'questions.jsonl').write_text(''.join(json.dumps(q, ensure_ascii=False)+'\n' for q in questions))
    reused_hashes = {}
    (output/'videotree_runs').mkdir()
    for uid in cohorts['100']:
        original = original_results[uid].resolve()
        reused_hashes[str(original)] = sha(original)
        (output/'videotree_runs'/uid).symlink_to(original.parent, target_is_directory=True)
    write(output/'reuse_hashes.json', reused_hashes)
    for shard in range(shards):
        write(output/'shards'/str(shard)/'video_ids.json', new_ids[shard::shards])
    spec = {'created_at': now(), 'dataset': 'EgoSchema public labeled 500-question subset',
            'cohort_sizes': [100, 300, 500], 'seed': seed, 'shards': shards,
            'streams_per_gpu': streams_per_gpu, 'gpu_jobs': shards//streams_per_gpu,
            'data_root': str(data), 'model': old['model'], 'feature_model': old['feature_model'],
            'feature_batch_size': old['feature_batch_size'], 'max_new_tokens': old['shared_generation_max_new_tokens'],
            'baseline_seed': 0, 'baseline_temperature': 0, 'reused_questions': 100, 'new_questions': 400,
            'reuse_from': str(previous), 'previously_debugged_ids': old.get('excluded_debug_video_ids', []),
            'cohort_rule': 'Original 100 in unchanged order, then seeded shuffle of all remaining public questions; 100 is a subset of 300, which is a subset of 500.',
            'cost_notes': ['Pipeline seconds include original per-question model loading, tree construction and QA.',
                           'Frames/features are prepared separately; their measured stage times are reported separately.',
                           'Reused 100 retain their historical times and GPU contention; those times are not remeasured.',
                           'LaViLa caption generation cost is unavailable and excluded.',
                           'Overlapping cohorts are not independent trials; all are from the public subset.']}
    write(output/'scaling_spec.json', spec)
    files = BASELINE_FILES+['script/run_videotree_scaling.py', 'script/videotree_scaling.pbs', 'requirements.txt']
    source_hashes = {}
    for name in files:
        target = output/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, target)
        source_hashes[name] = sha(target)
    for name in ('data', 'checkpoints'):
        (output/'source'/name).symlink_to((ROOT/name).resolve(), target_is_directory=True)
    write(output/'source_hashes.json', source_hashes)
    input_names = ['cohorts.json', 'videotree_input.json', 'evaluation_only.json', 'questions.jsonl', 'scaling_spec.json']
    input_names += [f'shards/{shard}/video_ids.json' for shard in range(shards)]
    write(output/'input_hashes.json', {name: sha(output/name) for name in input_names})
    write(output/'dataset_hashes.json', {str(data/name): sha(data/name) for name in ('lavila_subset.json', 'duration.json')})
    score(output)
    return spec


def verify(output):
    output = Path(output)
    for name, expected in read(output/'source_hashes.json').items():
        if sha(output/'source'/name) != expected:
            raise ValueError('Frozen baseline source changed: '+name)
    for name, expected in read(output/'input_hashes.json').items():
        if sha(output/name) != expected:
            raise ValueError('Frozen cohort input changed: '+name)
    for name in ('reuse_hashes.json', 'dataset_hashes.json'):
        for path, expected in read(output/name).items():
            if sha(path) != expected:
                raise ValueError('Frozen baseline data/result changed: '+path)


def wilson(correct, total):
    if not total:
        return None
    p, z = correct/total, 1.96
    mid = (p+z*z/(2*total))/(1+z*z/total)
    half = z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/(1+z*z/total)
    return [max(0., mid-half), min(1., mid+half)]


def score(output):
    output = Path(output)
    cohorts, labels, spec = read(output/'cohorts.json'), read(output/'evaluation_only.json'), read(output/'scaling_spec.json')
    with (output/'score.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows, metrics = {}, {}
        for uid in cohorts['500']:
            directory = output/'videotree_runs'/uid
            row = {'question_id': uid, 'truth': labels[uid], 'prediction': None, 'correct': False,
                   'status': 'pending', 'pipeline_seconds': None, 'reused': uid in cohorts['100']}
            if (directory/'failure.json').exists():
                row.update(status='error', error=read(directory/'failure.json'))
            elif (directory/'summary.json').exists():
                summary = read(directory/'summary.json')
                examples = summary.get('examples', [])
                answer = examples[0].get('answer') if len(examples) == 1 else None
                if (not isinstance(answer, str) or answer not in tuple('ABCDE') or
                        examples[0].get('video_id') != uid):
                    row.update(status='error', error={'error': 'Missing or illegal answer'})
                else:
                    prediction = ord(answer)-65
                    row.update(status='completed', prediction=prediction, correct=prediction == labels[uid],
                               pipeline_seconds=summary['seconds'], selected_frames=examples[0]['selected_frames'])
            rows[uid] = row
        for size, ids in cohorts.items():
            finished = [rows[uid] for uid in ids if rows[uid]['status'] != 'pending']
            correct = sum(row['correct'] for row in finished)
            seconds = sorted(row['pipeline_seconds'] for row in finished if row['pipeline_seconds'] is not None)
            import statistics
            metrics[size] = {'planned': len(ids), 'completed': len(finished), 'pending': len(ids)-len(finished),
                             'correct': correct, 'errors': sum(row['status'] == 'error' for row in finished),
                             'accuracy_on_completed': correct/len(finished) if finished else None,
                             'final_accuracy': correct/len(ids) if len(finished) == len(ids) else None,
                             'wilson_95_interval': wilson(correct, len(ids)) if len(finished) == len(ids) else None,
                             'mean_pipeline_seconds': statistics.mean(seconds) if seconds else None,
                             'median_pipeline_seconds': statistics.median(seconds) if seconds else None}
        report = {'updated_at': now(), 'complete': metrics['500']['pending'] == 0,
                  'groups': metrics, 'cost_notes': spec['cost_notes'], 'rows': list(rows.values())}
        write(output/'comparison.json', report)
        csv = ['question_id,in_100,in_300,in_500,truth,prediction,correct,status,pipeline_seconds,reused']
        for uid, row in rows.items():
            csv.append(','.join(map(str, [uid, uid in cohorts['100'], uid in cohorts['300'], True, row['truth'],
                row['prediction'] if row['prediction'] is not None else '', row['correct'], row['status'],
                row['pipeline_seconds'] if row['pipeline_seconds'] is not None else '', row['reused']])))
        (output/'comparison.csv').write_text('\n'.join(csv)+'\n')
    return report


def stage_command(directory, phase, arguments):
    started = time.monotonic()
    result = subprocess.run([sys.executable, *map(str, arguments)], cwd=ROOT, check=False)
    write(directory/f'{phase}.json', {'phase': phase, 'returncode': result.returncode,
          'wall_seconds': time.monotonic()-started, 'ended_at': now()})
    return result.returncode


def run_shard(output, shard):
    output = Path(output).resolve()
    verify(output)
    spec = read(output/'scaling_spec.json')
    if not 0 <= shard < spec['shards']:
        raise ValueError('Shard outside prepared experiment')
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('Run inside a single-GPU PBS allocation')
    from demo import run_pipeline
    from data_extraction.extract_images import extract_video
    directory, data = output/'shards'/str(shard), Path(spec['data_root'])
    clean, captions = read(output/'videotree_input.json'), read(data/'lavila_subset.json')
    cohorts, ids = read(output/'cohorts.json'), read(directory/'video_ids.json')
    failed = False
    with (directory/'run.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for size in ('300', '500'):
            stage_ids = [uid for uid in ids if (uid in cohorts['300']) == (size == '300')
                         and not any((output/'videotree_runs'/uid/name).exists() for name in ('summary.json', 'failure.json'))]
            ready, cache_before = [], {}
            started = time.monotonic()
            for uid in stage_ids:
                frames, features = ROOT/'data/EgoSchema/egoschema_frames'/uid, ROOT/'data/EgoSchema/egoschema_features'/f'{uid}.pt'
                cache_before[uid] = {'frames': (frames/'frames.json').exists(), 'features': features.exists()}
                write(directory/'status.json', {'phase': 'frames', 'cohort': size, 'video_id': uid, 'updated_at': now()})
                try:
                    extract_video(data/'videos'/f'{uid}.mp4', frames, len(captions[uid]))
                    ready.append(uid)
                except Exception as exc:
                    failed = True
                    write(output/'videotree_runs'/uid/'failure.json', {'phase': 'frames', 'error': str(exc), 'ended_at': now()})
            write(directory/f'frames-{size}.json', {'wall_seconds': time.monotonic()-started, 'cache_before': cache_before})
            if ready:
                write(directory/'status.json', {'phase': 'features', 'cohort': size, 'updated_at': now()})
                arguments = [ROOT/'data_extraction/extract_features.py', '--model', spec['feature_model'],
                             '--batch-size', str(spec['feature_batch_size']), '--video-ids']
                rc = stage_command(directory, f'features-{size}', [*arguments, *ready])
                if rc:
                    # One damaged input must not turn its whole shard into failures.
                    accepted = []
                    for uid in ready:
                        if stage_command(directory, f'feature-recovery-{uid}', [*arguments, uid]) == 0:
                            accepted.append(uid)
                        else:
                            failed = True
                            write(output/'videotree_runs'/uid/'failure.json', {'phase': 'features', 'error': 'Feature extraction failed; see worker.log', 'ended_at': now()})
                    ready = accepted
            score(output)
            for uid in ready:
                out = output/'videotree_runs'/uid
                write(directory/'status.json', {'phase': 'videotree_query', 'cohort': size, 'video_id': uid, 'updated_at': now()})
                started = time.monotonic()
                try:
                    args = SimpleNamespace(skip_preprocess=True, model=spec['model'], feature_model=spec['feature_model'],
                                           feature_batch_size=spec['feature_batch_size'], max_new_tokens=spec['max_new_tokens'])
                    run_pipeline(args, data, out, {uid: clean[uid]}, data/'lavila_subset.json')
                except Exception as exc:
                    failed = True
                    write(out/'failure.json', {'phase': 'query', 'error': str(exc), 'wall_seconds': time.monotonic()-started, 'ended_at': now()})
                score(output)
            write(directory/'status.json', {'phase': 'cohort_shard_finished', 'cohort': size, 'updated_at': now()})
    write(directory/'status.json', {'phase': 'finished', 'errors': failed, 'ended_at': now()})
    return int(failed)


def run_job(output, job_index):
    output = Path(output).resolve()
    verify(output)
    spec = read(output/'scaling_spec.json')
    if not 0 <= job_index < spec['gpu_jobs']:
        raise ValueError('Invalid GPU job index')
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('Each GPU job needs its own single-GPU allocation')
    if spec['streams_per_gpu'] > 1 and torch.cuda.get_device_properties(0).total_memory < 120*1024**3:
        raise RuntimeError('Two VideoTree streams require at least 120 GiB GPU memory')
    shards = list(range(job_index, spec['shards'], spec['gpu_jobs']))
    status = {'job_id': os.environ.get('PBS_JOBID'), 'node': socket.gethostname(),
              'gpu': torch.cuda.get_device_name(0), 'shards': shards, 'started_at': now(), 'status': 'running'}
    write(output/'jobs'/f'job-{job_index}-status.json', status)
    processes = []
    with ExitStack() as stack:
        for shard in shards:
            log = stack.enter_context((output/'shards'/str(shard)/'worker.log').open('a', buffering=1))
            processes.append((shard, subprocess.Popen([sys.executable, str(ROOT/'script/run_videotree_scaling.py'),
                'run-shard', '--out', str(output), '--index', str(shard)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)))
        status['returncodes'] = {str(shard): process.wait() for shard, process in processes}
    status.update(status='finished', ended_at=now())
    write(output/'jobs'/f'job-{job_index}-status.json', status)
    score(output)
    return int(any(status['returncodes'].values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run-job', 'run-shard', 'score'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--reuse-from')
    parser.add_argument('--index', type=int, default=0)
    args = parser.parse_args()
    if args.action == 'prepare':
        print(json.dumps(prepare(args.out, args.reuse_from), indent=2))
    elif args.action == 'score':
        print(json.dumps(score(args.out)['groups'], indent=2))
    else:
        return (run_job if args.action == 'run-job' else run_shard)(args.out, args.index)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
