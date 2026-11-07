"""Run N on the original full cohort with fresh navigation and separate artifacts."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script import run_m_scene_experiment as common


def audit_variant(folder, *, empty=False):
    """Check the actual N path, including extraction, cache and source-closure logs."""
    from script_core.n_scene_memory import VERSION
    result=io.read(folder/'partial_temporal/result.json')
    calls=common.leaf_calls(folder)
    ops=[c['operation'] for c in calls]
    if result.get('memory_variant')!='N' or result.get('method')!=VERSION:
        raise ValueError('N gate ran another memory variant')
    if 'n_answer' not in ops or set(ops)&{'m_answer','m_evidence_plan','h_extract_fields','h_extract_batch'}:
        raise ValueError('N calls were not isolated from M/H execution')
    target=folder/'partial_temporal/N'
    if not (target/'final_input.json').is_file():
        raise ValueError('N final input missing')
    if empty:
        if any(op.startswith('n_extract') or op=='n_evidence_plan' for op in ops):
            raise ValueError('Empty N candidates triggered extraction/planning')
    else:
        if not set(ops)&{'n_extract_fields','n_extract_batch'} or 'n_evidence_plan' not in ops:
            raise ValueError('N extraction/planning was not exercised')
        evidence=result['qa_evidence']
        if '/N/I/h_script.sqlite3' not in evidence['database']:
            raise ValueError('N cache namespace missing')
        if 'N-required-role-review-v2' not in evidence['extractor_version']:
            raise ValueError('Wrong N source extractor')
        for name in ('proposed_plan.json','plan.json','source_preservation.json','audit.json'):
            if not (target/name).is_file():
                raise ValueError('Missing N source-preservation audit: '+name)
    return dict(passed=True,method=result['method'],operations=sorted(set(ops)),empty=empty)


def functional(out, backend, cfg):
    if cfg['query']['temporal_qa_evidence']!='role_bound_scene_script':
        raise ValueError('N requires its dedicated mode')
    gate=common.functional(out,backend,cfg)
    try:
        checks=[dict(case=name,**audit_variant(out/'functional'/name,empty=name=='empty'))
                for name in ('single','comparison','empty')]
        gate['N_variant_checks']=checks
    except Exception as exc:
        gate['passed']=False
        gate['failures'].append(dict(case='N_variant_contract',error=str(exc)))
        raise
    finally:
        gate['updated_at']=io.now()
        io.write(out/'validation/functional.json',gate)
    return gate


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--source-run',type=Path)
    parser.add_argument('--selection-validation',type=Path)
    args=parser.parse_args();out=args.out.resolve()
    if args.mode=='prepare':
        if args.source_run is None or args.selection_validation is None:
            parser.error('prepare requires --source-run and --selection-validation')
        common.prepare_revision(args.source_run.resolve(),out,args.selection_validation.resolve(),variant='N')
    elif args.mode=='queue':
        common.queue(out)
    elif args.mode=='worker':
        common.worker(out,functional_gate=functional)
    else:
        print(json.dumps(common.report(out),ensure_ascii=False))


if __name__=='__main__':main()
