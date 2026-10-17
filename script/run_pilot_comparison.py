"""Prepare, run and independently score a paired SCRIPT/VideoTree GPU pilot."""
import argparse
from contextlib import ExitStack
import datetime
import fcntl
import hashlib
import json
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


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def prepare(output, count=100, shards=2, seed=0, data_root=None,
            feature_batch_size=16, embedding_batch_size=8, streams_per_gpu=1, reuse_from=None,
            video_ids=None):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Choose a new output directory; existing pilot inputs are immutable.')
    data = Path(data_root or ROOT/'data/EgoSchema/egoschema').resolve()
    annotations, captions = read(data/'subset_anno.json'), read(data/'lavila_subset.json')
    excluded = {'0074f737-11cb-497d-8d07-77c3a8127391', '0a01d7d0-11d6-4af6-abd9-2025656d3c63'}
    eligible = sorted(uid for uid, a in annotations.items() if (video_ids is not None or uid not in excluded)
                      and type(a.get('truth')) is int and a['truth'] in range(5)
                      and uid in captions and (data/'videos'/f'{uid}.mp4').is_file())
    if not 1 <= count <= len(eligible) or not 1 <= shards <= count:
        raise ValueError('Invalid sample/shard count for the available labeled videos.')
    if (feature_batch_size < 1 or embedding_batch_size < 1 or streams_per_gpu < 1
            or shards % streams_per_gpu):
        raise ValueError('Positive batches/streams required; shards must divide evenly among GPU jobs.')
    selected = list(video_ids) if video_ids is not None else random.Random(seed).sample(eligible, count)
    if len(selected) != count or len(set(selected)) != count or not set(selected) <= set(eligible):
        raise ValueError('Explicit cohort must contain the requested number of distinct available questions')
    # Inference files contain questions/options only. Labels are isolated for offline scoring.
    labels = [{'question_id': uid, 'video_id': uid, 'selected_option': str(annotations[uid]['truth'])}
              for uid in selected]
    write(output/'evaluation_only.json', {'questions': labels})
    import yaml
    cfg = {'schema_version': 2, 'seed': seed, 'method': 'B2',
           'construction': {'strategy': 'goal_lazy'}, 'cache': {'scope': 'query'},
           'initialization': {'caption_source': 'provided'},
           'models': {'batching': {'visual_encoder': feature_batch_size, 'text_embedding': embedding_batch_size,
                                   'text_embedding_max_tokens': 8192}}}
    (output/'config.yaml').write_text(yaml.safe_dump(cfg, sort_keys=False))
    caption_hash = hashlib.sha256((data/'lavila_subset.json').read_bytes()).hexdigest()
    for uid in selected:
        write(output/'captions'/f'{uid}.json', {'producer': {'name': 'LaViLa',
              'original_file': str(data/'lavila_subset.json'), 'original_file_sha256': caption_hash},
              'spans': [{'start': i, 'end': i+1, 'text': text} for i, text in enumerate(captions[uid])]})
    for index in range(shards):
        ids = selected[index::shards]
        directory = output/'shards'/str(index)
        write(directory/'manifest.json', [{'video_id': uid, 'uri': str(data/'videos'/f'{uid}.mp4'),
              'caption_uri': str(output/'captions'/f'{uid}.json')} for uid in ids])
        clean = {uid: {k: annotations[uid][k] for k in ['question', *[f'option {i}' for i in range(5)]]} for uid in ids}
        write(directory/'videotree_input.json', clean)
        questions = [{'question_id': uid, 'video_id': uid, 'question': clean[uid]['question'],
                      'options': [clean[uid][f'option {i}'] for i in range(5)]} for uid in ids]
        (directory/'questions.jsonl').write_text(''.join(json.dumps(q, ensure_ascii=False)+'\n' for q in questions))
    snapshot = output/'source'
    files = ['script_qa.py', 'demo.py', 'adaptive_breath_expansion.py', 'depth_expansion.py',
             'main_qa.py', 'dataset.py', 'model.py', 'util.py', 'prompts.py', 'eval.py', 'requirements.txt',
             'script/run_pilot_comparison.py', 'script/pilot_comparison.pbs',
             'script/benchmark_feature_batches.py',
             'script/repair_smoke.pbs',
             'script/check_run_integrity.py',
             'script/summarize_repair_run.py',
             'script/run_script_scaling.py', 'script/script_scaling.pbs',
             'script/recheck_answers.py', 'script/answer_recheck.pbs', 'script/analyze_query_performance.py',
             'data_extraction/extract_images.py', 'data_extraction/extract_features.py']
    files += [str(p.relative_to(ROOT)) for d in ['script_core', 'kmeans_pytorch'] for p in (ROOT/d).glob('*.py')]
    hashes = {}
    for name in files:
        source, target = ROOT/name, snapshot/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    for name in ['checkpoints', 'data']:
        (snapshot/name).symlink_to((ROOT/name).resolve(), target_is_directory=True)
    write(output/'source_hashes.json', hashes)
    spec = {'created_at': now(), 'count': count, 'shards': shards, 'seed': seed, 'video_ids': selected,
            'dataset': 'EgoSchema labeled 500-question subset', 'data_root': str(data),
            'excluded_debug_video_ids': sorted(excluded), 'model': str((ROOT/'checkpoints/Qwen/Qwen2.5-VL-7B-Instruct').resolve()),
            'feature_model': str((ROOT/'checkpoints/BAAI/EVA-CLIP-8B').resolve()),
            'script_method': 'B2', 'construction': 'goal_lazy', 'cache_scope': 'query',
            'script_caption_source': 'provided', 'shared_caption_sha256': caption_hash,
            'script_online_cap_seconds': 600, 'shared_generation_max_new_tokens': 1024,
            'feature_batch_size': feature_batch_size, 'embedding_batch_size': embedding_batch_size,
            'streams_per_gpu': streams_per_gpu, 'gpu_jobs': shards//streams_per_gpu,
            'comparison': 'preliminary system comparison with the same questions, LaViLa captions and Qwen weights',
            'input_difference': 'Both use the same released LaViLa descriptions. SCRIPT may additionally observe raw video online; VideoTree QA reads selected captions.',
            'cost_limitations': ['Released LaViLa caption generation cost is unavailable and excluded from both measured runtimes.',
                                 'Existing VideoTree frame/feature caches are reported; their historical costs are unavailable.',
                                 'VideoTree has its native expansion limits; this pilot does not enforce a matched end-to-end time cap.',
                                 'Per-question processing seconds include contention from the recorded streams_per_gpu setting.',
                                 'EVA preprocessing/precision follows each existing implementation; this is not a mechanism-isolation experiment.']}
    write(output/'pilot_spec.json', spec)
    if video_ids is not None:
        spec['cohort_source'] = 'Explicit frozen cohort order'
        spec['included_debug_video_ids'] = sorted(set(selected) & excluded)
        write(output/'pilot_spec.json', spec)
    if reuse_from:
        previous = Path(reuse_from).resolve()
        old_spec = read(previous/'pilot_spec.json')
        if (old_spec['video_ids'] != spec['video_ids'] or old_spec['shards'] != shards
                or old_spec['shared_caption_sha256'] != spec['shared_caption_sha256']
                or read(previous/'evaluation_only.json') != read(output/'evaluation_only.json')):
            raise ValueError('Reuse requires identical frozen questions, order, captions and labels')
        input_hashes = {}
        for shard in range(shards):
            old_dir, new_dir = previous/'shards'/str(shard), output/'shards'/str(shard)
            if (old_dir/'questions.jsonl').read_bytes() != (new_dir/'questions.jsonl').read_bytes():
                raise ValueError('Reuse question file differs from original')
            index = read(old_dir/'script_base'/'videos.json')
            write(new_dir/'script_base'/'videos.json', index)
            for uid, path in index.items():
                base_hash = Path(path)/'base_hash.json'
                input_hashes[str(base_hash)] = hashlib.sha256(base_hash.read_bytes()).hexdigest()
            (new_dir/'videotree_runs').mkdir()
            for row in read(old_dir/'manifest.json'):
                uid = row['video_id']
                original = old_dir/'videotree_runs'/uid/'summary.json'
                if not original.exists():
                    raise ValueError('Reuse requires a complete original VideoTree baseline')
                input_hashes[str(original)] = hashlib.sha256(original.read_bytes()).hexdigest()
                (new_dir/'videotree_runs'/uid).symlink_to(original.parent, target_is_directory=True)
        spec.update(reuse_base_from=str(previous), reuse_videotree_from=str(previous))
        spec['cost_limitations'].append('Original question-independent M0 and frozen VideoTree predictions are reused; historical initialization and baseline costs are retained, not remeasured.')
        write(output/'reuse_hashes.json', input_hashes)
        write(output/'pilot_spec.json', spec)
    score(output)
    return spec


