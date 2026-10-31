"""Materialize and evaluate D from the frozen C evidence, without new extraction."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
import controlled_memory_io as io

ARMS = ('D_state_segments',)


def verify(out):
    for name,expected in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name)!=expected:
            raise ValueError('Frozen D artifact changed: '+name)


def project(original, semantic, max_gap_seconds=2):
    from state_segment_memory import materialize, render
    if io.fixed(original)!=io.fixed(semantic):
        raise ValueError('C evidence uses a different fixed QA contract')
    start,body,end=io.parts(semantic)
    memory=materialize(json.loads(body),max_gap_seconds)
    description,audit=render(memory)
    result=deepcopy(original)
    result['prompts'][0]=start+description+end
    if io.fixed(result)!=io.fixed(original):
        raise ValueError('D changed a field outside Description')
    return result,memory,audit


def prepare_ready(out):
    experiment=io.read(out/'experiment.json')
    parent=Path(experiment['normalization_parent'])
    for uid in io.read(out/'cohort.json'):
        target=out/'final_inputs/D_state_segments'/f'{uid}.json'
        if target.exists():
            continue
        source=parent/'final_inputs/C_semantic'/f'{uid}.json'
        seal=parent/'materialized'/uid/'sealed.json'
        if not source.exists() or not seal.exists():
            continue
        began=time.process_time()
        for name,expected in io.read(seal).items():
            if io.sha(seal.parent/name)!=expected:
                raise ValueError('Parent extraction seal changed: '+uid+'/'+name)
        original=io.read(out/'final_inputs/A_original'/f'{uid}.json')
        semantic=io.read(source)
        result,memory,audit=project(original,semantic,experiment['max_gap_seconds'])
        folder=out/'materialized'/uid
        io.write(out/'source_inputs/C_semantic'/f'{uid}.json',semantic)
        io.write(folder/'memory.json',memory)
        io.write(folder/'audit.json',dict(audit,materialization_cpu_seconds=time.process_time()-began))
        io.write(target,result)
        cost=sum(float(json.loads(line).get('actual_cost') or 0)
            for line in (seal.parent/'calls.jsonl').read_text().splitlines())
        io.write(out/'input_seals'/f'{uid}.json',dict(
            parent_input=str(source),parent_input_sha256=io.sha(source),
            parent_memory_seal=str(seal),parent_memory_sha256=io.sha(seal),
            inherited_extraction_seconds=cost,
            storage={name:io.sha(folder/name) for name in ['memory.json','audit.json']},
            inputs={'D_state_segments':io.sha(target)},updated_at=io.now()))


def next_range(out):
    ids=io.read(out/'cohort.json')
    for start,uid in enumerate(ids):
        if (out/'runs/D_state_segments'/uid/'result.json').exists():
            continue
        if not (out/'input_seals'/f'{uid}.json').exists():
            continue
        end=start+1
        while end<min(start+40,len(ids)) and (out/'input_seals'/f'{ids[end]}.json').exists():
            end+=1
        return start,end
    return None


def worker(out,start,end,phase):
    import compact_protocol as protocol
    protocol.io.ARMS=ARMS
    protocol.io.ORDERS=(ARMS,)*6
    for uid in io.read(out/'cohort.json')[start:end]:
        seal=io.read(out/'input_seals'/f'{uid}.json')
        for arm,expected in seal['inputs'].items():
            if io.sha(out/'final_inputs'/arm/f'{uid}.json')!=expected:
                raise ValueError('D input seal changed')
        for name,expected in seal['storage'].items():
            if io.sha(out/'materialized'/uid/name)!=expected:
                raise ValueError('D segment storage seal changed')
    return protocol.qa_worker(out,start,end,phase)


def stats(rows,truth,ids,planned):
    right=sum(not rows[u].get('error') and str(rows[u]['selected_option'])==str(truth[u]) for u in ids)
    return dict(completed=len(ids),planned=planned,pending=planned-len(ids),correct=right,
        errors=sum(bool(rows[u].get('error')) for u in ids),
        accuracy=right/planned if len(ids)==planned else None,
        accuracy_among_completed=right/len(ids) if ids else None,
        mean_input_tokens=statistics.mean(rows[u]['input_tokens'] for u in ids
            if rows[u].get('input_tokens') is not None) if any(rows[u].get('input_tokens') is not None for u in ids) else None,
        mean_qa_seconds=statistics.mean(rows[u]['seconds'] for u in ids) if ids else None)


def report(out):
    ids=io.read(out/'cohort.json');truth=io.read(out/'evaluation_only.json')
    experiment=io.read(out/'experiment.json');reference=Path(experiment['reference_compact_run'])
    roots={arm:(out if arm=='D_state_segments' else reference) for arm in
        ['A_original','B_compact_text','C_compact_text','D_state_segments']}
    rows={arm:{u:io.read(path) for u in ids
        if (path:=root/'runs'/arm/u/'result.json').exists()} for arm,root in roots.items()}
    common=[u for u in ids if all(u in results for results in rows.values())]
    correct=lambda arm,u:not rows[arm][u].get('error') and str(rows[arm][u]['selected_option'])==str(truth[u])
    comparisons={}
    for arm in ['A_original','B_compact_text','C_compact_text']:
        pair=[u for u in ids if u in rows[arm] and u in rows['D_state_segments']]
        gained=[u for u in pair if correct('D_state_segments',u) and not correct(arm,u)]
        lost=[u for u in pair if correct(arm,u) and not correct('D_state_segments',u)]
        comparisons['D_vs_'+arm]=dict(paired=len(pair),corrected=gained,regressed=lost,
            delta_percentage_points=100*(len(gained)-len(lost))/len(pair) if pair else None)
    audits=[io.read(path) for u in ids if (path:=out/'materialized'/u/'audit.json').exists()]
    projection={key:sum(a[key] for a in audits) for key in ['source_rows','unique_records','unique_facts',
        'rendered_segments','persistent_segments','inferred_persistent_segments','materialization_cpu_seconds']}
    new_cost=sum(float(json.loads(line).get('actual_cost') or 0)
        for stage in ['runs','setup'] for f in (out/stage).rglob('calls.jsonl') for line in f.read_text().splitlines())
    d_rows=rows['D_state_segments']
    result=dict(updated_at=io.now(),planned=len(ids),complete=len(d_rows)==len(ids),
        arms={'D_state_segments':stats(d_rows,truth,list(d_rows),len(ids))},
        common_completed=len(common),paired_common={a:stats(rs,truth,common,len(ids)) for a,rs in rows.items()},
        comparisons=comparisons,prepared_inputs=len(audits),projection=projection,
        new_qa_and_setup_seconds=new_cost,
        inherited_extraction_seconds_completed_D=sum(io.read(out/'input_seals'/f'{u}.json')['inherited_extraction_seconds'] for u in d_rows),
        normalization_model_calls=0,reference_answers_reused_for_comparison_only=True,development_cohort=True)
    io.write(out/'report.json',result)
    return result


def prepare(reference,out,max_gap_seconds=2):
    if out.exists():
        raise ValueError('Use a new D experiment directory')
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'script_core'))
    for name,expected in io.read(reference/'frozen_hashes.json').items():
        if io.sha(reference/name)!=expected:
            raise ValueError('Reference experiment artifact changed: '+name)
    if not io.read(reference/'validation/baseline_reproduction.json')['passed']:
        raise ValueError('Reference baseline reproduction has not passed')
    parent=Path(io.read(reference/'experiment.json')['parent'])
    out.mkdir(parents=True)
    for name in ['cohort.json','evaluation_only.json','historical_only.json','qa_config.json','qa_expected_artifacts.json']:
        shutil.copy2(reference/name,out/name)
    shutil.copytree(reference/'runtime/qa',out/'runtime/qa',symlinks=True,ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(reference/'packets',out/'packets')
    shutil.copytree(reference/'final_inputs/A_original',out/'final_inputs/A_original')
    (out/'control').mkdir()
    for src,name in [(Path(__file__),Path(__file__).name),
        (root/'script_core/state_segment_memory.py','state_segment_memory.py'),
        (reference/'control/qa_evidence_view.py','qa_evidence_view.py'),
        (reference/'control/compact_protocol.py','compact_protocol.py'),
        (reference/'control/controlled_memory_io.py','controlled_memory_io.py')]:
        shutil.copy2(src,out/'control'/name)
    io.write(out/'experiment.json',dict(created_at=io.now(),planned=len(io.read(out/'cohort.json')),
        arms=list(ARMS),normalization_parent=str(parent),reference_compact_run=str(reference),
        max_gap_seconds=max_gap_seconds,policy='D extends C with materialized state segments and factored repeated assertions.',
        continuity='Matching explicit state/property assertions within bounded gaps; inferred persistence and literal identity assumption retained in storage.',
        actions='Repeated action samples retain discrete intervals; no continuous event duration or event count inferred.',
        fixed='Same C evidence, original system/questions/options/old memory/visuals/generation/parser; only Description changes.',
        runtime='Shared resident final-QA backend; fresh D answers only; no new semantic extraction.',
        failures='No retries, hidden truncation or answer fallback. Errors retained and counted wrong.'))
    io.write(out/'validation/baseline_reproduction.json',dict(passed=True,
        inherited_from=str(reference/'validation/baseline_reproduction.json'),
        source_sha256=io.sha(reference/'validation/baseline_reproduction.json'),
        policy='D reuses the already verified model/runtime/protocol; no extra A inference.'))
    prepare_ready(out)
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p)
        for p in out.rglob('*') if p.is_file() and not p.is_symlink()})
    verify(out)
    report(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    print(json.dumps(dict(out=str(out),prepared_inputs=len(list((out/'input_seals').glob('*.json'))),planned=300),ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','report','worker'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--reference',type=Path)
    parser.add_argument('--max-gap-seconds',type=float,default=2)
    parser.add_argument('--start',type=int,default=0)
    parser.add_argument('--end',type=int,default=40)
    parser.add_argument('--phase',default='D_state_segments')
    args=parser.parse_args();out=args.out.resolve()
    if args.mode=='prepare':prepare(args.reference.resolve(),out,args.max_gap_seconds)
    elif args.mode=='report':print(json.dumps(report(out),ensure_ascii=False,indent=2))
    else:raise SystemExit(worker(out,args.start,args.end,args.phase))
