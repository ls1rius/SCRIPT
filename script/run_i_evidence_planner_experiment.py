"""L: migrate K's independent evidence planner onto the frozen I/H memory."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import controlled_memory_io as io
import run_evidence_planner_experiment as engine

ARM = 'L_evidence_planner'
ARMS = ('I_control', 'L_all_evidence', ARM)
PARENT_ARM = 'I_merged_lazy_script'


def prepare(parent, out, tokenizer=None):
    parent, out = parent.resolve(), out.resolve()
    if out.exists():
        raise ValueError('Use a fresh L output directory')
    engine.verify(parent)
    policy = io.read(parent / 'experiment.json')
    report = io.read(parent / 'report.json')
    core = engine.module('i_evidence_planner')
    merged = engine.module('merged_lazy_memory')
    if (policy.get('arm') != PARENT_ARM or policy.get('parent_memory_variant') != 'H'
            or policy.get('version') != merged.VERSION or policy.get('render_version') != merged.RENDER_VERSION):
        raise ValueError('L requires current reversible H-derived I, never legacy F-derived I')
    if (not report['complete'] or io.read(parent / 'queue_status.json')['state'] != 'complete'
            or not io.read(parent / 'validation/input_contract.json')['passed']):
        raise ValueError('I must complete and pass its source/input contract before L')
    h = Path(policy['parent_H_run'])
    engine.verify(h)
    if not io.read(h / 'validation/functional.json')['passed']:
        raise ValueError('I source H real-model functional gate must pass')
    ids, cfg = io.read(parent / 'cohort.json'), io.read(parent / 'qa_config.json')
    if not ids or len(ids) != len(set(ids)) or len(ids) != policy['planned']:
        raise ValueError('Incomplete or duplicate I cohort')
    if cfg['models']['generation']['do_sample'] or cfg['models']['precision'] != 'bfloat16':
        raise ValueError('L requires the original bf16 greedy I model protocol')
    began = time.monotonic()
    prepared, bindings = {}, {}
    for uid in ids:
        seal_path = parent / 'input_seals' / (uid + '.json')
        seal = io.read(seal_path)
        folder = parent / 'materialized' / uid
        for name, expected in seal['storage'].items():
            if io.sha(folder / name) != expected:
                raise ValueError('I materialization seal changed: ' + uid + '/' + name)
        path = parent / 'final_inputs' / PARENT_ARM / (uid + '.json')
        result_path = parent / 'runs' / PARENT_ARM / uid / 'result.json'
        result = io.read(result_path)
        # I's one historical answer-format failure remains in the cohort. L
        # regenerates every control answer; it never filters by parent correctness.
        if io.sha(path) != seal['inputs'][PARENT_ARM] or result.get('input_sha256') != io.sha(path):
            raise ValueError('I answer/input binding failed: ' + uid)
        original, memory = io.read(path), io.read(folder / 'memory.json')
        description, source_audit = merged.render(memory)
        stored_audit = io.read(folder / 'audit.json')
        if any(stored_audit.get(k) != v for k,v in source_audit.items()) or description != io.parts(original)[1]:
            raise ValueError('I render/source audit differs from its frozen input: ' + uid)
        question = io.read(parent / 'packets' / (uid + '.json'))['packet']['question']
        question = {k:question[k] for k in ('question_id','video_id','question','options')}
        if question['question_id'] != uid:
            raise ValueError('I question identity mismatch')
        engine.strict_answer_parser(question['options'])
        pool = core.build_pool(memory, original, io.parts)
        all_text, audit = core.render(pool, core.keep_all(pool))
        prepared[uid] = dict(question=question, pool=pool, I_control=deepcopy(original),
            L_all_evidence=core.final_input(original, all_text, io.parts),
            planner_input=core.planner_input(question, pool, uid), all_audit=audit)
        bindings[uid] = dict(input_sha256=io.sha(path), memory_sha256=io.sha(folder / 'memory.json'),
            source_seal_sha256=io.sha(seal_path), result_sha256=io.sha(result_path))
    if tokenizer is None:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(cfg['models']['vlm'], local_files_only=True)
    preflight = {}
    for uid,item in prepared.items():
        counts = {name:engine.token_count(tokenizer,item[name]) for name in ('I_control','L_all_evidence','planner_input')}
        if max(counts.values()) > cfg['models']['max_input_tokens']:
            raise ValueError('L context overflow; no truncation: '+uid+' '+str(counts))
        refs = [r['id'] for r in item['pool']['records']]
        values = ['keep','skip','uncertain'] + ['group:'+r for r in refs]
        longest = max(len(tokenizer.encode(json.dumps(v))) for v in values)
        bound = sum(len(tokenizer.encode(json.dumps(r))) + longest + 4 for r in refs) + 128
        if bound > engine.PLANNER_MAX_NEW_TOKENS:
            raise ValueError('L planning output budget insufficient: '+uid)
        preflight[uid] = dict(**counts, planner_output_token_bound=bound)
    out.mkdir(parents=True)
    for name in ('cohort.json','evaluation_only.json','qa_config.json','qa_expected_artifacts.json'):
        shutil.copy2(parent/name,out/name)
    control = out/'control';control.mkdir()
    for name in ('run_i_evidence_planner_experiment.py','run_evidence_planner_experiment.py','controlled_memory_io.py','gpu_queue.py'):
        shutil.copy2(Path(__file__).parent/name,control/name)
    for name in ('i_evidence_planner','evidence_planner','merged_lazy_memory','qa_evidence_view','g_rl'):
        shutil.copy2(Path(engine.module(name).__file__),control/(name+'.py'))
    runtime=out/'runtime';runtime.mkdir()
    for name in ('model.py','prompts.py'):
        shutil.copy2(parent/'runtime/qa'/name,runtime/name)
    for uid,item in prepared.items():
        for name,value in item.items():io.write(out/name/(uid+'.json'),value)
        io.write(out/'baseline_results'/(uid+'.json'),io.read(parent/'runs'/PARENT_ARM/uid/'result.json'))
    h_report = io.read(h/'report.json')
    io.write(out/'experiment.json',dict(created_at=io.now(),experiment_label='L',arm=ARM,arms=list(ARMS),
        version=core.VERSION,planner_core='i_evidence_planner',parent_I=str(parent),parent_H=str(h),
        parent_memory_variant='I',underlying_memory_variant='H',planned=len(ids),
        parent_manifest_sha256=io.sha(parent/'frozen_hashes.json'),bindings=bindings,
        primary_comparison=ARM+'_vs_L_all_evidence',secondary_comparison=ARM+'_vs_I_control',
        after_queue=None,model=cfg['models']['vlm'],training=False,adapter=None,
        new_extraction_calls=0,new_retrieval=False,answers_reused=False,
        selected_input_filename='L_input.json',reader_protocol=engine.READER_PROTOCOL,
        final_answer_parser=engine.READER_PROTOCOL,plan_decoder=engine.PLANNER_DECODER,
        planner_max_new_tokens=engine.PLANNER_MAX_NEW_TOKENS,
        inherited_construction_seconds=report['inherited_construction_seconds']+report['projection']['materialization_cpu_seconds'],
        inherited_prior_development_seconds=h_report.get('prior_development_seconds',0.),
        inherited_H_construction_seconds=report['inherited_construction_seconds'],
        inherited_I_merge_cpu_seconds=report['projection']['materialization_cpu_seconds'],
        parent_answer_errors_retained=report['arms'][PARENT_ARM]['errors'],
        cpu_preparation_seconds=time.monotonic()-began,prior_preparation_seconds=0.,prior_runtime_seconds=0.,
        protocol_policy='Three fresh answers per question under the same constrained letter protocol; original I control input is byte-identical.',
        evidence_policy='I/H open states, actorless actions, raw clauses, all derived facts, temporal and visual evidence; original samples and I groups archived.',
        merge_policy='Compact exactly adjacent samples of identical statements; never bridge gaps or infer identities, continuity or counts.',
        failure_policy='Stop on gate, input, plan, generation or runtime failure; no automatic retries or semantic answer guessing.',
        resource_policy=dict(preference=['0','1','2','3'],minimum_free_mib=32768,maximum_utilization=100,
            stable_observations=3,selection='most_free_memory')))
    io.write(out/'validation/cpu_input_preflight.json',dict(passed=True,model_loaded=False,
        benchmark_labels_used=False,questions=len(ids),input_tokens=preflight,
        maximum_input_tokens=max(max(c[k] for k in ('I_control','L_all_evidence','planner_input')) for c in preflight.values()),
        total_candidate_records=sum(len(i['pool']['records']) for i in prepared.values()),
        original_I_inputs_unchanged=True,parent_format_failures_not_filtered=True))
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p) for p in out.rglob('*') if p.is_file()})
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    engine.report(out)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--parent',type=Path)
    args=parser.parse_args();out=args.out.resolve()
    if args.mode=='prepare':
        if args.parent is None:parser.error('prepare requires --parent completed I')
        prepare(args.parent,out)
    elif args.mode=='queue':return engine.queue(out)
    elif args.mode=='worker':return engine.worker(out)
    else:print(json.dumps(engine.report(out),ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
