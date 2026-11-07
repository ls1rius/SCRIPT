"""Full 300-case O/O+ evaluation on incrementally frozen completed N evidence."""
import argparse
import os
from pathlib import Path
import shutil
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from script import controlled_memory_io as io
from script_core import o_sentence_filter as core


def verify_case(target):
    for name,sha in io.read(target/'source_hashes.json').items():
        if io.sha(target/name)!=sha:raise ValueError('Frozen O source changed: '+str(target/name))


def prepare_case(out,uid):
    """Copy a completed source atomically. No outcomes are used for selection."""
    policy=io.read(out/'experiment.json')
    if uid not in policy['case_ids']:raise ValueError('Question outside fixed cohort')
    parent=Path(policy['parent_N']);src=parent/'runs'/uid;target=out/'cases'/uid
    if target.exists():verify_case(target);return True
    if not (src/'result.json').exists():return False
    result=io.read(src/'result.json')
    if result.get('error'):raise ValueError('N source case failed: '+uid)
    if not result.get('answer'):return False
    if list(src.rglob('*.pending.json')):return False
    files={str(p.relative_to(src)):io.sha(p) for p in src.rglob('*') if p.is_file()}
    stage=out/'source_staging'/uid
    if stage.exists():raise ValueError('Incomplete source staging retained; no automatic retry')
    stage.mkdir(parents=True)
    shutil.copytree(src,stage/'parent_N')
    question=io.read(out/'questions'/f'{uid}.json')
    if io.read(parent/'inputs'/f'{uid}.json')['question']!=question:
        raise ValueError('Parent question changed')
    bundle=io.read(stage/'parent_N/partial_search/evidence_bundle.json')
    semantic=stage/'parent_N/partial_temporal/semantic_sources.json'
    if semantic.exists():
        source=io.read(semantic);sentences=core.sentence_sources(source['units']);empty=False
    else:
        marker=stage/'parent_N/partial_temporal/N/empty_evidence.json'
        if not marker.exists():raise ValueError('Missing source evidence without explicit empty marker')
        empty_info=io.read(marker)
        if empty_info.get('observed_sources')!=[] or bundle.get('opened_scenes'):
            raise ValueError('Invalid empty-evidence source')
        sentences=[];empty=True
    io.write(stage/'question.json',question)
    io.write(stage/'original_qa.json',bundle['qa_input'])
    io.write(stage/'sentences.json',sentences)
    io.write(stage/'extraction_policy.json',dict(input='source sentence only',question_field_plan_used=False,
        available_fields=list(core.fields_core.FIELDS)))
    io.write(stage/'source_audit.json',dict(parent=str(src),new_navigation=False,new_extraction=True,
        sentence_count=len(sentences),opened_scenes=bundle.get('opened_scenes',[]),
        selected_seconds=bundle.get('selected_seconds',[]),explicit_empty_evidence=empty,
        historical_navigation_preserved=True,source_files=files,prepared_at=io.now(),
        upstream_processing_seconds=result.get('cost_summary',{}).get('total'),
        scope='N selected source text only; no new video reads; no reuse of N extracted facts or answers.'))
    after={str(p.relative_to(src)):io.sha(p) for p in src.rglob('*') if p.is_file()}
    if files!=after:raise ValueError('N source changed while being frozen')
    if any(io.sha(stage/'parent_N'/name)!=sha for name,sha in files.items()):
        raise ValueError('Source copy mismatch')
    io.write(stage/'source_hashes.json',{str(p.relative_to(stage)):io.sha(p) for p in stage.rglob('*') if p.is_file()})
    target.parent.mkdir(parents=True,exist_ok=True);stage.rename(target)
    verify_case(target)
    return True


