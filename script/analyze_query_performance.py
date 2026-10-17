"""Offline diagnostics of completed paired runs; never used by inference."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


def analyze(root):
    root = Path(root)
    comparison = json.loads((root/'comparison.json').read_text())
    rows = {r['question_id']: r for r in comparison['rows']}
    operations, stops, predictions = defaultdict(list), defaultdict(list), defaultdict(Counter)
    collisions, absence_refutations, gate_choices = [], [], Counter()
    round_counts = Counter()
    for directory in sorted(root.glob('shards/*/script_runs/*')):
        if not (directory/'result.json').is_file():
            continue
        query = json.loads((directory/'query.json').read_text())
        row = rows[query['id']]
        result = json.loads((directory/'result.json').read_text())
        stop = (result.get('stop_decision') or {}).get('reason', 'error')
        stops[stop].append(row['SCRIPT']['correct'])
        predictions[stop][row['SCRIPT']['prediction']] += 1
        steps = directory/'steps.jsonl'
        round_counts[len(steps.read_text().splitlines()) if steps.exists() else 0] += 1
        for line in (directory/'calls.jsonl').read_text().splitlines():
            call = json.loads(line)
            operations[call['operation']].append(call['actual_cost'])
            if call['operation'] not in ('observe', 'gate') or not call.get('output_ref'):
                continue
            raw = json.loads((directory/call['output_ref']).read_text())
            data = (raw.get('output') or {}).get('data')
            if not isinstance(data, dict):
                continue
            if call['operation'] == 'observe':
                grouped = defaultdict(set)
                for claim in data.get('claims', []):
                    if claim.get('predicate') == 'other':
                        key = json.dumps([claim.get('roles'), claim.get('interval'), sorted(claim.get('source_frame_ids', []))])
                        grouped[key].add(claim.get('description', '').strip().casefold())
                for descriptions in grouped.values():
                    if len(descriptions) > 1:
                        collisions.append({'question_id': query['id'], 'descriptions': sorted(descriptions)})
            else:
                gate_choices[data.get('selected_option')] += 1
                checks = data.get('option_checks', [])
                if isinstance(checks, dict):
                    checks = [dict(check, option_id=key) for key, check in checks.items()]
                for check in checks:
                    if check['result'] == 'refuted' and 'no evidence' in check['reason'].lower():
                        absence_refutations.append({'question_id': query['id'], **check})
    completed = sum(map(len, stops.values()))
    return {'completed_questions': completed, 'comparison': comparison['groups'],
        'stop_groups': {k: {'questions': len(v), 'correct': sum(v), 'accuracy': statistics.mean(v),
                            'predictions': dict(predictions[k])} for k, v in stops.items()},
        'round_distribution': dict(round_counts), 'all_gate_predictions': dict(gate_choices),
        'operations': {k: {'calls': len(v), 'mean_seconds_per_question': sum(v)/max(1, completed),
                            'median_seconds_per_call': statistics.median(v)}
                       for k, v in sorted(operations.items(), key=lambda item: -sum(item[1]))},
        'same_source_distinct_description_groups': len(collisions),
        'same_source_fact_collision_examples': collisions[:10],
        'refutations_explicitly_using_no_evidence': len(absence_refutations),
        'absence_refutation_examples': absence_refutations[:10],
        'interpretation': [
            'Stop-group accuracy is descriptive; groups were not randomized.',
            'In V5, exact duplicate matching ignored other-claim descriptions. Collision examples demonstrate the affected inputs.',
            'Repeated verification and assessment time is included in the unchanged online budget.',
            'Labels are used only by this offline diagnostic, never by query inference.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('run')
    args = parser.parse_args()
    report = analyze(args.run)
    output = Path(args.run)/'performance_diagnostics.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: report[k] for k in ('completed_questions', 'stop_groups',
        'round_distribution', 'same_source_distinct_description_groups',
        'refutations_explicitly_using_no_evidence')}, ensure_ascii=False, indent=2))