def verify_snapshot(output):
    for name, expected in read(output/'source_hashes.json').items():
        if hashlib.sha256((output/'source'/name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Frozen pilot source changed: {name}')
    if (output/'reuse_hashes.json').exists():
        for name, expected in read(output/'reuse_hashes.json').items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
                raise ValueError(f'Reused experiment input changed: {name}')


def command(directory, phase, arguments):
    started = time.monotonic()
    state = {'phase': phase, 'started_at': now(), 'command': [sys.executable, *map(str, arguments)]}
    write(directory/'status.json', state)
    print('PILOT', phase, flush=True)
    result = subprocess.run(state['command'], cwd=ROOT, check=False)
    state.update(ended_at=now(), wall_seconds=time.monotonic()-started, returncode=result.returncode)
    write(directory/f'{phase}.json', state)
    return result.returncode


def build_script(output, directory, spec):
    from script_core.backend import preflight
    from script_core.budget import ModelWorker
    from script_core.build import build_video
    from script_core.config import load_config
    from script_qa import safe_directory_id
    started, cpu = time.monotonic(), time.process_time()
    cfg, artifacts = preflight(load_config(output/'config.yaml'))
    startup = {'seconds': time.monotonic()-started, 'cpu_seconds': time.process_time()-cpu}
    base = directory/'script_base'
    write(base/'artifacts.json', artifacts)
    write(base/'run_config.json', cfg)
    backend, index, errors = ModelWorker(cfg), {}, {}
    try:
        for row in read(directory/'manifest.json'):
            uid = row['video_id']
            write(directory/'status.json', {'phase': 'script_build', 'video_id': uid, 'updated_at': now()})
            try:
                _, _ = build_video(row, cfg, backend, base/safe_directory_id(uid), startup)
                startup = None
                index[uid] = str((base/safe_directory_id(uid)).resolve())
            except Exception as error:
                errors[uid] = {'code': 'build_failed', 'message': str(error)}
                print(f'PILOT BUILD FAILED {uid}: {error}', flush=True)
            write(base/'videos.json', index)
            write(directory/'script_build_errors.json', errors)
    finally:
        backend.close(graceful=True)
    write(directory/'script_build.json', {'phase': 'script_build', 'wall_seconds': time.monotonic()-started,
                                         'built': len(index), 'errors': errors, 'ended_at': now()})
    return index


def run(output, shard):
    output = Path(output).resolve()
    verify_snapshot(output)
    spec = read(output/'pilot_spec.json')
    directory = output/'shards'/str(shard)
    if not 0 <= shard < spec['shards']:
        raise ValueError('Shard outside the prepared sample.')
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('Each shard must run in its own single-GPU PBS allocation.')
    write(directory/'allocation.json', {'job_id': os.environ.get('PBS_JOBID'), 'node': socket.gethostname(),
          'gpu': torch.cuda.get_device_name(0), 'gpu_memory_bytes': torch.cuda.get_device_properties(0).total_memory,
          'started_at': now(), 'policy': 'Batch job exits and releases its GPU after the shard is scored.'})
    started = time.monotonic()
    with (directory/'run.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            index = (read(directory/'script_base'/'videos.json') if spec.get('reuse_base_from')
                     else build_script(output, directory, spec))
            questions = [json.loads(s) for s in (directory/'questions.jsonl').read_text().splitlines()]
            accepted = [q for q in questions if q['video_id'] in index]
            query_path = directory/'built_questions.jsonl'
            query_path.write_text(''.join(json.dumps(q, ensure_ascii=False)+'\n' for q in accepted))
            query_rc = 0
            if accepted:
                query_rc = command(directory, 'script_query', [ROOT/'script_qa.py', 'query', '--base', directory/'script_base',
                        '--questions', query_path, '--config', output/'config.yaml', '--out', directory/'script_runs', '--resume'])
            score(output)
            integrity_rc = command(directory, 'integrity', [ROOT/'script/check_run_integrity.py',
                '--runs', directory/'script_runs', '--out', directory/'integrity_report.json'])
            if spec.get('reuse_videotree_from'):
                write(directory/'status.json', {'phase': 'finished', 'ended_at': now(),
                    'wall_seconds': time.monotonic()-started, 'query_returncode': query_rc,
                    'integrity_returncode': integrity_rc})
                return query_rc or integrity_rc
            ids = [q['video_id'] for q in questions]
            frame_root, feature_root = ROOT/'data/EgoSchema/egoschema_frames', ROOT/'data/EgoSchema/egoschema_features'
            write(directory/'videotree_existing_caches.json', {uid: {'frames': (frame_root/uid/'frames.json').exists(),
                          'features': (feature_root/f'{uid}.pt').exists()} for uid in ids})
            frames_ok = command(directory, 'videotree_frames', [ROOT/'data_extraction/extract_images.py',
                          '--data-root', spec['data_root'], '--video-ids', *ids]) == 0
            features_ok = frames_ok and command(directory, 'videotree_features', [ROOT/'data_extraction/extract_features.py',
                          '--model', spec['feature_model'], '--batch-size', spec['feature_batch_size'], '--video-ids', *ids]) == 0
            from demo import run_pipeline
            clean = read(directory/'videotree_input.json')
            for uid in ids:
                out = directory/'videotree_runs'/uid
                if (out/'summary.json').exists() or (out/'failure.json').exists():
                    continue
                stage_start = time.monotonic()
                write(directory/'status.json', {'phase': 'videotree_query', 'video_id': uid, 'updated_at': now()})
                try:
                    if not features_ok:
                        raise RuntimeError('VideoTree frame/feature preprocessing failed; inspect the shard log.')
                    args = SimpleNamespace(skip_preprocess=True, model=spec['model'], feature_model=spec['feature_model'],
                                           feature_batch_size=spec['feature_batch_size'], max_new_tokens=1024)
                    run_pipeline(args, Path(spec['data_root']), out, {uid: clean[uid]},
                                 Path(spec['data_root'])/'lavila_subset.json')
                except Exception as error:
                    write(out/'failure.json', {'error': str(error), 'ended_at': now(), 'wall_seconds': time.monotonic()-stage_start})
                    print(f'PILOT VIDEOTREE FAILED {uid}: {error}', flush=True)
                score(output)
            write(directory/'status.json', {'phase': 'finished', 'ended_at': now(), 'wall_seconds': time.monotonic()-started})
            score(output)
        finally:
            allocation = read(directory/'allocation.json')
            allocation.update(exiting_at=now(), process_wall_seconds=time.monotonic()-started)
            write(directory/'allocation.json', allocation)


def run_job(output, job_index):
    """Two isolated model workers can overlap CPU/I/O gaps on the same allocated GPU."""
    output = Path(output).resolve()
    verify_snapshot(output)
    spec = read(output/'pilot_spec.json')
    jobs = spec.get('gpu_jobs', spec['shards'])
    if not 0 <= job_index < jobs:
        raise ValueError('GPU job index outside the prepared experiment')
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('run-job requires one allocated GPU')
    if spec.get('streams_per_gpu', 1) > 1 and torch.cuda.get_device_properties(0).total_memory < 120*1024**3:
        raise RuntimeError('Concurrent pilot workers require at least 120 GiB GPU memory')
    shards = list(range(job_index, spec['shards'], jobs))
    state = {'job_id': os.environ.get('PBS_JOBID'), 'job_index': job_index, 'node': socket.gethostname(),
             'gpu': torch.cuda.get_device_name(0), 'shards': shards, 'started_at': now(), 'status': 'running'}
    write(output/'jobs'/f'job-{job_index}-status.json', state)
    processes = []
    with ExitStack() as stack:
        for shard in shards:
            log = stack.enter_context((output/'shards'/str(shard)/'worker.log').open('a', buffering=1))
            processes.append((shard, subprocess.Popen([sys.executable, str(ROOT/'script/run_pilot_comparison.py'),
                'run', '--out', str(output), '--shard', str(shard)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)))
        for shard, process in processes:
            returncode = process.wait()
            state.setdefault('returncodes', {})[str(shard)] = returncode
            write(output/'jobs'/f'job-{job_index}-status.json', state)
    state.update(ended_at=now(), status='finished')
    write(output/'jobs'/f'job-{job_index}-status.json', state)
    score(output)
    if spec.get('reuse_base_from') and (output/'experiment_plan.json').exists():
        from summarize_repair_run import summarize
        summarize(output)
    return int(any(state['returncodes'].values()))


def score(output):
    output = Path(output).resolve()
    from script_qa import safe_directory_id
    from script_core.evaluation import paired_cluster_bootstrap
    spec = read(output/'pilot_spec.json') if (output/'pilot_spec.json').exists() else None
    if spec is None:
        return None
    labels = {q['question_id']: str(q['selected_option']) for q in read(output/'evaluation_only.json')['questions']}
    with (output/'score.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = []
        for shard in range(spec['shards']):
            directory = output/'shards'/str(shard)
            errors = read(directory/'script_build_errors.json') if (directory/'script_build_errors.json').exists() else {}
            for q in [json.loads(s) for s in (directory/'questions.jsonl').read_text().splitlines()]:
                uid = q['question_id']
                row = {'question_id': uid, 'truth': labels[uid], 'SCRIPT': None, 'VideoTree': None}
                path = directory/'script_runs'/safe_directory_id(uid)/'result.json'
                failure = directory/'script_runs'/'query_failures'/(safe_directory_id(uid)+'.json')
                if failure.exists():
                    r = read(failure)
                    row['SCRIPT'] = {'prediction': None, 'correct': False, 'error': r['error'],
                                     'stop_reason': 'query_failed', 'cost': r['cost_summary'],
                                     'path': str(failure.relative_to(output))}
                elif path.exists():
                    r = read(path)
                    selected = (r.get('answer') or {}).get('selected_option')
                    row['SCRIPT'] = {'prediction': selected, 'correct': selected == labels[uid], 'error': r['error'],
                                     'stop_reason': (r.get('stop_decision') or {}).get('reason'), 'cost': r['cost_summary'],
                                     'diagnostics': r.get('diagnostics', {}),
                                     'wall_seconds': r.get('wall_seconds'), 'path': str(path.relative_to(output))}
                elif uid in errors:
                    row['SCRIPT'] = {'prediction': None, 'correct': False, 'error': errors[uid], 'stop_reason': 'build_failed'}
                vt = directory/'videotree_runs'/uid
                if (vt/'summary.json').exists():
                    r = read(vt/'summary.json')
                    example = r['examples'][0]
                    selected = str(ord(example['answer'])-ord('A'))
                    row['VideoTree'] = {'prediction': selected, 'correct': selected == labels[uid], 'error': None,
                                       'wall_seconds': r['seconds'], 'selected_frames': example['selected_frames'],
                                       'path': str((vt/'summary.json').relative_to(output))}
                elif (vt/'failure.json').exists():
                    row['VideoTree'] = {'prediction': None, 'correct': False, 'error': read(vt/'failure.json')}
                rows.append(row)
        groups = {}
        for method in ['SCRIPT', 'VideoTree']:
            completed = [r[method] for r in rows if r[method] is not None]
            correct = sum(r['correct'] for r in completed)
            groups[method] = {'planned_questions': len(rows), 'completed_questions': len(completed),
                              'correct': correct, 'accuracy_on_completed': correct/len(completed) if completed else None,
                              'final_accuracy': correct/len(rows) if len(completed) == len(rows) else None,
                              'errors': sum(bool(r.get('error')) for r in completed)}
            import statistics
            wall = [r['wall_seconds'] for r in completed if isinstance(r.get('wall_seconds'), (int, float))]
            groups[method].update(mean_wall_seconds=statistics.mean(wall) if wall else None,
                                  median_wall_seconds=statistics.median(wall) if wall else None)
            if method == 'SCRIPT':
                groups[method].update({field: sum(r.get('diagnostics', {}).get(field, 0) for r in completed)
                    for field in ('internal_tool_errors', 'hard_timeouts', 'soft_deadline_stops', 'model_preparations')})
                groups[method]['budget_violations'] = sum(bool(r.get('cost', {}).get('budget_violation')) for r in completed)
        paired = [r for r in rows if r['SCRIPT'] is not None and r['VideoTree'] is not None]
        bootstrap_rows = [{'question_id': r['question_id'], 'cluster_id': r['question_id'],
                           'method': method, 'correct': r[method]['correct']} for r in paired for method in groups]
        result = {'updated_at': now(), 'complete': len(paired) == len(rows), 'paired_questions': len(paired),
                  'groups': groups, 'paired_outcomes': {k: sum(condition(r) for r in paired) for k, condition in {
                      'both_correct': lambda r: r['SCRIPT']['correct'] and r['VideoTree']['correct'],
                      'SCRIPT_only_correct': lambda r: r['SCRIPT']['correct'] and not r['VideoTree']['correct'],
                      'VideoTree_only_correct': lambda r: not r['SCRIPT']['correct'] and r['VideoTree']['correct'],
                      'both_wrong': lambda r: not r['SCRIPT']['correct'] and not r['VideoTree']['correct']}.items()},
                  'paired_accuracy_bootstrap': paired_cluster_bootstrap(bootstrap_rows, 'SCRIPT', 'VideoTree'),
                  'rows': rows, 'comparison': spec['comparison'], 'input_difference': spec['input_difference'],
                  'cost_limitations': spec['cost_limitations'],
                  'notes': ['Errors count as incorrect; pending results are never silently removed from final accuracy.',
                            'Partial completed-sample accuracy is progress only, not the final comparison.',
                            'This is a preliminary native-system comparison, not original VideoTree paper reproduction.']}
        write(output/'comparison.json', result)
        lines = ['question_id,truth,script_prediction,videotree_prediction,script_correct,videotree_correct']
        for r in rows:
            values = [r['question_id'], r['truth']]
            values += [str((r[m] or {}).get('prediction') or '') for m in ['SCRIPT', 'VideoTree']]
            values += [str(r[m]['correct']) if r[m] else '' for m in ['SCRIPT', 'VideoTree']]
            lines.append(','.join(values))
        (output/'comparison.csv').write_text('\n'.join(lines)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run', 'run-job', 'score'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--shards', type=int, default=2)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--job-index', type=int, default=0)
    parser.add_argument('--feature-batch-size', type=int, default=16)
    parser.add_argument('--embedding-batch-size', type=int, default=8)
    parser.add_argument('--streams-per-gpu', type=int, default=1)
    parser.add_argument('--reuse-from', help='Reuse identical frozen M0 and completed VideoTree predictions')
    args = parser.parse_args()
    if args.action == 'prepare':
        result = prepare(args.out, args.count, args.shards, args.seed,
                         feature_batch_size=args.feature_batch_size, embedding_batch_size=args.embedding_batch_size,
                         streams_per_gpu=args.streams_per_gpu, reuse_from=args.reuse_from)
        print('PREPARED', result['count'], 'questions,', result['shards'], 'shards', args.out)
    elif args.action == 'run':
        run(args.out, args.shard)
    elif args.action == 'run-job':
        return run_job(args.out, args.job_index)
    else:
        result = score(args.out)
        print(json.dumps({k: result[k] for k in ['complete', 'paired_questions', 'groups']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.exit(main())
