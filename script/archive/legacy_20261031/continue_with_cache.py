"""Continue one frozen experiment with isolated caches and its completed work."""
from pathlib import Path
import sys
_PROJECT_ROOT = next(p for p in Path(__file__).resolve().parents if (p/'script_core').is_dir())
sys.path[:0] = [str(_PROJECT_ROOT), str(_PROJECT_ROOT/'script')]

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = _PROJECT_ROOT
sys.path.insert(0, str(ROOT))
from script import run_five_comparison as runner
from script.run_adaptive_comparison import read, write, sha, verify, now
from script_core.backend import preflight
from script_core.directory_cache import identity
from script_core.persistent_cache import TextCache, OPERATIONS, SOFTWARE, source_identity, effective_config
from script_core.temporal_entity import validate_directory


def prepare(prior, out):
    verify(prior)
    previous = read(prior/'config.json')
    old_artifacts = read(prior/'artifacts.json')
    runner.freeze(out, len(read(prior/'cohort.json')))
    cfg = read(out/'config.json')
    if effective_config(previous) != effective_config(cfg) or read(prior/'settings.json') != read(out/'settings.json'):
        raise ValueError('A cache continuation cannot change method configuration')
    for name in ('cohort.json', 'questions.json', 'captions.json', 'evaluation_only.json'):
        if read(prior/name) != read(out/name):
            raise ValueError('Continuation inputs changed: '+name)
    if source_identity(str(prior/'source/script_core')) != source_identity(str(out/'source/script_core')):
        raise ValueError('Model adapter/schema/method source changed; do not carry old outputs')
    old_pipeline = (prior/'source/script_core/temporal_pipeline.py').read_text()
    new_pipeline = (out/'source/script_core/temporal_pipeline.py').read_text()
    new_pipeline = new_pipeline.replace(
        "shared=bundle['preparation_cost_summary'].get('budgeted_total', bundle['preparation_cost_summary']['total'])",
        "shared=bundle['preparation_cost_summary']['total']")
    if old_pipeline != new_pipeline:
        raise ValueError('Pipeline logic changed beyond cache cost accounting')
    runtime, artifacts = preflight(cfg)
    for kind in ('visual_encoder', 'vlm', 'detector', 'text_embedding'):
        if artifacts[kind]['hash'] != old_artifacts[kind]['hash']:
            raise ValueError('Model contents changed: '+kind)
    software = {name: old_artifacts['software'][name] for name in SOFTWARE}
    if software != {name: artifacts['software'][name] for name in SOFTWARE}:
        raise ValueError('Model runtime software changed')
    write(out/'artifacts.json', artifacts)
    seeded = Counter()
    for log in prior.rglob('calls.jsonl'):
        if 'source' in log.relative_to(prior).parts:
            continue
        cache = TextCache(runtime, log.parent/'raw_calls', source_root=prior/'source/script_core', software=software)
        for line in log.read_text().splitlines():
            call = json.loads(line)
            if call['operation'] not in OPERATIONS or call.get('error'):
                continue
            path = Path(call['output_ref'])
            if not path.is_file():
                continue
            raw = read(path)
            if raw.get('error') or not isinstance(raw.get('output'), dict):
                continue
            if cache.put(call['operation'], raw['inputs'], raw['output'], call['actual_cost'],
                dict(experiment=str(prior), call_id=call['call_id'], output_ref=str(path),
                     output_file_hash=sha(path))):
                seeded[cache.scope] += 1
    directories = []
    for path in prior.glob('*/directories/*/directory.json'):
        uid = path.parent.name
        packet = read(prior/'inputs'/f'{uid}.json')
        if sha(packet['feature_uri']) != packet['feature_hash']:
            raise ValueError('Preprocessed feature content changed: '+uid)
        d = read(path)
        validate_directory(d)
        if d['captions'] != packet['captions']:
            raise ValueError('Directory original summaries changed')
        key = identity(packet, runtime, read(out/'settings.json'), prior/'source/script_core', software)
        from script_core.config import digest
        dest = out/'preprocessing/directories'/uid/key/'directory.json'
        write(dest, dict(key=key, directory=d, checksum=digest(d), origin=str(path)))
        shutil.copytree(path.parent, out/path.parent.relative_to(prior), dirs_exist_ok=True)
        directories.append(dict(video_id=uid, source=str(path), key=key))
    carried = []
    for phase in ('smoke', 'formal'):
        for arm in runner.ARMS:
            for path in (prior/phase/'arms'/arm/'runs').glob('*/result.json'):
                uid = path.parent.name
                shutil.copytree(path.parent, out/path.parent.relative_to(prior), dirs_exist_ok=True)
                carried.append(dict(phase=phase, arm=arm, question_id=uid, result_hash=sha(path)))
                for related in (prior/phase/'memory'/arm/f'{uid}.json', prior/phase/'ablation'/uid/'result.json'):
                    if related.exists():
                        if related.name == 'result.json':
                            shutil.copytree(related.parent, out/related.parent.relative_to(prior), dirs_exist_ok=True)
                        else:
                            dest = out/related.relative_to(prior)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(related, dest)
    for path in (prior/'verification/refill').glob('**/result.json'):
        shutil.copytree(path.parent, out/path.parent.relative_to(prior), dirs_exist_ok=True)
    continuation = dict(prior=str(prior), same_experiment=True, cross_arm_reuse=False,
        seeded_text_calls=dict(seeded), reused_directories=directories, carried_results=carried,
        interrupted_attempts_retained_at=str(prior), source_fingerprint_verified=True,
        checkpoint_contents_verified=True, updated_at=now())
    write(out/'verification/cache_continuation.json', continuation)
    experiment = read(prior/'experiment.json')
    experiment.update(parent=str(prior), continuation=continuation,
        reuse_policy='Only this experiment; caches isolated by arm; original preprocessing directory retained; no prior experiment or other method result seeds a method cache',
        cached_cost_policy='Physical execution and original source processing costs are reported separately; original source cost remains in each method budget')
    write(out/'experiment.json', experiment)
    write(out/'resource_policy.json', read(prior/'resource_policy.json'))
    controls = read(out/'control_hashes.json')
    write(out/'control_hashes.json', {n:sha(out/n) for n in controls})
    verify(out)
    return continuation


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--detach', action='store_true')
    args = parser.parse_args()
    out, prior = args.out.resolve(), args.prior.resolve()
    summary = prepare(prior, out)
    if args.detach:
        command = [sys.executable, '-u', str(out/'source/script/run_five_comparison.py'), 'queue',
                   '--out', str(out), '--allowed-gpus', '1', '2', '3', '--poll-seconds', '30']
        start = out/'start_queue.sh'
        start.write_text('#!/bin/bash\nset -euo pipefail\ncd '+shlex.quote(str(out/'source'))+
                         '\nexec '+shlex.join(command)+' >> '+shlex.quote(str(out/'controller.log'))+' 2>&1\n')
        session = 'script-cache-continuation-'+str(int(__import__('time').time()))
        subprocess.run(['tmux', 'new-session', '-d', '-s', session, 'bash '+shlex.quote(str(start))], check=True)
        write(out/'launch.json', dict(session=session, command=command, created_at=now()))
    print(json.dumps(summary, indent=2))
