"""Prepare a controlled final-evidence study on CPU; never launch inference."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.run_adaptive_comparison import now, read, sha
from script.run_followup_validation import verify
from script_core.config import digest
from script_core.state import write_json as write

START = '  Description: '
END = '\n\n###\n\n Questions:'


def description_parts(inputs):
    if len(inputs['prompts']) != 1:
        raise ValueError('Expected one original user prompt')
    prompt = inputs['prompts'][0]
    if prompt.count(START) != 1 or prompt.count(END) != 1:
        raise ValueError('Ambiguous original Description boundaries')
    start = prompt.index(START) + len(START)
    end = prompt.index(END, start)
    return prompt[:start], prompt[start:end], prompt[end:]


def fixed_contract(inputs):
    prefix, _, suffix = description_parts(inputs)
    return dict(query_id=inputs['query_id'], head=inputs['head'], prefix=prefix, suffix=suffix,
                other_fields={k:v for k,v in inputs.items() if k not in ('query_id','head','prompts')})


def replace_description(original, description):
    prefix, _, suffix = description_parts(original)
    result = deepcopy(original)
    result['prompts'][0] = prefix + description + suffix
    if fixed_contract(result) != fixed_contract(original):
        raise ValueError('A field outside Description changed')
    return result


def lossless_records(row):
    captions = row['packet']['captions']
    selected = row['selected_seconds']
    if len(selected) != len(set(selected)) or any(type(i) is not int or not 0 <= i < len(captions) for i in selected):
        raise ValueError('Invalid original selected positions')
    records = [dict(caption_id=captions[i]['id'], frame_index=i,
                    sample_interval=deepcopy(captions[i]['interval']),
                    evidence=dict(original_text=captions[i]['text'])) for i in sorted(selected)]
    if len({r['caption_id'] for r in records}) != len(records):
        raise ValueError('Duplicate source caption ID')
    return records


def lossless_input(row):
    records = lossless_records(row)
    payload = json.dumps(dict(observations=records), ensure_ascii=False, separators=(',', ':'))
    result = replace_description(row['original'], payload)
    decoded = json.loads(description_parts(result)[1])['observations']
    if decoded != records:
        raise ValueError('Lossless serialization did not round-trip')
    return result


def prepare(prior, original_run, semantic_run, out):
    if out.exists():
        raise ValueError('Use a fresh design directory')
    verify(prior)
    verify(original_run)
    verify(semantic_run)
    ids = read(prior / 'cohort.json')
    if len(ids) != 300 or len(set(ids)) != 300 or ids != read(original_run / 'cohort.json') or ids != read(semantic_run / 'cohort.json'):
        raise ValueError('The fixed 300-question cohorts differ')
    controls = read(prior / 'historical.json')
    truth = read(prior / 'evaluation_only.json')
    out.mkdir(parents=True)
    checks = []
    source_hashes = {}
    for uid in ids:
        path = prior / 'inputs' / (uid + '.json')
        row = read(path)
        source = Path(controls[uid]['source'])
        calls = [json.loads(line) for line in (source.parent / 'calls.jsonl').read_text().splitlines()]
        original_calls = [call for call in calls if call['operation'] == 'videotree_qa']
        if len(original_calls) != 1 or original_calls[0]['parameters'] != row['original']:
            raise ValueError('Input is not the exact original QA call: ' + uid)
        if digest(row['original']) != row['original_input_hash']:
            raise ValueError('Original QA input digest changed: ' + uid)
        records = lossless_records(row)
        original = row['original']
        formatted = lossless_input(row)
        write(out / 'final_inputs/A_original' / (uid + '.json'), original)
        write(out / 'final_inputs/B_lossless_layout' / (uid + '.json'), formatted)
        # Normalizer input contains source evidence only, with no question/options/labels.
        bundle_path = semantic_run / 'source_bundles' / (uid + '.json')
        write(out / 'normalization_sources' / (uid + '.json'), read(bundle_path))
        source_hashes[uid] = dict(original_packet_path=str(path), original_packet_sha256=sha(path),
            original_result_path=str(source), original_result_sha256=sha(source),
            normalization_source_sha256=sha(bundle_path), original_input_digest=digest(original))
        checks.append(dict(question_id=uid, exact_saved_call=True, unchanged_fixed_contract=True,
            lossless_round_trip=True, read_caption_count=len(records),
            original_input_digest=digest(original), layout_input_digest=digest(formatted)))
    write(out / 'cohort.json', ids)
    write(out / 'smoke_cohort.json', ids[:20])
    write(out / 'evaluation_only.json', truth)
    write(out / 'historical_only.json', controls)
    write(out / 'source_hashes.json', source_hashes)
    cfg = read(original_run / 'config.json')
    write(out / 'original_config.json', cfg)
    summary = dict(updated_at=now(), questions=len(checks), all_inputs_match_original_calls=True,
        all_fixed_contracts_unchanged=True, all_lossless_payloads_round_trip=True,
        read_captions=sum(c['read_caption_count'] for c in checks), model_calls=0,
        historical_correct=sum(str(controls[u]['selected_option']) == str(truth[u]) for u in ids),
        historical_is_fresh_replay=False, checks=checks)
    write(out / 'validation/input_audit.json', summary)
    semantic_code = semantic_run / 'source/script_core/semantic_memory.py'
    manifest = dict(created_at=now(), state='design_prepared_no_inference', planned=300,
        task='Controlled final-evidence replacement on the original SCRIPT protocol',
        original_run=str(original_run), captured_input_source=str(prior),
        original_source=str(original_run / 'source'),
        original_source_manifest_sha256=sha(original_run / 'frozen_hashes.json'),
        original_model_artifacts_sha256=sha(original_run / 'control_artifacts.json'),
        original_generation=cfg['models']['generation'], original_budget=cfg['budget'],
        semantic_normalizer_source=str(semantic_code), semantic_normalizer_sha256=sha(semantic_code),
        fixed=['cohort and order','model/runtime/generation','system and user instructions',
               'question/options/order','legacy temporal-memory block','accepted visual block',
               'answer format','original parser','upstream selected evidence'],
        arms=[dict(id='A_original',description='Exact captured original SCRIPT QA input',prepared=300),
              dict(id='B_lossless_layout',description='Only Description changes to a reversible per-caption container',prepared=300),
              dict(id='C_semantic',description='Same container and source rows as B; evidence becomes semantic facts or literal residuals',
                   prepared=0,stage='requires_renderer_and_blind_semantic_audit')],
        primary_comparison='C versus A: semantic replacement under the original SCRIPT protocol',
        diagnostic_comparisons=['B versus A: lossless presentation effect','C versus B: semantic representation effect'],
        replay_gate='First fixed 20 A inputs must pass exact-input/runtime checks and prediction reproducibility audit before treatments. Never gate on accuracy.',
        scoring='Original parser primary; strict letter/unique exact option-text sidecar audit on identical raw responses, no new generation.',
        failures='Format/schema/context failures retained and counted wrong; no answer retries or silent baseline fallback. Infrastructure recovery is separate and costed.',
        answer_policy='Generate independent answers under the original protocol.',
        scope='Final-QA experiment on frozen upstream evidence; not a fresh end-to-end pipeline run.',
        no_new_gpu_job=True, new_model_calls=0, development_cohort=True)
    write(out / 'experiment.json', manifest)
    for src, name in [(Path(__file__), 'prepare_controlled_memory_design.py'),
                      (ROOT / 'docs/Controlled_Memory_Experiment_2026-10-28.md', 'design.md')]:
        shutil.copy2(src, out / name)
    files = [p for p in out.rglob('*') if p.is_file()]
    write(out / 'frozen_hashes.json', {str(p.relative_to(out)):sha(p) for p in files})
    verify(out)
    print(json.dumps(dict(out=str(out), planned=300, prepared_arms=['A_original','B_lossless_layout'],
        exact_original_inputs=300, lossless_round_trips=300, read_captions=summary['read_captions'],
        historical_correct=summary['historical_correct'], model_calls=0), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--original-run', type=Path, required=True)
    parser.add_argument('--semantic-run', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.prior.resolve(), args.original_run.resolve(), args.semantic_run.resolve(), args.out.resolve())


if __name__ == '__main__':
    main()
