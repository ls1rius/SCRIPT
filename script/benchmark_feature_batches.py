"""Measure EVA batches on identical cached images before launching concurrent workers."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    output = Path(args.out)
    import torch
    from script_core.backend import LocalModels
    from script_core.config import load_config
    from script_core.state import write_json
    frames = json.loads((output/'benchmark_frames.json').read_text())
    if len(frames) < 64:
        raise ValueError('Batch benchmark needs at least 64 real cached frames')
    cfg = load_config(output/'config.yaml')
    model = LocalModels(cfg)
    model.cfg['models']['batching']['visual_encoder'] = 16
    model.features(frames[:16])
    results, baseline = [], None
    for batch in [16, 32, 64]:
        model.cfg['models']['batching']['visual_encoder'] = batch
        model.features(frames[:batch])
        times = []
        torch.cuda.reset_peak_memory_stats()
        for _ in range(2):
            torch.cuda.synchronize()
            started = time.monotonic()
            vectors = torch.tensor(model.features(frames))
            torch.cuda.synchronize()
            times.append(time.monotonic()-started)
        if baseline is None:
            baseline = vectors
        difference = (vectors-baseline).abs().max().item()
        if difference > .003:
            raise ValueError(f'Batch {batch} changed normalized features beyond the bfloat16 tolerance: {difference}')
        mean = sum(times)/len(times)
        results.append({'batch': batch, 'images': len(frames), 'seconds': times,
                        'mean_seconds': mean, 'images_per_second': len(frames)/mean,
                        'peak_allocated_bytes': torch.cuda.max_memory_allocated(), 'max_absolute_feature_difference': difference})
        print('FEATURE_BATCH_BENCHMARK', json.dumps(results[-1]), flush=True)
    write_json(output/'feature_batch_benchmark.json', {'gpu': torch.cuda.get_device_name(0), 'results': results,
               'scope': 'Same real cached frames; includes PIL reads and preprocessing; excludes model startup; two measurements per batch.'})


if __name__ == '__main__':
    main()
