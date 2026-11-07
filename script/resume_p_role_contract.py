"""Explicit P role-wire repair: preserve completed results and failed attempts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script.parallel_artifacts import tree_hashes,copy_tree_verified
from script_core import p_sentence_filter as p


def prepare(out,parent):
    from script import run_p_sentence_experiment as single,run_p_full_experiment as full
    if out.exists():raise ValueError('New directory required')
    single.verify(parent)
    if io.read(parent/'queue_status.json')['state']!='failed':raise ValueError('Expected failed parent')
    old=io.read(parent/'report.json');policy=io.read(parent/'experiment.json');before=tree_hashes(parent)
    success=[r['question_id'] for r in old['cases'] if r['complete'] and not r['error']]
    failed=[r for r in old['cases'] if r['error']]
    if len(failed)!=1 or 'Duplicate P argument role' not in failed[0]['error']['message']:
        raise ValueError('Expected duplicate-role extraction failure')
    uid=failed[0]['question_id'];attempt=parent/'runs'/uid
    failures=[]
    for f in (attempt/'sentences').glob('*/extraction.json'):
        value=io.read(f)
        if value['output'].get('parse_error'):failures.append((f,value))
    if len(failures)!=1:raise ValueError('Expected one saved malformed extraction')
    out.mkdir(parents=True)
    copies=['cohort.json','evaluation_only.json','qa_config.json','qa_expected_artifacts.json','questions','cases','runtime']
    def copy(name):
        src=parent/name;dst=out/name
        if src.is_dir():copy_tree_verified(src,dst,workers=2)
        else:shutil.copy2(src,dst)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(copy,copies))
    def copy_run(q):copy_tree_verified(parent/'runs'/q,out/'runs'/q,workers=2)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(copy_run,success))
    copy_tree_verified(attempt,out/'historical_failed_attempt'/uid,workers=4)
    whitelist=['script_core/p_sentence_filter.py','script_core/o_batch_generation.py','script_core/schemas.py',
        'script/run_p_full_experiment.py','script/p_role_contract_validation.py',
        'script/resume_p_role_contract.py','script/parallel_artifacts.py']
    for name in whitelist:shutil.copy2(ROOT/name,out/'runtime'/name)
    policy.update(version=p.VERSION,resume_parent=str(parent),resumed_success_case_ids=success,
        role_contract_validation=True,extraction_output_limit=8192,
        method_upgrade=dict(parent_version=old['version'],current_version=p.VERSION,changed='Complete unique role keys with explicit null; reject duplicate JSON keys; P extraction output reserve8192',unchanged='Canonical evidence, blind fidelity, original-question relevance, option review and final reader'),
        historical_failed_attempts=policy.get('historical_failed_attempts',[])+[dict(parent=str(attempt),processing_seconds=failed[0]['cost_summary']['total'],reason=failed[0]['error'])],
        fresh_failed_case=uid,created_at=io.now(),scope=policy['scope']+' Version subgroups separate inherited v15 results from new v16 results; no same-configuration claim.')
    policy.pop('inherited_generic_validation',None)
    io.write(out/'experiment.json',policy);io.write(out/'validation/parent_hashes.json',before)
    io.write(out/'validation/failed_extraction_input.json',failures[0][1])
    io.write(out/'validation/role_upgrade_preflight.json',dict(passed=True,successful_cases=len(success),failed_attempt_seconds=failed[0]['cost_summary']['total'],fresh_extraction_required=True,why_no_partial_extraction_reuse='Wire schema and extraction prompt changed; successful old partial outputs are retained but not inserted into new-method extraction.',whitelist=whitelist,source_hashes_before=before,at=io.now()))
    if tree_hashes(parent)!=before:raise ValueError('Parent mutated during prepare')
    io.write(out/'frozen_hashes.json',tree_hashes(out));single.verify(out);full.report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    print('P ROLE RESUME PREPARED',out,flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);ap.add_argument('--parent',type=Path,required=True)
    a=ap.parse_args();prepare(a.out.resolve(),a.parent.resolve())
