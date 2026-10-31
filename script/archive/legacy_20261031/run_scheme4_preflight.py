"""Real-model cause and event regression checks, selected without accuracy labels."""
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
    from script.run_optimized_comparison import inventory, refill_probe
    from script.run_temporal_entity import checked_packet
    from script_core import adaptive_entity, optimized_entity
    from script_core.budget import BudgetLedger, Meter, ModelWorker
    from script_core.config import load_config
    verify(prior)
    if out.exists():
        raise ValueError('Select a fresh validation output directory')
    row = inventory()[gpu]
    # Earlier live runs peak below 40 GiB including both loaded models.
    # Keep at least 8 GiB headroom without waiting for a fully idle device.
    if row['free_mib'] < 48*1024:
        raise ValueError('Insufficient free GPU memory')
    lease = (Path('/tmp')/f'videoagent-{os.getuid()}-{row["uuid"]}.lock').open('a')
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.environ.update(CUDA_VISIBLE_DEVICES=row['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID', HF_HUB_OFFLINE='1',
        HF_HOME=str(ROOT/'checkpoints/.cache/huggingface'), HF_MODULES_CACHE=str(ROOT/'checkpoints/.cache/modules'),
        TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='4', MKL_NUM_THREADS='4')
    out.mkdir(parents=True)
    cases = []
    for uid in read(prior/'cohort.json'):
        p = prior/'formal/arms/adaptive_scheme4/runs'/uid/'result.json'
        if p.exists() and read(p).get('error'):
            cases.append(('adaptive_scheme4', uid))
    # Event failures with/without existing anchors, both early exit and plan-limit
    # paths, plus a previously complete control. Selection never reads truths.
    cases.extend(('optimized_scheme4', uid) for uid in (
        '68f6ddfd-bf42-4bee-b1c9-a48db428e586', 'f60b20bb-ba25-4eea-a09f-01bbe8bbda88',
        'edd710a7-0f50-45dc-8619-de4bcaedee9b', 'e97c3e1c-27ee-4b3d-8783-2e325b0eada8',
        '5d15c716-f179-462c-84a6-fa94e9d14e94', 'dfb6c468-e124-40f6-9c4e-c13ee45a2ad9'))
    hashes = {str(p.relative_to(ROOT)): sha(p) for folder in ('script_core', 'script')
              for p in (ROOT/folder).glob('*.py')}
    write(out/'selection.json', dict(prior=str(prior), cases=cases, source_hashes=hashes,
        gpu_index=gpu, gpu_uuid=row['uuid'], minimum_free_mib=48*1024, accuracy_not_used=True,
        methods=dict(adaptive_scheme4=adaptive_entity.VERSION, optimized_scheme4=optimized_entity.VERSION)))
    cfg, settings = load_config(prior/'config.json'), read(prior/'settings.json')
    backend = ModelWorker(cfg)
    results, probes = [], {}
    try:
        meter = Meter(backend, BudgetLedger(600, answer_reserve=0, calls_path=out/'setup/calls.jsonl'),
                      cfg, out/'setup/raw_calls')
        try:
            meter.call('prepare_vlm', {}, build=True)
            meter.call('clip_text', dict(texts=['video']), build=True)
        finally:
            meter.close()
        for arm, uid in cases:
            write(out/'progress.json', dict(stage='real_model_regression', arm=arm, question_id=uid,
                completed=len(results), planned=len(cases), updated_at=now()))
            packet = checked_packet(prior, uid, check_images=True)
            directory = read(prior/'formal/directories'/uid/'directory.json')
            implementation = adaptive_entity.run if arm == 'adaptive_scheme4' else optimized_entity.run
            result = implementation(packet, directory, packet['frames'], backend, cfg,
                out/'smoke/arms'/arm/'runs'/uid, settings, memory_path=out/'smoke/memory'/arm/f'{uid}.json')
            selected = {k: result.get(k) for k in ('error', 'answer', 'incomplete', 'entity_count', 'event_count',
                'rejected_field_count', 'extraction_depth', 'stop_reason', 'cost_summary')}
            selected.update(arm=arm, question_id=uid)
            results.append(selected)
            print(json.dumps({k:v for k,v in selected.items() if k not in ('cost_summary','answer')}), flush=True)
            if arm not in probes:
                probes[arm] = (source_link_probe(out, packet, directory, backend, cfg, arm=arm)
                    if arm == 'adaptive_scheme4' else refill_probe(out, packet, directory, backend, cfg))
            passed = all(r['error'] is None and r['answer'] and not r['cost_summary']['budget_violation']
                and not any(i['stage'] == 'requested_structure' for i in r['incomplete']) for r in results)
            passed = passed and all(p['passed'] for p in probes.values())
            write(out/'report.json', dict(completed=len(results), planned=len(cases), passed_so_far=bool(passed),
                results=results, probes=probes, accuracy_not_used=True, updated_at=now()))
        unchanged = all(sha(ROOT/name) == value for name, value in hashes.items())
        passed = bool(passed and unchanged)
        write(out/'gate.json', dict(passed=passed, completed=len(results), planned=len(cases),
            source_unchanged=unchanged, accuracy_not_used=True, updated_at=now()))
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
