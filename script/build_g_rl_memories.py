"""Build the extra 200 training memories using the exact frozen serial G extractor."""
import argparse
from copy import deepcopy
import importlib
import json
from pathlib import Path
import shutil
import sys
import time
import traceback


def read(p):
    return json.loads(Path(p).read_text())


def prepare(out, parent, full, ids):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from script_core.complete_memory import source_bundle
    from script.prepare_controlled_memory_design import lossless_input
    from script.controlled_memory_io import write, sha, now
    if out.exists():
        raise ValueError('Training-memory directory already exists')
    out.mkdir(parents=True)
    source_records = {}
    for uid in ids:
        result_path = full/'runs/script'/uid/'result.json'
        result = read(result_path)
        if result.get('error'):
            raise ValueError('Upstream training evidence failed: ' + uid)
        folder = result_path.resolve().parent
        context_path = folder/'qa_context.json'
        if not context_path.exists():
            context_path = folder/'partial_temporal/qa_context.json'
        context = read(context_path)
        packet = read(full/'inputs'/f'{uid}.json')
        packet = {k: packet[k] for k in ['question','captions']}
        calls = [json.loads(line) for line in (context_path.parent/'calls.jsonl').read_text().splitlines()]
        qa = [c for c in calls if c['operation'] == 'videotree_qa']
        if len(qa) != 1:
            raise ValueError('Expected one exact saved upstream QA request')
        original = qa[0]['parameters']
        if uid != original['query_id']:
            raise ValueError('Upstream question mismatch')
        visual = result.get('supplemental_visual', {}).get('observations', [])
        bundle = source_bundle(packet, context, visual)
        row = dict(packet=packet, original=original, selected_seconds=context['selected_seconds'])
        write(out/'final_inputs/A_original'/f'{uid}.json', original)
        write(out/'source_observations'/f'{uid}.json', lossless_input(row))
        write(out/'normalization_sources'/f'{uid}.json', bundle)
        write(out/'packets'/f'{uid}.json', {'packet': packet})
        source_records[uid] = {'result_path': str(result_path.resolve()), 'result_sha256': sha(result_path),
                               'context_path': str(context_path), 'context_sha256': sha(context_path),
                               'original_input_sha256': qa[0]['input_hash']}
    for name in ['qa_config.json','qa_expected_artifacts.json']:
        shutil.copy2(parent/name, out/name)
    shutil.copytree(parent/'runtime/qa', out/'runtime/qa', symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(parent/'control', out/'control', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(__file__, out/'control/build_g_rl_memories.py')
    write(out/'cohort.json', ids)
    write(out/'source_records.json', source_records)
    write(out/'inherited_G_functional_gate.json', {
        'parent': str(parent), 'gate_sha256': sha(parent/'validation/functional.json'),
        'gate': read(parent/'validation/functional.json'),
        'scope': 'Exact unchanged G extractor/runtime; parent gate reused, not a new model accuracy check.'})
    write(out/'experiment.json', {'created_at': now(), 'parent_G': str(parent), 'planned': len(ids),
          'purpose': 'Frozen G memories for the user-specified remaining-200 RL training set; no training answers generated here.',
          'extraction_cap': 1500, 'answer_reserve': 45, 'max_identity_gap': 8, 'max_state_gap': 2,
          'execution': 'Original serial source calls and at most one full role review; no batch optimization.',
          'new_retrieval_or_visual_observations': False})
    write(out/'frozen_hashes.json', {str(p.relative_to(out)): sha(p) for p in out.rglob('*')
                                   if p.is_file() and not p.is_symlink()})
    write(out/'progress.json', {'stage': 'prepared', 'completed': 0, 'planned': len(ids), 'updated_at': now()})


def worker(out):
    # Import only the frozen parent's packages in this separate process.
    sys.path.insert(0, str(out/'runtime/qa'))
    sys.path.insert(0, str(out/'control'))
    import controlled_memory_io as io
    import run_generalized_entity_experiment as G
    import compact_protocol as protocol
    core = importlib.import_module('script_core.generalized_entity_memory')
    G.verify(out)
    if not read(out/'inherited_G_functional_gate.json')['gate']['passed']:
        raise ValueError('Parent G functional gate did not pass')
    cfg, backend, Ledger, Meter = protocol.warm_backend(out, 'qa', 'G_train_200')
    policy = read(out/'experiment.json')
    ids = read(out/'cohort.json')
    try:
        for index, uid in enumerate(ids):
            folder = out/'materialized'/uid
            if folder.exists():
                raise ValueError('A source extraction attempt already exists; inspect retained costs')
            bundle = read(out/'normalization_sources'/f'{uid}.json')
            observations = G.source_observations(out, uid, bundle)
            ledger = Ledger(policy['extraction_cap'], answer_reserve=policy['answer_reserve'],
                            calls_path=folder/'calls.jsonl')
            meter = Meter(backend, ledger, cfg, folder/'raw_calls')
            source_results = {}
            began = time.process_time()
            try:
                for unit in bundle['units']:
                    io.write(out/'progress.json', {'stage': 'extracting', 'completed': index, 'planned': len(ids),
                             'question_id': uid, 'sources_completed': len(source_results),
                             'source_units': len(bundle['units']), 'updated_at': io.now()})
                    value = G.extract_source(unit, meter, core, protocol)
                    source_results[unit['id']] = value
                    io.write(folder/'source_results'/f"{unit['id']}.json", value)
                memory, audit = core.materialize(observations, source_results,
                    policy['max_identity_gap'], policy['max_state_gap'])
                description, rendered = core.render(memory)
                original = read(out/'final_inputs/A_original'/f'{uid}.json')
                prefix, _, suffix = io.parts(original)
                inputs = deepcopy(original)
                inputs['prompts'][0] = prefix + description + suffix
                if io.fixed(inputs) != io.fixed(original):
                    raise ValueError('Training G input changed the fixed original QA contract')
                audit.update(render=rendered, source_units=len(bundle['units']),
                    failed_source_units=sum(v['error'] is not None for v in source_results.values()),
                    role_reviews=sum(v['role_review'] for v in source_results.values()),
                    joint_model_calls=sum(c['operation'] == core.OPERATION for c in ledger.calls),
                    preparation_cpu_seconds=time.process_time()-began)
                io.write(folder/'memory.json', memory)
                io.write(folder/'audit.json', audit)
                io.write(out/'final_inputs/G_generalized_entities'/f'{uid}.json', inputs)
                io.write(folder/'sealed.json', {name: io.sha(folder/name) for name in ['memory.json','audit.json']})
            finally:
                io.write(folder/'cost_summary.json', ledger.summary())
                meter.close()
            print('G_RL_MEMORY', index+1, '/', len(ids), uid, flush=True)
        io.write(out/'progress.json', {'stage': 'complete', 'completed': len(ids), 'planned': len(ids), 'updated_at': io.now()})
        G.verify(out)
        return 0
    finally:
        backend.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    try:
        raise SystemExit(worker(args.out.resolve()))
    except Exception:
        traceback.print_exc()
        raise
