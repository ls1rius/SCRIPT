"""Read-only diagnosis; never replace the frozen experiment's primary scores."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.run_memory_quotes_comparison import strict_prediction

ARMS = ('original_replay', 'complete_with_quotes', 'complete_structured')


def read(path):
    return json.loads(path.read_text())


def canonical(text):
    # Ignore formatting only. Do not use word overlap, semantics or ground truth
    # to decide which option a malformed model response intended.
    return re.sub(r'\s+', ' ', text.replace('**', '').replace('`', '')).strip().casefold().rstrip('.!?')


def audited_option(text, options):
    try:
        return dict(option=strict_prediction(text), method='strict_letter')
    except ValueError:
        pass
    lines = re.findall(r'(?im)^\s*prediction\s*:\s*(.*?)\s*$', text)
    if len(lines) == 1:
        matches = [str(i) for i, option in enumerate(options) if canonical(option) == canonical(lines[0])]
        if len(matches) == 1:
            return dict(option=matches[0], method='unique_exact_option_text')
    return dict(option=None, method='unresolved_format')


def analyze(source, out):
    source = source.resolve()
    if out.exists(): raise ValueError('Use a fresh audit directory')
    out.mkdir(parents=True)
    snapshot = read(source/'current_report.json')
    ids = [u for u in read(source/'cohort.json') if all(
        (source/'runs'/a/u/'result.json').exists() for a in ARMS)]
    truth = read(source/'evaluation_only.json')
    results = {}; cases = []; kinds = Counter(); residual_kinds = Counter()
    sources = retained = facts_count = null_objects = 0
    suspicious = []; field_sizes = Counter(); color_units = 0
    for uid in ids:
        row = read(source/'inputs'/f'{uid}.json'); options = row['packet']['question']['options']
        rs = {a:read(source/'runs'/a/uid/'result.json') for a in ARMS}
        for a, result in rs.items():
            result['option_text_audit'] = audited_option(result['raw_response'], options)
        results[uid] = rs
        packed = read(source/'materialized'/uid/'packed.json')
        memory = read(source/'materialized'/uid/'memory.json')
        audit = read(source/'materialized'/uid/'audit.json')
        bundle = read(source/'source_bundles'/f'{uid}.json')
        units = {u['id']:u for u in bundle['units']}
        kinds.update(audit['facts_by_type']); residual_kinds.update(audit['retained_by_type'])
        sources += audit['source_units']; retained += audit['retained_units']
        for k, value in packed.items():
            field_sizes[k] += len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        color_units += sum(bool(re.search(r'\b(red|blue|green|yellow|black|white|brown|pink|purple|orange|grey|gray)\b',u['text'],re.I)) for u in units.values())
        for group in ('actions','attributes','states','relations'):
            for fact in memory[group]:
                facts_count += 1; null_objects += fact['object'] is None
                if fact['object'] and re.match(r'^(with|using|on|in|from|into|onto|around|of|towards|toward)\b',fact['object'],re.I):
                    suspicious.append(dict(question_id=uid, fact=fact,
                        source_texts=[units[u]['text'] for u in fact['evidence_ids']]))
        baseline = rs['original_replay']; structured = rs['complete_structured']
        if str(baseline['selected_option']) == str(truth[uid]) and str(structured['selected_option']) != str(truth[uid]):
            cases.append(dict(question_id=uid, question=row['packet']['question'], ground_truth=truth[uid],
                responses={a:{k:r.get(k) for k in ('selected_option','raw_response','input_tokens','strict_audit_option','option_text_audit')}
                    for a,r in rs.items()},
                source_bundle=str(source/'source_bundles'/f'{uid}.json'),
                complete_memory=str(source/'materialized'/uid/'memory.json')))
    summaries = {}
    for arm in ARMS:
        rs = [results[u][arm] for u in ids]
        correct = lambda result,uid,key: str(result[key]) == str(truth[uid])
        primary = sum(correct(r,u,'selected_option') for u,r in zip(ids,rs))
        strict = sum(correct(r,u,'strict_audit_option') for u,r in zip(ids,rs))
        exact = sum(str(r['option_text_audit']['option']) == str(truth[u]) for u,r in zip(ids,rs))
        changes = [dict(question_id=u, legacy=r['selected_option'], audit=r['option_text_audit'],
                       first_line=r['raw_response'].splitlines()[0]) for u,r in zip(ids,rs)
                   if r['option_text_audit']['option'] is not None and str(r['selected_option']) != r['option_text_audit']['option']]
        summaries[arm] = dict(completed=len(ids), legacy_correct=primary, legacy_accuracy=primary/len(ids),
            strict_correct=strict, strict_format_failures=sum(r['strict_audit_option'] is None for r in rs),
            unique_option_text_audit_correct=exact, unique_option_text_audit_accuracy=exact/len(ids),
            unresolved_format=sum(r['option_text_audit']['option'] is None for r in rs),
            parsing_changes=changes, mean_input_tokens=statistics.mean(r['input_tokens'] for r in rs))
    report = dict(source=str(source), source_report_timestamp=snapshot['updated_at'], cohort=ids,
        scoring_policy='Frozen primary scores unchanged. Sidecar audit accepts strict letters or a unique exact full option text; unresolved format counts incorrect. No semantic model matching or ground truth in mapping.',
        arms=summaries, memory=dict(source_units=sources, facts=facts_count, facts_by_type=dict(kinds),
            null_object_facts=null_objects, retained_units=retained, retained_by_type=dict(residual_kinds),
            units_with_literal_color_words=color_units, packed_json_characters_by_field=dict(field_sizes)),
        role_warnings=suspicious, legacy_regressions=cases,
        interpretation_limits=['Preposition-leading objects are a syntactic warning, not an exhaustive semantic error count.',
            'Zero property facts does not imply absence of properties; residuals may contain them.',
            'Prompt length/layout and source weighting are plausible mechanisms, not causally isolated results.',
            'Coverage accounts for source IDs, not correctness of bindings or faithful reconstruction.'])
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(directory=str(out),completed=len(ids),arms=summaries,
        facts_by_type=dict(kinds),retained_by_type=dict(residual_kinds),role_warnings=len(suspicious)),ensure_ascii=False,indent=2))
    return report


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=p.parse_args();analyze(args.source,args.out)
