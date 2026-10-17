"""Live answer ablation on frozen evidence; not an end-to-end runtime benchmark."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import statistics
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script_core.backend import preflight
from script_core.budget import BudgetLedger, Meter, ModelWorker, ResourceExhausted, ToolFailure
from script_core.config import load_config, digest
from script_core.state import write_json
from script_core.video import file_hash
from script_core.answering import reconcile_answer


def prepare(source, output, cohort=300):
    """Freeze only existing answer inputs; labels stay in offline evaluation."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Choose a new immutable recheck directory')
    root = Path(__file__).resolve().parents[1]
    rows = json.loads((source/f'comparison_{cohort}.json').read_text())['rows']
    cohort_ids = [row['question_id'] for row in rows]
    directories = {json.loads((p/'query.json').read_text())['id']: p
                   for p in source.glob('shards/*/script_runs/*') if (p/'result.json').is_file()}
    # Inspect the two motivating cases and two previously correct controls
    # first. All other successful answers in the same cohort follow unchanged.
    priority = ['45fd8bca-c836-4601-b2c8-cc2a08cf8f86', '8345d177-0ed4-4965-8455-bd17d2b42399',
                '0c51c89d-d86b-45de-a1cf-c65153a7fbc1', '5f584429-b12b-4b15-9975-bf0cbfeaf2bc']
    ordered = [uid for uid in priority if uid in cohort_ids] + [uid for uid in cohort_ids if uid not in priority]
    manifest, skipped = [], []
    for uid in ordered:
        directory = directories[uid]
        result = json.loads((directory/'result.json').read_text())
        if result.get('error') or not result.get('answer'):
            skipped.append(uid)
            continue
        calls = [json.loads(line) for line in (directory/'calls.jsonl').read_text().splitlines()]
        call = next(c for c in reversed(calls) if c['operation'] == 'answer' and c.get('output_ref'))
        raw = directory/call['output_ref']
        inputs = deepcopy(json.loads(raw.read_text())['inputs'])
        if 'selected_option' in inputs['stop_decision']:
            inputs['stop_decision']['candidate_option'] = inputs['stop_decision'].pop('selected_option')
        relative = f'inputs/{uid}.json'
        write_json(output/relative, inputs)
        manifest.append({'question_id': uid, 'input_file': relative, 'input_sha256': file_hash(output/relative),
                         'source_answer_call': str(raw.resolve()), 'source_sha256': file_hash(raw),
                         'source_answer_seconds': call.get('actual_cost')})
    write_json(output/'input_manifest.json', manifest)
    write_json(output/'evaluation_only.json', {'rows': rows})
    shutil.copyfile(source/'config.yaml', output/'config.yaml')
    files = [*root.glob('script_core/*.py'), root/'script/recheck_answers.py',
             root/'script/answer_recheck.pbs', root/'script/run_pilot_comparison.py', root/'requirements.txt']
    hashes = {}
    for path in files:
        relative = str(path.relative_to(root))
        target = output/'source'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        hashes[relative] = file_hash(target)
    for name in ('checkpoints', 'data'):
        (output/'source'/name).symlink_to((root/name).resolve(), target_is_directory=True)
    write_json(output/'source_hashes.json', hashes)
    spec = {'mode': 'fixed_evidence_contrastive_answer_recheck', 'video_ids': [r['question_id'] for r in manifest],
            'count': len(manifest), 'source_cohort': cohort, 'skipped_without_answer': skipped,
            'source': str(source), 'gpu_jobs': 1, 'evaluation_only_sha256': file_hash(output/'evaluation_only.json'),
            'limitations': ['Development-cohort replay; not held-out accuracy.',
                            'Frozen observations/captions; only the final answer changes.',
                            'Answer-stage timing is not end-to-end latency or a complete 600-second budget audit.',
                            'Per-option semantic judgments are model outputs, not proofs.']}
    write_json(output/'pilot_spec.json', spec)
    return spec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--cohort', type=int, default=300)
    args = parser.parse_args()
    root, source = Path(args.experiment), Path(args.source)
    if args.prepare:
        print(json.dumps(prepare(source, root, args.cohort)), flush=True)
        return 0
    from run_pilot_comparison import verify_snapshot
    verify_snapshot(root)
    output = root/'answer_recheck'
    if output.exists():
        raise ValueError('Answer recheck output already exists; choose a new experiment.')
    spec = json.loads((root/'pilot_spec.json').read_text())
    frozen = (root/'input_manifest.json').exists()
    manifest = {r['question_id']: r for r in json.loads((root/'input_manifest.json').read_text())} if frozen else {}
    directories = {} if frozen else {json.loads((p/'query.json').read_text())['id']: p
                   for p in source.glob('shards/*/script_runs/*') if (p/'result.json').is_file()}
    if set(manifest if frozen else directories) != set(spec['video_ids']):
        raise ValueError('Recheck must use exactly the frozen development cohort.')
    if frozen:
        if file_hash(root/'evaluation_only.json') != spec['evaluation_only_sha256']:
            raise ValueError('Frozen evaluation changed')
        for row in manifest.values():
            if file_hash(root/row['input_file']) != row['input_sha256']:
                raise ValueError('Frozen answer input changed')
    cfg, artifacts = preflight(load_config(root/'config.yaml'))
    write_json(output/'artifacts.json', artifacts)
    backend, results = ModelWorker(cfg), []
    try:
        for uid in spec['video_ids']:
            if frozen:
                row = manifest[uid]
                raw_path = root/row['input_file']
                inputs = json.loads(raw_path.read_text())
            else:
                directory = directories[uid]
                calls = [json.loads(line) for line in (directory/'calls.jsonl').read_text().splitlines()]
                answer_call = next(c for c in reversed(calls) if c['operation'] == 'answer' and c.get('output_ref'))
                raw_path = directory/answer_call['output_ref']
                inputs = deepcopy(json.loads(raw_path.read_text())['inputs'])
            decision = inputs['stop_decision']
            if 'selected_option' in decision:
                decision['candidate_option'] = decision.pop('selected_option')
            run = output/digest(uid)[:20]
            ledger = BudgetLedger(600., calls_path=run/'calls.jsonl')
            meter = Meter(backend, ledger, cfg, run/'raw_calls')
            started = time.monotonic()
            result = {'question_id': uid, 'experiment_mode': 'fixed_evidence_answer_recheck',
                      'source_answer_call': str(raw_path.resolve()), 'source_sha256': file_hash(raw_path),
                      'error': None, 'prediction': None}
            try:
                answer, _ = meter.json('answer', inputs,
                    ('text', 'selected_option', 'claim_ids', 'evidence_ids'), final=True)
                answer = reconcile_answer(answer, inputs)
                result.update(prediction=answer['selected_option'], answer=answer)
            except (ToolFailure, ResourceExhausted, ValueError) as exc:
                result['error'] = str(exc)
            finally:
                meter.close()
            result['answer_stage_seconds_including_preparation'] = time.monotonic()-started
            result['cost_summary'] = ledger.summary()
            result['source_answer_seconds'] = manifest[uid].get('source_answer_seconds') if frozen else answer_call.get('actual_cost')
            result['source_evidence_status'] = inputs['stop_decision'].get('evidence_status')
            candidate = inputs['stop_decision'].get('candidate_option')
            result['evidence_status'] = ('insufficient' if candidate is not None and candidate != result['prediction']
                                         else result['source_evidence_status'])
            write_json(run/'result.json', result)
            results.append(result)
            write_json(output/'progress.json', {'completed': len(results), 'planned': len(spec['video_ids']),
                                                'errors': sum(bool(r['error']) for r in results)})
            print(f'RECHECK {len(results)}/{len(spec["video_ids"])} {uid} prediction={result["prediction"]} error={result["error"]}', flush=True)
    finally:
        backend.close()
    # Load labels only after all inference has completed.
    old = {r['question_id']: r for r in json.loads(((root/'evaluation_only.json') if frozen else (source/'comparison.json')).read_text())['rows']}
    correct = sum(r['prediction'] == old[r['question_id']]['truth'] and not r['error'] for r in results)
    report = {'mode': 'fixed_evidence_answer_recheck', 'questions': len(results), 'correct': correct,
              'errors': sum(bool(r['error']) for r in results),
              'source_correct': sum(old[r['question_id']]['SCRIPT']['correct'] for r in results),
              'fixed_wrong_answers': sum(not old[r['question_id']]['SCRIPT']['correct'] and r['prediction'] == old[r['question_id']]['truth'] for r in results),
              'lost_correct_answers': sum(old[r['question_id']]['SCRIPT']['correct'] and r['prediction'] != old[r['question_id']]['truth'] for r in results),
              'internally_reconciled_answers': sum(r.get('answer', {}).get('alignment_decision', {}).get('corrected', False) for r in results),
              'budget_violations': sum(r['cost_summary']['budget_violation'] for r in results),
              'mean_generation_seconds': statistics.mean(sum(v for k, v in r['cost_summary']['by_operation'].items()
                                                             if k in ('answer', 'repair')) for r in results),
              'mean_source_answer_seconds': statistics.mean(r['source_answer_seconds'] for r in results if r['source_answer_seconds'] is not None),
              'mean_answer_stage_seconds_including_preparation': statistics.mean(r['answer_stage_seconds_including_preparation'] for r in results),
              'limitations': spec.get('limitations', ['Uses frozen observations and captions, including their errors.',
                              'Only final decision code/prompt changes; does not measure new observation/verification behavior.',
                              'Answer-stage timing is not end-to-end SCRIPT latency.',
                              'Development questions; not held-out accuracy.'])}
    write_json(output/'summary.json', report)
    print(json.dumps(report), flush=True)
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