def prepare(out,parent,diagnostic):
    from script.run_m_scene_experiment import verify as verify_parent
    from script.run_o_sentence_experiment import verify as verify_o
    verify_parent(parent);verify_o(diagnostic)
    if not io.read(diagnostic/'report.json')['complete']:raise ValueError('Need completed O diagnostic archive')
    for name in ['o_sentence_filter.py','o_generation.py','o_batch_generation.py','m_generation.py']:
        if io.sha(ROOT/'script_core'/name)!=io.sha(diagnostic/'runtime/script_core'/name):
            raise ValueError('Full-run semantic/decoder core differs from tested diagnostic: '+name)
    if io.read(parent/'qa_config.json')!=io.read(diagnostic/'qa_config.json'):
        raise ValueError('Full-run model/budget configuration differs from tested diagnostic')
    if out.exists():raise ValueError('Use a new full O/O+ directory')
    ids=io.read(parent/'cohort.json')
    if len(ids)!=300 or len(set(ids))!=300:raise ValueError('Expected original 300-case cohort')
    out.mkdir(parents=True)
    for name in ['qa_config.json','qa_expected_artifacts.json','evaluation_only.json','cohort.json']:
        shutil.copy2(parent/name,out/name)
    cfg=io.read(out/'qa_config.json')
    if cfg['query']['tool_transient_retries'] or cfg['query']['json_repair_retries']:
        raise ValueError('Full O/O+ requires no hidden retries')
    parent_questions={}
    for uid in ids:
        raw=parent/'inputs'/f'{uid}.json';question=io.read(raw)['question']
        if question['question_id']!=uid:raise ValueError('Source question identity mismatch')
        io.write(out/'questions'/f'{uid}.json',question);parent_questions[str(raw)]=io.sha(raw)
    io.write(out/'experiment.json',dict(created_at=io.now(),planned=300,case_ids=ids,
        variant=['O','O+'],conditions=['question_only'],full_cohort=True,
        composition_context_eval=True,independent_sentence_batch_size=4,
        parent_N=str(parent),case_budget_seconds=3600,version=core.VERSION,plus_version=core.PLUS_VERSION,
        scope='Full 300-question cohort; O/O+ question-only selection/composition on frozen completed N evidence; not an independent navigation evaluation.',
        new_navigation=False,new_extraction=True,answers_reused=False,training=False,adapter=None,
        final_answer_options='all',upstream_source_policy='Freeze each completed N case in original cohort order; wait for unfinished cases, stop if upstream fails.',
        additional_case_ids=[uid for uid in ids if uid not in io.read(diagnostic/'experiment.json')['case_ids']],
        known_case_ids=io.read(diagnostic/'experiment.json')['case_ids'],
        known_case_scope='Previously examined ten-question diagnostic, reported separately from the other 290.',
        parent_question_hashes=parent_questions,default_modes_unchanged=True,
        no_automatic_retries=True,diagnostic_archive=str(diagnostic),
        known_semantic_limitations='Historical question-only partial_filter failed; this is a user-authorized evaluation of unchanged semantics, not a claim of semantic repair.',
        options_ablation=dict(enabled=False,retained_for_future=True,archive=str(diagnostic)),
        resource_policy=dict(preference=['2'],minimum_free_mib=32768,maximum_utilization=100,
            stable_observations=3,selection='most_free_memory')))
    # Preserve the failed semantic probe truthfully; new technical batch checks
    # run separately. No options-conditioned probe is regenerated in this run.
    old_gate=io.read(diagnostic/'validation/functional.json')
    io.write(out/'validation/functional.json',dict(old_gate,inherited_from=str(diagnostic/'validation/functional.json'),
        historical=True,scope='Historical diagnostic semantic result; not a new full-run gate.'))
    for name in ['script','script_core']:
        shutil.copytree(ROOT/name,out/'runtime'/name,ignore=shutil.ignore_patterns('__pycache__'))
    for path in ROOT.glob('*.py'):shutil.copy2(path,out/'runtime'/path.name)
    # Reuse the explicit co-residency scheduler, never replace M's GPU lease.
    shutil.copy2(diagnostic/'cohost_queue.py',out/'cohost_queue.py')
    io.write(out/'cohost_authorization.json',dict(allow_gpu_2_co_residency=True,
        user_request='剩下的O和O+去跑完整的；带选项保留后期消融',updated_at=io.now(),
        conditions=['question_only'],batch_size=4,minimum_free_mib=32768))
    ready=0
    for uid in ids:
        if not prepare_case(out,uid):break
        ready+=1
    io.write(out/'validation/source_preflight.json',dict(passed=True,planned=300,ready_prefix=ready,
        original_order=True,answer_labels_used_for_selection=False,model_loaded=False,waiting_for_future_N=True))
    verify_parent(parent);verify_o(diagnostic)
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p) for p in out.rglob('*')
        if p.is_file() and 'source_staging' not in p.parts})
    verify_o(out)
    from script.o_composition_context_eval import report
    report(out)
    io.write(out/'queue_status.json',dict(state='prepared',ready_source_cases=ready,updated_at=io.now()))
    print('FULL O/O+ PREPARED',out,'ready',ready,'planned',len(ids),flush=True)


def assert_upstream_running(parent):
    status=io.read(parent/'queue_status.json')
    if status['state'] not in ['running','waiting_for_gpu','prepared']:
        raise RuntimeError('Unfinished N source and upstream is '+status['state'])
    pid=status.get('controller_pid')
    if pid:
        try:os.kill(pid,0)
        except ProcessLookupError:raise RuntimeError('N source controller is no longer alive')


def reload_after_source_wait(out,backend):
    from script.gpu_queue import acquire
    policy=io.read(out/'experiment.json')['resource_policy']
    lease=acquire(policy,lambda info:io.write(out/'progress.json',dict(stage='waiting_for_gpu_reload',updated_at=io.now(),**info)))
    gpu,_=lease.__enter__()
    os.environ['CUDA_VISIBLE_DEVICES']=gpu['uuid']
    os.environ['VIDEOAGENT_GPU_LEASE']=gpu['reservation_id']
    try:
        began=time.monotonic();details=backend.call('prepare_vlm',{},600)
        path=out/'reloads.json';rows=io.read(path) if path.exists() else []
        rows.append(dict(at=io.now(),seconds=time.monotonic()-began,details=details,gpu=gpu));io.write(path,rows)
        return lease
    except BaseException:
        lease.__exit__(*__import__('sys').exc_info());raise


def run_live(out,backend,cfg):
    from script.o_batched_matrix import run
    from script.gpu_shared_queue import release
    policy=io.read(out/'experiment.json');parent=Path(policy['parent_N']);lease=None
    try:
        for index,uid in enumerate(policy['case_ids'],1):
            released=False
            while not prepare_case(out,uid):
                assert_upstream_running(parent)
                if not released:
                    backend.close(graceful=True)
                    key=os.environ.pop('VIDEOAGENT_GPU_LEASE',None)
                    if key:release(key)
                    if lease:lease.__exit__(None,None,None);lease=None
                    released=True
                io.write(out/'progress.json',dict(stage='waiting_for_N_source',case=index,question_id=uid,
                    parent=str(parent),model_released=True,updated_at=io.now()))
                time.sleep(30)
            if released:lease=reload_after_source_wait(out,backend)
            verify_case(out/'cases'/uid)
            run(out,backend,cfg,case_ids=[uid])
    finally:
        if lease:
            backend.close(graceful=True)
            lease.__exit__(None,None,None)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--parent',type=Path,required=True);parser.add_argument('--diagnostic',type=Path,required=True)
    args=parser.parse_args();prepare(args.out.resolve(),args.parent.resolve(),args.diagnostic.resolve())
