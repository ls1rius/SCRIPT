"""Attach a coverage arm to an already frozen caption-first run and score pairs."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def prepare(output, guided):
    output, guided = Path(output).resolve(), Path(guided).resolve()
    if output.exists():
        raise ValueError('Choose a new matched experiment directory')
    # Keep running inference code immutable. The new arm uses byte-identical code.
    for name, expected in read(guided / 'source_hashes.json').items():
        if sha(guided / 'source' / name) != expected:
            raise ValueError('Guided source hash mismatch: ' + name)
    coverage = output / 'coverage_first'
    coverage.mkdir(parents=True)
    shutil.copytree(guided / 'source', coverage / 'source', symlinks=True)
    for name in ('base/videos.json', 'cohort.json', 'reference_only.json', 'source_hashes.json', 'reuse_hashes.json'):
        target = coverage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(guided / name, target)
    cfg = read(guided / 'config.json')
    cfg['query']['reading_policy'] = 'coverage_first'
    write(coverage / 'config.json', cfg)
    for i in range(4):
        target = coverage / 'groups' / str(i)
        target.mkdir(parents=True)
        shutil.copyfile(guided / 'groups' / str(i) / 'questions.jsonl', target / 'questions.jsonl')
    # The shared engineering gate includes a strict-policy coverage control.
    gate = coverage / 'groups' / 'smoke'
    gate.mkdir()
    (gate / 'integrity.json').symlink_to(guided / 'groups' / 'smoke' / 'integrity.json')
    frozen = ['config.json', 'base/videos.json', 'cohort.json', 'reference_only.json',
              *[f'groups/{i}/questions.jsonl' for i in range(4)]]
    write(coverage / 'input_hashes.json', {name: sha(coverage / name) for name in frozen})
    write(coverage / 'experiment.json', {'mode': 'coverage_first_reused_m0', 'count': 300,
          'shared_engineering_gate': str(guided / 'groups' / 'smoke' / 'integrity.json'),
          'source_identical_to': str(guided), 'fresh_m0_videos': 0, 'reused_m0_videos': 300})
    (output / 'script_guided').symlink_to(guided, target_is_directory=True)
    analysis = output / 'analysis_source'
    analysis.mkdir()
    for name in ('compare_caption_first_arms.py', 'caption_first_coverage.pbs'):
        shutil.copyfile(Path(__file__).parent / name, analysis / name)
    write(output / 'analysis_source_hashes.json', {p.name: sha(p) for p in analysis.iterdir()})
    spec = {'arms': {'coverage_first': str(coverage), 'script_guided': str(guided)},
            'count_per_arm': 300, 'shared_m0': True, 'identical_inference_source': True,
            'max_simultaneous_gpus': 2,
            'scheduling': 'Each coverage job starts after its corresponding guided job ends, including per-query failures.',
            'limitations': ['Development cohort; provided captions and existing M0 are reused.',
                            'Coverage retains the coverage policy; caption-first branching applies to eligible guided questions.',
                            'No pure text M0 rebuild is part of this evaluation.']}
    write(output / 'experiment.json', spec)
    return spec


def paired(coverage, guided):
    if set(coverage) != set(guided):
        raise ValueError('Matched arms must contain the exact same question IDs')
    counts = dict(both_correct=0, coverage_only=0, guided_only=0, both_wrong=0)
    for uid, a in coverage.items():
        b = guided[uid]
        if not a['completed'] or not b['completed']:
            continue
        key = ('both_correct' if a['correct'] and b['correct'] else 'coverage_only' if a['correct']
               else 'guided_only' if b['correct'] else 'both_wrong')
        counts[key] += 1
    return {'completed_pairs': sum(counts.values()), **counts}


def score(output):
    output = Path(output).resolve()
    with (output / 'score.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        spec = read(output / 'experiment.json')
        summaries, rows = {}, {}
        for arm, path in spec['arms'].items():
            root = Path(path)
            subprocess.run([sys.executable, str(root / 'source' / 'script' / 'run_caption_first.py'),
                            'score', '--out', str(root)], check=True, capture_output=True, text=True)
            result = read(root / 'comparison.json')
            rows[arm] = {r['question_id']: r for r in result['rows']}
            summaries[arm] = {k: v for k, v in result.items() if k != 'rows'}
            values = [r['visual_reads'] for r in result['rows'] if r['completed'] and r['visual_reads'] is not None]
            summaries[arm]['mean_visual_reads'] = statistics.mean(values) if values else None
        if set(rows['coverage_first']) != set(read(Path(spec['arms']['script_guided']) / 'cohort.json')):
            raise ValueError('Results differ from the frozen cohort')
        report = {'complete': all(r['complete'] for r in summaries.values()), 'groups': summaries,
                  'paired': paired(rows['coverage_first'], rows['script_guided']),
                  'same_m0': True, 'identical_inference_source': True, 'rows': rows}
        write(output / 'comparison.json', report)
        return {k: v for k, v in report.items() if k != 'rows'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'score'])
    parser.add_argument('--out', required=True)
    parser.add_argument('--guided')
    args = parser.parse_args()
    print(json.dumps(prepare(args.out, args.guided) if args.command == 'prepare' else score(args.out), ensure_ascii=False))
