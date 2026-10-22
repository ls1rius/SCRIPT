"""Prepare matched 300-question key-anchor and hierarchical-memory experiments."""
import argparse
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'script'))
from run_scene_lazy import prepare as prepare_scene, main as run_main
from run_summary_graph import read, write, sha

ARMS = {'c0': 'Original VideoTree through SCRIPT, fresh',
    'scene_lazy': 'Previous scene-lazy pipeline, fresh matched control',
    'scene_keys': 'Scene-lazy plus source-backed key anchors and typed correspondence',
    'scene_hierarchical': 'Key anchors plus block directory construction and bounded hierarchy/pointers'}


def prepare(output, reference, videotree):
    prepare_scene(output, reference, videotree)
    spec = read(output/'experiment.json')
    spec.update(mode='scene_key_memory_ablation', arms=ARMS, profiles=ARMS,
        parents={'scene_lazy': 'c0', 'scene_keys': 'scene_lazy', 'scene_hierarchical': 'scene_keys'})
    spec['notes'] += [
        'All four arms execute fresh; previous frozen experiments are not modified or used as inference input.',
        'Key anchors are extracted without questions, options or answers, at most six per 30-caption batch plus two preceding captions.',
        'Anchors retain original source text/times and distinguish unknown prior state and unknown persistence.',
        'Local entity/relation graphs are still built only after opening a scene; cross-scene typed correspondences require traversal.',
        'Key-mode directories allow 600 seconds of measured preprocessing; basic directories allow 300; all costs are reported.',
        'Both new arms still have the same 600-second online cap and maximum four opened scenes.',
        'Hierarchical arm partitions at most 120 captions per model call, embeds at most 16 documents per call, uses fanout eight/beam two.',
        'Hierarchical arm uses bounded approximate semantic pointer candidates, not an all-pairs nearest-neighbor search.',
        'Key retrieval samples capped inverted postings before dense reranking. It may miss common-term matches and has no recall guarantee.',
        'Hierarchy navigation excerpts can be shortened; original captions and independent key records remain intact.',
        'Key arm is a bundled retrieval/context/typed-correspondence change; hierarchy arm also changes phase boundaries.',
        'Real accuracy uses the original short 300-question cohort. Synthetic CPU scale checks are not long-video accuracy evidence.',
        'Full artifacts remain linear in input size and are loaded/validated in memory; this is not a disk-streaming unbounded-memory service.']
    write(output/'experiment.json', spec)
    hashes = read(output/'source_hashes.json')
    for name in ('script/run_key_memory.py', 'script/key_memory.pbs', 'configs/script_key_memory.yaml'):
        target = output/'source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, target)
        hashes[name] = sha(target)
    write(output/'source_hashes.json', hashes)
    write(output/'input_hashes.json', {p: sha(output/p) for p in read(output/'input_hashes.json')})
    return {'prepared': 300, 'arms': ARMS, 'output': str(output)}


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'prepare':
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('command')
        parser.add_argument('--out', required=True)
        parser.add_argument('--reference', required=True)
        parser.add_argument('--videotree', required=True)
        args = parser.parse_args()
        import json
        print(json.dumps(prepare(Path(args.out).resolve(), args.reference, args.videotree)))
    else:
        raise SystemExit(run_main())
