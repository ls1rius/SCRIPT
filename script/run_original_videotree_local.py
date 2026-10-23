"""Run the sibling VideoTree checkout on all 500 labeled EgoSchema examples.

Freeze upstream code, inject a persistent local Qwen backend, and report the
exact current 100/300 cohorts alongside all 500. Upstream prompts and tree
algorithms (including their original indexing behavior) are retained.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(ZoneInfo('Asia/Singapore')).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def question(uid, row):
    return dict(question_id=uid, video_id=uid, question=row['question'],
                options=[row[f'option {i}'] for i in range(5)])


def aligned_cohorts(annotations, reference):
    cohort = read(reference / 'cohort.json')
    if len(cohort) != 300 or len(set(cohort)) != 300 or not set(cohort) <= set(annotations):
        raise ValueError('Reference must contain 300 distinct labeled examples')
    if len(annotations) != 500:
        raise ValueError('Expected all 500 publicly labeled examples')
    all_ids = cohort + sorted(set(annotations) - set(cohort))
    return {'100': cohort[:100], '300': cohort, '500': all_ids}


def summarize(ids, results, truth):
    rows = [results[u] for u in ids if u in results]
    correct = sum(r.get('error') is None and r.get('prediction') == truth[r['video_id']] for r in rows)
    errors = sum(r.get('error') is not None for r in rows)
    return dict(planned=len(ids), completed=len(rows), pending=len(ids)-len(rows),
                correct=correct, errors=errors, complete=len(rows) == len(ids),
                accuracy=correct/len(ids) if len(rows) == len(ids) else None,
                completed_accuracy=correct/len(rows) if rows else None,
                errors_count_wrong=True,
                mean_inference_seconds=sum(r.get('inference_seconds', 0) for r in rows)/len(rows) if rows else None,
                qa_parser_fallbacks=sum(bool(r.get('qa_parser_fallback')) for r in rows))


def comparison(ids, results, reference, arm, truth):
    other = {}
    for uid in ids:
        path = reference / 'arms' / arm / 'runs' / uid / 'result.json'
        if path.exists():
            other[uid] = read(path)
    shared = [u for u in ids if u in other and u in results]
    def ours(u):
        return not results[u].get('error') and results[u].get('prediction') == truth[u]
    def theirs(u):
        return not other[u].get('error') and other[u].get('answer') == truth[u]
    return dict(planned=len(ids), reference_completed=len(other), paired_completed=len(shared),
                videotree_correct=sum(bool(ours(u)) for u in shared),
                reference_correct=sum(bool(theirs(u)) for u in shared),
                both_correct=sum(bool(ours(u) and theirs(u)) for u in shared),
                videotree_only_correct=sum(bool(ours(u) and not theirs(u)) for u in shared),
                reference_only_correct=sum(bool(not ours(u) and theirs(u)) for u in shared),
                both_wrong=sum(bool(not ours(u) and not theirs(u)) for u in shared),
                rows=[dict(video_id=u, truth=truth[u], videotree_prediction=results[u].get('prediction'),
                           reference_prediction=other[u].get('answer'), videotree_error=results[u].get('error'),
                           reference_error=other[u].get('error')) for u in shared],
                scope='Identical questions/options/source captions; original VideoTree prompts/indexing/tree differ; GPU models differ; timing is not a hardware-controlled comparison')


def report(out):
    plan = read(out/'experiment.json')
    cohorts, truth = read(out/'cohorts.json'), read(out/'evaluation_only.json')
    results = {p.parent.name: read(p) for p in (out/'runs').glob('*/result.json')}
    workers = {p.stem: read(p) for p in (out/'workers').glob('*.json')}
    reports = {n: summarize(ids, results, truth) for n, ids in cohorts.items()}
    for n, data in reports.items():
        write(out/f'reports/videotree_{n}.json', dict(updated_at=now(), cohort_file='cohorts.json',
              cohort_key=n, **data))
    for n in ('100', '300'):
        comparisons = {arm: comparison(cohorts[n], results, Path(plan['reference']), arm, truth)
                       for arm in plan['reference_arms']}
        write(out/f'reports/comparison_{n}.json', dict(updated_at=now(), arms=comparisons))
    write(out/'progress.json', dict(updated_at=now(), reports=reports, workers=workers))


def import_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(out):
    for name, expected in read(out/'frozen_hashes.json').items():
        if sha(out/name) != expected:
            raise ValueError('Frozen input/source changed: '+name)


def status(out, worker, **fields):
    write(out/'workers'/f'{worker}.json', dict(pid=os.getpid(), updated_at=now(), **fields))


def prepare(args):
    out, reference, original = Path(args.out).resolve(), Path(args.reference).resolve(), Path(args.videotree).resolve()
    if out.exists():
        raise ValueError('Use a new output directory; existing runs retain their logs and spent work')
    indices = args.gpus.split(',')
    if len(set(indices+[args.asset_gpu])) != len(indices)+1:
        raise ValueError('Assign distinct inference and asset GPUs')
    inventory = subprocess.check_output(['nvidia-smi', '--query-gpu=index,uuid,memory.used,utilization.gpu',
                                         '--format=csv,noheader,nounits'], text=True)
    available = {p[0].strip(): dict(uuid=p[1].strip(), memory=int(p[2]), utilization=int(p[3]))
                 for line in inventory.splitlines() if len(p := line.split(',')) == 4}
    for index in indices+[args.asset_gpu]:
        if index not in available or available[index]['memory'] > 500 or available[index]['utilization'] > 5:
            raise ValueError('GPU is occupied or unavailable: '+index)
    data = (ROOT/'data/EgoSchema/egoschema').resolve()
    annotations, captions = read(data/'subset_anno.json'), read(data/'lavila_subset.json')
    cohorts = aligned_cohorts(annotations, reference)
    expected_questions = [json.loads(line) for line in (reference/'questions.jsonl').read_text().splitlines()]
    questions = {u: question(u, annotations[u]) for u in cohorts['500']}
    if expected_questions != [questions[u] for u in cohorts['300']]:
        raise ValueError('Current question/options/order differ from reference')
    for uid in cohorts['500']:
        if annotations[uid].get('truth') not in range(5) or len(captions[uid]) != 180 or not (data/'videos'/f'{uid}.mp4').is_file():
            raise ValueError('Missing labeled video/captions: '+uid)
    # Match the actual frozen reference inputs, not a regenerated sample.
    for uid in cohorts['300']:
        packet = read(reference/'inputs'/f'{uid}.json')
        if packet['question'] != questions[uid] or [r['text'] for r in packet['captions']] != captions[uid]:
            raise ValueError('Reference input changed: '+uid)
    config = read(reference/'config.json')
    if sha(data/'lavila_subset.json') != read(reference/'experiment.json')['cohort_provenance']['caption_sha256']:
        raise ValueError('Captions differ from reference provenance')
    out.mkdir(parents=True)
    for name in ('runs', 'ready', 'workers', 'logs', 'reports', 'upstream', 'runtime'):
        (out/name).mkdir()
    for path in original.rglob('*.py'):
        if '.git' in path.parts:
            continue
        target = out/'upstream'/path.relative_to(original)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # The upstream file imports a missing optional SoftDTW module even for cosine
    # clustering, and squeeze() creates a scalar singleton index on modern torch.
    path = out/'upstream/kmeans_pytorch/__init__.py'
    text = path.read_text()
    text = text.replace('from .soft_dtw_cuda import SoftDTW\n', '')
    text = text.replace("elif distance == 'soft_dtw':\n", "elif distance == 'soft_dtw':\n        from .soft_dtw_cuda import SoftDTW\n")
    text = text.replace('torch.nonzero(choice_cluster == index).squeeze()', 'torch.nonzero(choice_cluster == index).flatten()')
    path.write_text(text)
    for name in ('model.py', 'data_extraction/extract_images.py'):
        target = out/'runtime'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/name, target)
    shutil.copy2(__file__, out/'runtime/runner.py')
    write(out/'cohorts.json', cohorts)
    write(out/'questions.json', questions)
    write(out/'captions.json', captions)
    write(out/'durations.json', read(data/'duration.json'))
    write(out/'evaluation_only.json', {u: annotations[u]['truth'] for u in cohorts['500']})
    write(out/'alignment.json', dict(reference=str(reference), current_300_order_exact=True,
          first_100_exact=True, questions_options_exact=True, captions_exact=True,
          reference_cohort_sha256=sha(reference/'cohort.json'),
          question_hashes={u: digest(q) for u, q in questions.items()},
          caption_hashes={u: digest(captions[u]) for u in questions},
          historical_missing_cohort_identity_claimed=False))
    allocation = {f'inference_{i}': dict(index=g, uuid=available[g]['uuid']) for i, g in enumerate(indices)}
    allocation['assets'] = dict(index=args.asset_gpu, uuid=available[args.asset_gpu]['uuid'])
    upstream_hashes = {str(p.relative_to(original)): sha(p) for p in original.rglob('*.py') if '.git' not in p.parts}
    write(out/'experiment.json', dict(created_at=now(), planned=500, reference=str(reference),
          reference_arms=list(read(reference/'experiment.json')['arms']),
          source=str(original), upstream_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=original, text=True).strip(),
          upstream_original_hashes=upstream_hashes, gpus=allocation, inference_workers=len(indices),
          data_root=str(data), frame_root=str((ROOT/'data/EgoSchema/egoschema_frames').resolve()),
          feature_root=str((ROOT/'data/EgoSchema/egoschema_features').resolve()),
          feature_model=config['models']['visual_encoder'], model=config['models']['vlm'],
          max_new_tokens=config['models']['generation']['max_new_tokens'], seed=0,
          feature_batch_size=16, frame_workers=4, per_question_timeout_seconds=1800,
          baseline='Sibling original VideoTree width launch + depth functions (4,4) + QA launch',
          adaptations=['Persistent local Qwen2.5-VL-7B text backend instead of upstream GPT-4',
                       'Explicit local data/output paths and feature placement on the assigned GPU',
                       'Lazy optional SoftDTW import; flatten singleton kmeans indices for modern torch',
                       'Fixed per-question seed, raw call traces, isolated per-question failures'],
          original_prompt_indexing_retained=True, original_relevance_cluster_mapping_retained=True,
          original_qa_parser_fallback_retained_and_flagged=True,
          few_shot_examples=None, errors_count_wrong=True, cached_features_reused=True,
          inference_cache_reused=False, timing_scope='Inference excludes shared offline frame/feature preparation and model startup; hardware differs from reference'))
    frozen = [p for folder in ('upstream', 'runtime') for p in (out/folder).rglob('*.py')]
    frozen += [out/n for n in ('experiment.json', 'cohorts.json', 'questions.json', 'captions.json', 'durations.json', 'alignment.json', 'evaluation_only.json')]
    write(out/'frozen_hashes.json', {str(p.relative_to(out)): sha(p) for p in frozen})
    report(out)
    return out


def assets(out):
    import cv2
    import torch
    from PIL import Image
    from transformers import AutoModel, CLIPImageProcessor
    extract = import_file('local_extract_images', out/'runtime/data_extraction/extract_images.py').extract_video
    plan, ids, captions = read(out/'experiment.json'), read(out/'cohorts.json')['500'], read(out/'captions.json')
    cv2.setNumThreads(1)
    processor = CLIPImageProcessor(size={'shortest_edge': 224}, crop_size={'height': 224, 'width': 224})
    model = None
    errors = 0
    with ThreadPoolExecutor(max_workers=plan['frame_workers']) as pool:
        futures = {u: pool.submit(extract, Path(plan['data_root'])/'videos'/f'{u}.mp4',
                                  Path(plan['frame_root'])/u, len(captions[u])) for u in ids}
        for n, uid in enumerate(ids, 1):
            status(out, 'assets', state='preparing', completed=n-1, planned=len(ids), video_id=uid, errors=errors)
            try:
                futures[uid].result()
                target = Path(plan['feature_root'])/f'{uid}.pt'
                valid = False
                if target.exists() and target.with_suffix('.json').exists():
                    meta = read(target.with_suffix('.json'))
                    features = torch.load(target, map_location='cpu', weights_only=True)
                    valid = (meta.get('model') == plan['feature_model'] and meta.get('fps') == 1
                             and list(features.shape) == [len(captions[uid]), 1280]
                             and meta.get('frame_indices') == list(range(len(captions[uid])))
                             and torch.isfinite(features).all().item())
                reused = valid
                if not valid:
                    if model is None:
                        print('LOADING_EVA', flush=True)
                        model = AutoModel.from_pretrained(plan['feature_model'], torch_dtype=torch.float16,
                            trust_remote_code=True, local_files_only=True, low_cpu_mem_usage=True).to('cuda').eval()
                    batches = []
                    batch_size = plan['feature_batch_size']
                    for start in range(0, len(captions[uid]), batch_size):
                        images = []
                        for i in range(start, min(start+batch_size, len(captions[uid]))):
                            with Image.open(Path(plan['frame_root'])/uid/f'{i}.jpg') as im:
                                images.append(im.convert('RGB'))
                        pixels = processor(images=images, return_tensors='pt').pixel_values.to('cuda', dtype=torch.float16)
                        with torch.inference_mode():
                            batches.append(model.encode_image(pixels).float().cpu())
                    features = torch.cat(batches)
                    if not torch.isfinite(features).all():
                        raise ValueError('Non-finite visual features')
                    temporary = target.with_suffix('.pt.videotree.tmp')
                    torch.save(features, temporary)
                    temporary.replace(target)
                    write(target.with_suffix('.json'), dict(model=plan['feature_model'], shape=list(features.shape),
                          fps=1, dtype=str(features.dtype), frame_indices=list(range(len(features))), extraction_batch_size=batch_size))
                write(out/'ready'/f'{uid}.json', dict(ready=True, feature_uri=str(target), feature_sha256=sha(target), reused=reused))
                print('READY', n, uid, 'reused', reused, flush=True)
            except Exception as exc:
                errors += 1
                write(out/'ready'/f'{uid}.json', dict(ready=False, error=dict(type=type(exc).__name__, message=str(exc))))
                print('ASSET_ERROR', uid, traceback.format_exc(), flush=True)
    status(out, 'assets', state='finished' if not errors else 'finished_with_errors', completed=len(ids), planned=len(ids), errors=errors)
    return int(bool(errors))


class TracedModel:
    def __init__(self, out, plan):
        local = import_file('local_qwen_backend', out/'runtime/model.py')
        self.qa = local.Qwen(plan['model'], 0, 'cuda', plan['max_new_tokens'])
        self.qa.set_post_process_fn(lambda text: text)
        self.calls = 0

    def set_post_process_fn(self, fn):
        self.post_process = fn

    def forward(self, head, prompts):
        self.calls += 1
        path = self.target/'raw_calls'/f'{self.calls:03d}.json'
        value = dict(started_at=now(), stage=self.stage, head=head, prompts=prompts)
        write(path.with_suffix('.pending.json'), value)
        started = time.monotonic()
        try:
            _, info = self.qa.forward(head, prompts)
            value.update(info=info, seconds=time.monotonic()-started)
            # Persist the raw response even if the original parser raises.
            write(path, value)
            prediction = self.post_process(info['response'])
            value['parsed'] = prediction
            write(path, value)
            return prediction, info
        except Exception as exc:
            value.update(seconds=time.monotonic()-started, error=dict(type=type(exc).__name__, message=str(exc)))
            write(path, value)
            raise
        finally:
            path.with_suffix('.pending.json').unlink(missing_ok=True)


def run_question(out, uid, model, width, depth, qa, ready):
    import re
    import torch
    import numpy as np
    plan = read(out/'experiment.json')
    target = out/'runs'/uid
    if target.exists():
        raise RuntimeError('Question already has spent work; use a new run: '+uid)
    target.mkdir()
    model.target, model.calls = target, 0
    question_row = read(out/'questions.json')[uid]
    # Ground truth is only read by reporting, never passed to upstream datasets.
    write(target/'input_anno.json', {uid: dict(question=question_row['question'],
          **{f'option {i}': value for i, value in enumerate(question_row['options'])})})
    np.random.seed(plan['seed'])
    torch.manual_seed(plan['seed'])
    torch.cuda.manual_seed_all(plan['seed'])
    common = ['--anno_path', str(target/'input_anno.json'), '--data_path', str(out/'captions.json'),
              '--duration_path', str(out/'durations.json'), '--frame_feat_path', plan['feature_root'],
              '--output_base_path', str(target), '--save_info', '--save_every', '1',
              '--start_from_scratch', '--disable_eval', '--model', 'injected-local-qwen']
    saved_argv = sys.argv
    started = time.monotonic()
    result = dict(video_id=uid, started_at=now(), input_hash=digest(question_row), feature_sha256=ready['feature_sha256'])
    def deadline(signum, frame):
        raise TimeoutError('VideoTree per-question operational timeout')
    old_handler = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(plan['per_question_timeout_seconds'])
    try:
        width.get_model = lambda args: model
        width.load_frame_features = lambda name, folder: torch.load(Path(folder)/f'{name}.pt', map_location='cuda', weights_only=True)
        model.stage = 'width'
        sys.argv = ['adaptive_breath_expansion.py', *common, '--output_filename', 'relevance_score.json', '--prompt_type', 'cap_score']
        width.launch()
        relevance = read(target/'relevance_score.json')[uid]['pred']
        width_row = read(target/'width_res.json')[0]
        features = torch.load(ready['feature_uri'], map_location='cpu', weights_only=True)
        model.stage = 'depth'
        clusters = depth.hierarchical_clustering_with_external_primary(features, width_row['cluster_ids_x'], relevance, 4, 4)
        indices = depth.find_closest_points_in_temporal_order_subsub(features, clusters, relevance)
        if not indices:
            raise ValueError('Original depth expansion selected no nodes')
        write(target/'depth_expansion_res.json', [dict(name=uid, sorted_values=indices, relevance=relevance)])
        qa.get_model = lambda args: model
        model.stage = 'qa'
        sys.argv = ['main_qa.py', *common, '--output_filename', 'standard_qa.json', '--prompt_type', 'qa_standard',
                    '--tree_node_idx', str(target/'depth_expansion_res.json')]
        qa.launch()
        answer = read(target/'standard_qa.json')[uid]
        prediction = answer['pred']
        if prediction not in range(5):
            raise ValueError('Invalid VideoTree prediction')
        result.update(prediction=prediction, response=answer['response'], selected_indices=indices,
                      qa_parser_fallback=re.search(r'prediction: ([A-E])', answer['response'], re.IGNORECASE) is None,
                      error=None)
    except Exception as exc:
        result.update(prediction=None, error=dict(type=type(exc).__name__, message=str(exc), stage=model.stage))
        (target/'error_traceback.txt').write_text(traceback.format_exc())
        if isinstance(exc, torch.cuda.OutOfMemoryError):
            torch.cuda.empty_cache()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        sys.argv = saved_argv
    result.update(finished_at=now(), inference_seconds=time.monotonic()-started, model_calls=model.calls)
    write(target/'result.json', result)
    return result


def inference(out, shard):
    plan = read(out/'experiment.json')
    sys.path.insert(0, str(out/'upstream'))
    import adaptive_breath_expansion as width
    import depth_expansion as depth
    import main_qa as qa
    ids = read(out/'cohorts.json')['500'][shard::plan['inference_workers']]
    name = f'inference_{shard}'
    status(out, name, state='loading_model', completed=0, planned=len(ids), errors=0)
    model = TracedModel(out, plan)
    errors = 0
    for n, uid in enumerate(ids, 1):
        status(out, name, state='waiting_input', completed=n-1, planned=len(ids), video_id=uid, errors=errors)
        path = out/'ready'/f'{uid}.json'
        while not path.exists():
            state = read(out/'workers/assets.json').get('state') if (out/'workers/assets.json').exists() else None
            if state in ('failed', 'finished', 'finished_with_errors'):
                raise RuntimeError('Asset worker ended without publishing input: '+uid)
            time.sleep(2)
        ready = read(path)
        if ready['ready']:
            if sha(ready['feature_uri']) != ready['feature_sha256']:
                raise ValueError('Feature cache changed: '+uid)
            status(out, name, state='running', completed=n-1, planned=len(ids), video_id=uid, errors=errors)
            result = run_question(out, uid, model, width, depth, qa, ready)
        else:
            result = dict(video_id=uid, prediction=None, error=ready['error'], inference_seconds=0)
            write(out/'runs'/uid/'result.json', result)
        errors += bool(result.get('error'))
        status(out, name, state='running', completed=n, planned=len(ids), video_id=uid, errors=errors)
        print('RESULT', n, '/', len(ids), uid, 'prediction', result['prediction'], 'error', result['error'], flush=True)
    status(out, name, state='finished' if not errors else 'finished_with_errors', completed=len(ids), planned=len(ids), errors=errors)
    return int(bool(errors))


def worker_env(uuid):
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=uuid, CUDA_DEVICE_ORDER='PCI_BUS_ID',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
               OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
    return env


def controller(out):
    plan = read(out/'experiment.json')
    processes = {}
    for name, gpu in plan['gpus'].items():
        command = [sys.executable, '-u', str(out/'runtime/runner.py'), 'assets' if name == 'assets' else 'inference', '--out', str(out)]
        if name != 'assets':
            command += ['--shard', name.split('_')[-1]]
        log = (out/'logs'/f'{name}.log').open('a')
        processes[name] = subprocess.Popen(command, env=worker_env(gpu['uuid']), stdout=log, stderr=subprocess.STDOUT, cwd=out/'upstream')
        log.close()
        print('START', name, 'GPU', gpu['index'], 'PID', processes[name].pid, flush=True)
    write(out/'processes.json', {name: p.pid for name, p in processes.items()})
    exited = {}
    while len(exited) != len(processes):
        for name, process in processes.items():
            code = process.poll()
            if code is not None and name not in exited:
                exited[name] = code
                state = read(out/'workers'/f'{name}.json') if (out/'workers'/f'{name}.json').exists() else {}
                if state.get('state') not in ('finished', 'finished_with_errors'):
                    status(out, name, state='failed', exit_code=code)
                print('EXIT', name, code, flush=True)
        report(out)
        if len(exited) != len(processes):
            time.sleep(15)
    write(out/'completion.json', dict(finished_at=now(), exit_codes=exited,
          complete=read(out/'progress.json')['reports']['500']['complete']))
    return int(any(exited.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['launch', 'controller', 'assets', 'inference', 'report'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--reference', default=str(ROOT/'workdirs/four-arm-300-20261022-r2'))
    parser.add_argument('--videotree', default=str(ROOT.parent/'VideoTree'))
    parser.add_argument('--gpus', default='0,7')
    parser.add_argument('--asset-gpu', default='1')
    parser.add_argument('--shard', type=int, default=0)
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if args.command == 'launch':
        out = prepare(args)
        session = 'codex-videotree-500-'+datetime.now(ZoneInfo('Asia/Singapore')).strftime('%Y%m%d-%H%M%S')
        command = [sys.executable, '-u', str(out/'runtime/runner.py'), 'controller', '--out', str(out)]
        script = '#!/usr/bin/env bash\nset -euo pipefail\nexec '+shlex.join(command)+' >> '+shlex.quote(str(out/'logs/controller.log'))+' 2>&1\n'
        (out/'start.sh').write_text(script)
        subprocess.run(['tmux', 'new-session', '-d', '-s', session, 'bash '+shlex.quote(str(out/'start.sh'))], check=True)
        write(out/'launch.json', dict(session=session, launched_at=now(), command=command))
        print(json.dumps(dict(out=str(out), session=session, gpus=read(out/'experiment.json')['gpus']), indent=2))
        return 0
    verify(out)
    if args.command == 'report':
        report(out)
        return 0
    name = 'assets' if args.command == 'assets' else f'inference_{args.shard}'
    try:
        if args.command == 'assets':
            return assets(out)
        if args.command == 'inference':
            return inference(out, args.shard)
        return controller(out)
    except Exception as exc:
        if args.command != 'controller':
            status(out, name, state='failed', error=dict(type=type(exc).__name__, message=str(exc)))
        raise


if __name__ == '__main__':
    sys.exit(main())
