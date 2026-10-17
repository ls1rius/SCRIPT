"""Audit saved runtime contracts separately from answer accuracy and QA timing."""
import argparse
import json
from pathlib import Path
import sys


def read(path):
    return json.loads(path.read_text())


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def audit(runs):
    failures, queries = [], 0
    counters = dict(supported_claims=0, action_claims=0, unusable_frames=0,
                    skipped_visual_packages=0, rechecked_claims=0)
    for path in sorted(Path(runs).glob('*/result.json')):
        queries += 1
        directory, result = path.parent, read(path)
        query = read(directory/'query.json')
        def fail(reason, ref=None):
            failures.append({'query_id': query['id'], 'reason': reason, 'reference': ref})
        answer = result.get('answer')
        if result.get('error') or not answer:
            fail('Query has no valid final answer')
            continue
        if query.get('answer_mode') == 'choice':
            choices = {str(i): text for i, text in enumerate(query['options'])}
            if answer.get('selected_option') not in choices or answer['text'] != choices[answer['selected_option']]:
                fail('Answer text and selected option disagree')
        evidence = {r['id']: r for r in records(directory/'evidence.jsonl')}
        for item in evidence.values():
            counters['unusable_frames'] += sum(not f.get('visual_usable', True) for f in item['frame_inputs'])
        for claim in records(directory/'claims.jsonl'):
            counters['rechecked_claims'] += len(claim.get('verification_history', [])) > 1
            if claim['status'] != 'supported':
                continue
            counters['supported_claims'] += 1
            frames = {f['frame_id']: f for ref in claim['evidence_ids'] for f in evidence[ref]['frame_inputs']}
            if claim['source_type'] == 'visual':
                refs = claim.get('source_frame_ids', [])
                if not refs or any(ref not in frames or not frames[ref].get('visual_usable', True) for ref in refs):
                    fail('Supported visual claim cites unreadable or missing pixels', claim['id'])
                if claim.get('fact_type') == 'inference':
                    fail('Inferred purpose stored as a supported visual fact', claim['id'])
            if claim.get('fact_type') == 'action':
                counters['action_claims'] += 1
                verifier = claim['verification']
                if not verifier.get('process_visible') or len(set(claim.get('source_frame_ids', []))) < 3:
                    fail('Supported action lacks visible process', claim['id'])
            judgments = {v['result'] for v in claim.get('verification_history', [])}
            if {'supported', 'refuted'} <= judgments:
                fail('Conflicting same-source verification was accepted', claim['id'])
        counters['skipped_visual_packages'] += sum(r.get('failure_reason') == 'no_usable_visual_frames'
            for r in records(directory/'build_records.jsonl'))
        for call in records(directory/'calls.jsonl'):
            if call['operation'] not in ('observe', 'verify_claim', 'verify_claims') or not call.get('output_ref'):
                continue
            raw = read(directory/call['output_ref'])
            frames = raw['inputs'].get('frames', [])
            if frames and not any(f.get('visual_usable', True) for f in frames):
                fail('Model was called on a wholly unusable visual package', call['call_id'])
    return dict(queries=queries, passed=queries > 0 and not failures, counters=counters, failures=failures)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    report = audit(args.runs)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    sys.exit(0 if report['passed'] else 2)
