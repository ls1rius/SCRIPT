"""Prepare new compact QA inputs and CPU token counts from frozen B/C inputs."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import controlled_memory_io as io
from script_core.qa_evidence_view import compact_description


def project(inputs, merge_repeated=False):
    before, description, after = io.parts(inputs)
    text, audit = compact_description(json.loads(description), merge_repeated=merge_repeated)
    result = deepcopy(inputs)
    result['prompts'][0] = before + text + after
    if io.fixed(result) != io.fixed(inputs):
        raise ValueError('A field outside Description changed')
    return result, audit


def preview(run, out, tokenizer=None, merge_repeated=False):
    run, out = Path(run).resolve(), Path(out).resolve()
    if out.exists():
        raise ValueError('Use a new preview directory')
    arms = ('A_original', 'B_lossless_layout', 'C_semantic')
    ids = [uid for uid in io.read(run/'cohort.json') if all(
        (run/'final_inputs'/arm/f'{uid}.json').is_file() for arm in arms)]
    if not ids:
        raise ValueError('No common prepared A/B/C inputs')
    out.mkdir(parents=True)
    io.write(out/'cohort.json', ids)
    counts = {}
    projections = {}

    def measure(arm, inputs):
        _, description, _ = io.parts(inputs)
        row = dict(description_characters=len(description),
                   prompt_characters=len(inputs['head']) + len(inputs['prompts'][0]))
        if tokenizer is not None:
            messages = [dict(role='system', content=inputs['head']),
                        dict(role='user', content=inputs['prompts'][0])]
            row['input_tokens'] = len(tokenizer.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True))
        counts.setdefault(arm, []).append(row)
        return row

    for uid in ids:
        for arm in arms:
            path = run/'final_inputs'/arm/f'{uid}.json'
            original = io.read(path)
            measure(arm, original)
            if arm == 'A_original':
                continue
            compact, audit = project(original, merge_repeated=merge_repeated)
            name = 'B_compact_text' if arm.startswith('B_') else 'C_compact_text'
            if merge_repeated:
                unmerged, _ = project(original)
                measure(name, unmerged)
                name = name.replace('_text', '_merged')
            totals = projections.setdefault(name, dict(source_rows=0, rendered_rows=0))
            for key in totals:
                totals[key] += audit[key]
            measurements = measure(name, compact)
            io.write(out/'final_inputs'/name/f'{uid}.json', compact)
            io.write(out/'audits'/name/f'{uid}.json', dict(audit,
                source_input=str(path), source_sha256=io.sha(path),
                projected_input_sha256=io.digest(compact), measurements=measurements))
    summary = dict(created_at=io.now(), source_run=str(run), questions=len(ids),
        kind='CPU input preview; no answers generated', model_calls=0,
        code_hashes={str(p.relative_to(ROOT)):io.sha(p) for p in (
            ROOT/'script_core/qa_evidence_view.py', Path(__file__).resolve(),
            ROOT/'script/controlled_memory_io.py')},
        tokenizer=str(tokenizer.name_or_path) if tokenizer is not None else None,
        repeat_merging=merge_repeated, projection_totals=projections,
        fixed_regions_unchanged=True, variants={arm:{key:statistics.mean(r[key] for r in rows)
            for key in rows[0]} for arm, rows in counts.items()})
    io.write(out/'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--tokenizer', type=Path)
    parser.add_argument('--merge-repeated', action='store_true',
                        help='Group consecutive identical evidence while preserving each sampled interval')
    args = parser.parse_args()
    tokenizer = None
    if args.tokenizer:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)
    print(json.dumps(preview(args.run, args.out, tokenizer, args.merge_repeated), ensure_ascii=False, indent=2))
