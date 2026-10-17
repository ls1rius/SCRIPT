"""Finalize existing experiment artifacts after inference and integrity audits."""
import fcntl
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timezone


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def summarize(root):
    root = Path(root)
    with (root/'summary.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        comparison, spec = read(root/'comparison.json'), read(root/'pilot_spec.json')
        audits = {}
        for i in range(spec['shards']):
            for name in ('integrity_report.json', 'integrity.json'):
                path = root/'shards'/str(i)/name
                if path.exists():
                    report = read(path)
                    # Legacy integrity.json may contain command metadata instead
                    # of an audit. A successful command is not an audit verdict.
                    if isinstance(report.get('passed'), bool):
                        audits[str(i)] = report
                        break
        complete = comparison['complete'] and len(audits) == spec['shards']
        integrity = complete and all(a['passed'] for a in audits.values())
        summary = {'updated_at': datetime.now(timezone.utc).isoformat(),
                   'status': 'complete' if integrity else 'complete_integrity_failed' if complete else 'incomplete',
                   'complete': complete, 'experiment_directory': str(root),
                   'groups': comparison['groups'], 'integrity': audits,
                   'paired_vs_videotree': comparison['paired_accuracy_bootstrap'],
                   'limitations': spec['cost_limitations']+['Fixed development cohort; multiple repairs changed together.']}
        if (root/'smoke/status.json').exists():
            summary['smoke'] = read(root/'smoke/status.json')
        if complete:
            previous = Path(spec['reuse_base_from'])
            old = read(previous/'comparison.json')
            summary['previous_experiment'] = str(previous)
            summary['previous_groups'] = old['groups']
            old_rows = {r['question_id']: r for r in old['rows']}
            summary['paired_vs_previous'] = {
                'questions': len(comparison['rows']),
                'fixed_wrong_answers': sum(not old_rows[r['question_id']]['SCRIPT']['correct'] and r['SCRIPT']['correct'] for r in comparison['rows']),
                'lost_correct_answers': sum(old_rows[r['question_id']]['SCRIPT']['correct'] and not r['SCRIPT']['correct'] for r in comparison['rows']),
                'accuracy_difference': comparison['groups']['SCRIPT']['final_accuracy']-old['groups']['SCRIPT']['final_accuracy']}
            module_path = root/'source/script/analyze_query_performance.py'
            module_spec = importlib.util.spec_from_file_location('completed_analysis', module_path)
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            write(root/'performance_diagnostics.json', module.analyze(root))
        write(root/'run_summary.json', summary)
        plan = read(root/'experiment_plan.json')
        plan.update(status=summary['status'], updated_at=summary['updated_at'])
        write(root/'experiment_plan.json', plan)
        document = plan.get('report_document')
        if document:
            write(document, dict(plan, latest_results=summary))
        return summary
