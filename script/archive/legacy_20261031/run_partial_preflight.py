"""Real-model regression gate for repaired partial extraction, without answer labels."""
from pathlib import Path
import sys
_PROJECT_ROOT = next(p for p in Path(__file__).resolve().parents if (p/'script_core').is_dir())
sys.path[:0] = [str(_PROJECT_ROOT), str(_PROJECT_ROOT/'script')]

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

ROOT = _PROJECT_ROOT
sys.path.insert(0, str(ROOT))
from script.run_adaptive_comparison import now, read, sha, source_link_probe, verify
from script_core.state import write_json as write


def run(prior, out, gpu):
    from script.run_optimized_comparison import inventory
    from script.run_temporal_entity import checked_packet
    from script_core.adaptive_transition import run as partial, VERSION
    from script_core.budget import BudgetLedger, Meter, ModelWorker
    from script_core.config import load_config
    verify(prior)
    if out.exists():
        raise ValueError('Preflight requires a fresh output directory')
    row = inventory()[gpu]
    if row['free_mib'] < 56*1024:
        raise ValueError('Insufficient GPU memory for preflight')
    lease = (Path('/tmp')/f'videoagent-{os.getuid()}-{row["uuid"]}.lock').open('a')
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.environ.update(CUDA_VISIBLE_DEVICES=row['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
        HF_HUB_OFFLINE='1', HF_HOME=str(ROOT/'checkpoints/.cache/huggingface'),
        HF_MODULES_CACHE=str(ROOT/'checkpoints/.cache/modules'), TOKENIZERS_PARALLELISM='false',
        OMP_NUM_THREADS='4', MKL_NUM_THREADS='4')
    out.mkdir(parents=True)
    # First smoke pair plus one representative of every observed error message.
    # Selection uses execution errors only, never answer labels or accuracy.
    ids, seen = read(prior/'cohort.json')[:2], set()
    for uid in read(prior/'cohort.json'):
        p = prior/'formal/arms/adaptive_partial/runs'/uid/'result.json'
        if p.exists() and (error := read(p).get('error')) and error['message'] not in seen:
            seen.add(error['message'])
            if uid not in ids:
                ids.append(uid)
    source_hashes = {str(p.relative_to(ROOT)): sha(p) for folder in ('script_core', 'script')
                     for p in (ROOT/folder).glob('*.py')}
    write(out/'selection.json', dict(prior=str(prior), ids=ids, error_messages=sorted(seen),
        selection='First two smoke questions and first occurrence of each execution error; no accuracy labels',
        method=VERSION, source_hashes=source_hashes, gpu_index=gpu, gpu_uuid=row['uuid']))
    cfg, settings = load_config(prior/'config.json'), read(prior/'settings.json')
    backend, results, probes = ModelWorker(cfg), {}, {}
    try:
        meter = Meter(backend, BudgetLedger(600, answer_reserve=0, calls_path=out/'setup/calls.jsonl'),
                      cfg, out/'setup/raw_calls')
        try:
            meter.call('prepare_vlm', {}, build=True)
            meter.call('clip_text', dict(texts=['video']), build=True)
        finally:
            meter.close()
        for uid in ids:
            write(out/'progress.json', dict(stage='regression', question_id=uid,
                completed=len(results), planned=len(ids), updated_at=now()))
            packet = checked_packet(prior, uid, check_images=True)
            directory = read(prior/'formal/directories'/uid/'directory.json')
            result = partial(packet, directory, packet['frames'], backend, cfg,
                out/'smoke/arms/adaptive_partial/runs'/uid, settings,
                memory_path=out/'smoke/memory/adaptive_partial'/f'{uid}.json')
            results[uid] = {k: result[k] for k in ('error', 'answer', 'incomplete',
                'entity_count', 'relation_count', 'rejected_field_count', 'cost_summary')}
            print(json.dumps(dict(question_id=uid, **{k: result[k] for k in
                ('error', 'incomplete', 'entity_count', 'relation_count', 'rejected_field_count')})), flush=True)
            if uid in ids[:2]:
                probes[uid] = source_link_probe(out, packet, directory, backend, cfg, arm='adaptive_partial')
            passed = all(r['error'] is None and r['answer'] and not r['cost_summary']['budget_violation']
                         for r in results.values()) and all(p['passed'] for p in probes.values())
            write(out/'report.json', dict(completed=len(results), planned=len(ids), passed_so_far=bool(passed),
                results=results, probes=probes, accuracy_not_used=True, updated_at=now()))
        unchanged = all(sha(ROOT/name) == h for name, h in source_hashes.items())
        passed = bool(passed and unchanged)
        write(out/'gate.json', dict(passed=passed, completed=len(results), planned=len(ids),
            source_unchanged=unchanged, accuracy_not_used=True, updated_at=now(),
            incomplete_policy='Rejected fields remain visible; answers must use only retained sourced facts and original captions'))
        write(out/'progress.json', dict(stage='complete' if passed else 'failed', updated_at=now()))
        return 0 if passed else 1
    finally:
        backend.close()
        lease.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--gpu', choices=('1', '2', '3'), default='2')
    args = parser.parse_args()
    raise SystemExit(run(args.prior.resolve(), args.out.resolve(), args.gpu))
