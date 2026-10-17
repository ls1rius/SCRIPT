"""Frozen caption-first evaluation on existing M0; labels are scored offline."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script_core.config import load_config, base_config, digest


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(output, previous):
    import yaml
    output, previous = Path(output).resolve(), Path(previous).resolve()
    if output.exists():
        raise ValueError('Choose a new experiment directory')
    reference = read(previous / 'comparison.json')
    if not reference['complete']:
        raise ValueError('Reference must be complete')
    questions, bases = [], {}
    reused = {str(previous / 'comparison.json'): sha(previous / 'comparison.json')}
    for i in range(4):
        shard = previous / 'shards' / str(i)
        rows = [json.loads(line) for line in (shard / 'questions.jsonl').read_text().splitlines()]
        questions.extend(rows)
        index = shard / 'script_base' / 'videos.json'
        bases.update(read(index))
        reused[str(index)] = sha(index)
    ids = read(previous / 'cohort.json')
    lookup = {q['question_id']: q for q in questions}
    if len(ids) != 300 or set(lookup) != set(ids) or set(bases) != {q['video_id'] for q in questions}:
        raise ValueError('Expected the exact complete fixed 300-question cohort and bases')
    questions = [lookup[uid] for uid in ids]
    for path in bases.values():
        source_hash = Path(path) / 'base_hash.json'
        reused[str(source_hash)] = sha(source_hash)
    cfg = yaml.safe_load((previous / 'arms' / 'script_guided' / 'config.yaml').read_text())
    cfg.setdefault('query', {}).update(reading_policy='script_guided', caption_first=True,
                                      answer_strategy='direct', answer_order='content_hash', caption_policy='full')
    write(output / 'config.json', cfg)
    if base_config(load_config(output / 'config.json')) != base_config(load_config(previous / 'config.yaml')):
        raise ValueError('Base construction/model settings must remain identical')
    write(output / 'base' / 'videos.json', bases)
    write(output / 'reference_only.json', reference)
    write(output / 'cohort.json', ids)
    # Targeted diagnostics plus a strict-policy control; never an accuracy estimate.
    prefixes = ['45fd8bca', '8345d177', 'b5865ae0', 'd30c1b6c', 'f1d2978d', '263666b2', '3223ece4']
    smoke_ids = [next(uid for uid in ids if uid.startswith(p)) for p in prefixes]
    from script_core.query_policy import strict_question
    smoke_ids.append(next(q['question_id'] for q in questions if strict_question(q['question']) and q['question_id'] not in smoke_ids))
    groups = {'smoke': [lookup[uid] for uid in smoke_ids], **{str(i): questions[i::4] for i in range(4)}}
    for name, rows in groups.items():
        target = output / 'groups' / name / 'questions.jsonl'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(''.join(json.dumps(q, ensure_ascii=False) + '\n' for q in rows))
    files = [ROOT / 'script_qa.py', ROOT / 'requirements.txt', *sorted((ROOT / 'script_core').glob('*.py')),
             ROOT / 'script' / 'run_caption_first.py', ROOT / 'script' / 'caption_first.pbs',
             ROOT / 'script' / 'check_run_integrity.py']
    hashes = {}
    for path in files:
        name = str(path.relative_to(ROOT))
        target = output / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        hashes[name] = sha(target)
    (output / 'source' / 'checkpoints').symlink_to((ROOT / 'checkpoints').resolve(), target_is_directory=True)
    write(output / 'source_hashes.json', hashes)
    write(output / 'reuse_hashes.json', reused)
    frozen = ['config.json', 'base/videos.json', 'cohort.json', 'reference_only.json',
              *[f'groups/{name}/questions.jsonl' for name in groups]]
    write(output / 'input_hashes.json', {name: sha(output / name) for name in frozen})
    spec = {'mode': 'caption_first_reused_m0', 'count': 300, 'smoke_ids': smoke_ids,
            'source': str(previous), 'fresh_m0_videos': 0, 'reused_m0_videos': 300,
            'gpu_jobs_full': 2, 'streams_per_gpu': 2, 'max_simultaneous_gpus': 2,
            'limitations': ['Fixed development cohort; targeted smoke is not an accuracy estimate.',
                           'VideoTree and previous SCRIPT results reused; cold-build costs not remeasured.',
                           'Caption baseline and corrected answer are paired only for eligible caption-first questions.']}
    write(output / 'experiment.json', spec)
    return spec


def verify(output):
    for file, prefix in [('source_hashes.json', output / 'source'), ('input_hashes.json', output)]:
        for name, expected in read(output / file).items():
            if sha(prefix / name) != expected:
                raise ValueError('Frozen input/source changed: ' + name)
    for name, expected in read(output / 'reuse_hashes.json').items():
        if sha(name) != expected:
            raise ValueError('Reused M0/reference changed: ' + name)


def score(output):
    with (output / 'score.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return score_locked(output)


def score_locked(output):
    reference = {r['question_id']: r for r in read(output / 'reference_only.json')['rows']['script_guided']}
    rows = []
    for i in range(4):
        for line in (output / 'groups' / str(i) / 'questions.jsonl').read_text().splitlines():
            uid = json.loads(line)['question_id']
            path = output / 'groups' / str(i) / 'runs' / digest(uid)[:20] / 'result.json'
            try:
                result = read(path) if path.exists() else None
            except json.JSONDecodeError:
                result = None  # Another worker is still writing this result.
            reading = ((result or {}).get('diagnostics') or {}).get('reading', {})
            initial = reading.get('caption_initial_answer')
            prediction = ((result or {}).get('answer') or {}).get('selected_option')
            truth = reference[uid]['truth']
            error = (result or {}).get('error')
            rows.append({'question_id': uid, 'completed': result is not None, 'prediction': prediction,
                         'truth': truth, 'error': error, 'correct': bool(result and not error and prediction == truth),
                         'caption_prediction': initial['selected_option'] if initial else None,
                         'caption_correct': initial['selected_option'] == truth if initial else None,
                         'previous_correct': reference[uid]['correct'], 'videotree_correct': reference[uid]['videotree_correct'],
                         'visual_reads': reading.get('visual_reads'),
                         'online_seconds': ((result or {}).get('cost_summary') or {}).get('online'),
                         'budget_violation': bool(((result or {}).get('cost_summary') or {}).get('budget_violation'))})
    done = [r for r in rows if r['completed']]
    paired = [r for r in done if r['caption_prediction'] is not None]
    report = {'planned': len(rows), 'completed': len(done), 'complete': len(done) == len(rows),
              'correct': sum(r['correct'] for r in done), 'errors': sum(bool(r['error']) for r in done),
              'accuracy': sum(r['correct'] for r in done) / len(rows) if len(done) == len(rows) else None,
              'mean_online_seconds': statistics.mean(r['online_seconds'] for r in done if r['online_seconds'] is not None) if done else None,
              'budget_violations': sum(r['budget_violation'] for r in done),
              'caption_paired': {'n': len(paired), 'baseline_correct': sum(r['caption_correct'] for r in paired),
                  'final_correct': sum(r['correct'] for r in paired),
                  'fixed': sum(not r['caption_correct'] and r['correct'] for r in paired),
                  'lost': sum(r['caption_correct'] and not r['correct'] for r in paired)},
              'rows': rows}
    write(output / 'comparison.json', report)
    return {k: v for k, v in report.items() if k != 'rows'}


def run_group(output, group):
    directory = output / 'groups' / group
    command = [sys.executable, str(output / 'source' / 'script_qa.py'), 'query', '--base', str(output / 'base'),
               '--questions', str(directory / 'questions.jsonl'), '--config', str(output / 'config.json'),
               '--out', str(directory / 'runs'), '--resume']
    with (directory / 'query.log').open('a') as log:
        result = subprocess.run(command, cwd=output / 'source', stdout=log, stderr=subprocess.STDOUT)
    from check_run_integrity import audit
    integrity = audit(directory / 'runs')
    expected = len((directory / 'questions.jsonl').read_text().splitlines())
    integrity['expected_queries'] = expected
    integrity['passed'] &= integrity['queries'] == expected
    if group == 'smoke':
        records = [read(p) for p in (directory / 'runs').glob('*/result.json')]
        guided = [r for r in records if r.get('diagnostics', {}).get('reading', {}).get('script_guided_active')]
        failures = []
        if not guided:
            failures.append('No eligible caption-first question was exercised')
        for r in guided:
            reading = r['diagnostics']['reading']
            if not reading.get('caption_initial_answer') or reading.get('planning_error'):
                failures.append('Caption review failed or baseline was not saved')
            if reading['visual_reads'] > 3 or r['cost_summary']['budget_violation']:
                failures.append('Reading or processing budget exceeded')
        integrity['caption_first_failures'] = failures
        integrity['passed'] &= not failures
    write(directory / 'integrity.json', integrity)
    return int(result.returncode != 0 or not integrity['passed'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'score'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--previous')
    parser.add_argument('--stage', choices=['smoke', '0', '1'])
    args = parser.parse_args()
    output = Path(args.out).resolve()
    if args.command == 'prepare':
        print(json.dumps(prepare(output, args.previous), ensure_ascii=False))
        return 0
    verify(output)
    if args.command == 'score':
        print(json.dumps(score(output), ensure_ascii=False))
        return 0
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('Each job must have exactly one visible GPU')
    if args.stage == 'smoke':
        return run_group(output, 'smoke')
    if not read(output / 'groups' / 'smoke' / 'integrity.json')['passed']:
        raise RuntimeError('Engineering smoke must pass before full evaluation')
    groups = ['0', '2'] if args.stage == '0' else ['1', '3']
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda group: run_group(output, group), groups))
    # Full scoring is read-only apart from an atomic summary replacement.
    print(json.dumps(score(output)), flush=True)
    return int(any(codes))


if __name__ == '__main__':
    raise SystemExit(main())
