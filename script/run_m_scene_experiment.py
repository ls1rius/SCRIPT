"""Freeze and run M's full-source, gap-driven retrieval on the original cohort."""
import argparse
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import controlled_memory_io as io


def verify(out):
    for name, expected in io.read(out/'frozen_hashes.json').items():
        if io.sha(out/name) != expected:
            raise ValueError('Frozen M artifact changed: '+name)


def prepare(parent, out):
    import torch
    from PIL import Image
    from script_core.config import load_config, digest
    from script_core.temporal_entity import partition
    from script.run_evidence_planner_experiment import verify as verify_parent
    if out.exists():
        raise ValueError('Use a fresh M output directory; spent budgets are retained')
    started = time.monotonic()
    verify_parent(parent)
    policy = io.read(parent/'experiment.json')
    if policy.get('experiment_label') != 'L' or not io.read(parent/'report.json')['complete']:
        raise ValueError('M requires the completed L cohort')
    h = Path(policy['parent_H'])
    verify_parent(h)
    cfg = load_config(ROOT/'configs/script_M.yaml')
    ids = io.read(parent/'cohort.json')
    if len(ids) != 300 or len(set(ids)) != 300:
        raise ValueError('M must use the unchanged original 300-question cohort')
    out.mkdir(parents=True)
    for name in ('cohort.json', 'evaluation_only.json', 'qa_expected_artifacts.json'):
        shutil.copy2(parent/name, out/name)
    io.write(out/'qa_config.json', cfg)
    for i, uid in enumerate(ids):
        # Reuse ONLY the full source captions and original question, never H/L's
        # selected_seconds, facts, visual observations, plans or predictions.
        raw = io.read(h/'packets'/(uid+'.json'))['packet']
        question = {k:raw['question'][k] for k in ('question_id','video_id','question','options')}
        feature = (ROOT/'data/EgoSchema/egoschema_features'/(question['video_id']+'.pt')).resolve()
        meta = io.read(feature.with_suffix('.json'))
        captions = raw['captions']
        if (meta['fps'] != 1 or meta['shape'] != [len(captions),1280]
                or meta['frame_indices'] != list(range(len(captions)))
                or Path(meta['model']).resolve() != Path(cfg['models']['visual_encoder']).resolve()):
            raise ValueError('Cached feature provenance/grid mismatch: '+uid)
        features = torch.load(feature, map_location='cpu', weights_only=True)
        partition(question['video_id'], features.tolist(), captions)
        frames = []
        for n in range(len(captions)):
            path = (ROOT/'data/EgoSchema/egoschema_frames'/question['video_id']/f'{n}.jpg').resolve()
            with Image.open(path) as image:
                width, height = image.size
            frames.append(dict(frame_id=f'F{n}', pts=float(n), path=str(path), width=width,
                               height=height, sha256=io.sha(path)))
        packet = dict(question=question, captions=captions, frames=frames,
            caption_records_hash=digest(captions), feature_uri=str(feature), feature_hash=io.sha(feature),
            feature_metadata=meta)
        io.write(out/'inputs'/(uid+'.json'), packet)
        if (i+1) % 50 == 0:
            print('M source preflight', i+1, '/', len(ids), flush=True)
    runtime = out/'runtime'
    shutil.copytree(ROOT/'script_core', runtime/'script_core', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT/'script', runtime/'script', ignore=shutil.ignore_patterns('__pycache__'))
    for source in ROOT.glob('*.py'):
        shutil.copy2(source, runtime/source.name)
    io.write(out/'experiment.json', dict(created_at=io.now(), arm='M_scene_planned', planned=300,
        version='M-L-gap-driven-scenes-v1', parent_L=str(parent), parent_H_source=str(h),
        new_retrieval=True, new_extraction=True, answers_reused=False, training=False, adapter=None,
        forced_temporal_coverage=False, full_source_captions=True, cached_frames_available=True,
        directory_rebuilt=True, cpu_preparation_seconds=time.monotonic()-started,
        source_policy='Immutable full captions, cached EVA features and frames; no parent selected evidence or answers.',
        comparison_policy='Historical L is descriptive only: M changes retrieval and reconstructs its evidence.',
        failure_policy='Stop on failed functional gate, source audit or query; no automatic retry.',
        resource_policy=dict(preference=['0','1','2','3'],minimum_free_mib=32768,
            maximum_utilization=100,stable_observations=3,selection='most_free_memory')))
    io.write(out/'validation/cpu_source_preflight.json', dict(passed=True, questions=300,
        model_loaded=False, labels_used=False, all_frames_hashed=True,
        full_timeline_partition_checked=True))
    io.write(out/'frozen_hashes.json', {str(p.relative_to(out)):io.sha(p)
        for p in out.rglob('*') if p.is_file()})
    io.write(out/'queue_status.json', dict(state='prepared', updated_at=io.now()))
    report(out)


def leaf_calls(folder):
    calls = []
    for name in ('directory_calls','partial_search','partial_temporal'):
        path = folder/name/'calls.jsonl'
        if path.exists():
            calls.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return calls


def prepare_revision(prior, out, selection_validation, *, variant='M'):
    """Fresh full-cohort run from immutable sources, including a live prior run.

    The historical run stays intact. Its costs are a dated snapshot/reference,
    never presented as the new run's costs or a final total of a running job.
    """
    from script_core.m_scene_memory import VERSION
    if variant not in ('M','N'):
        raise ValueError('Only M and N full-source variants are supported')
    if variant == 'N':
        from script_core.n_scene_memory import VERSION
    from script_core.m_candidate_selection import VERSION as selection_version
    from script.run_n_selection_diagnostic import verify as verify_selection
    verify(prior)
    verify_selection(selection_validation)
    if out.exists():
        raise ValueError('Use a fresh M output directory')
    gate = io.read(selection_validation/'report.json')
    if not gate.get('complete') or not gate.get('passed'):
        raise ValueError('Candidate selection diagnostic must pass before a full rerun')
    if not io.read(selection_validation/'semantic_review.json').get('passed'):
        raise ValueError('Candidate selection source-grounding review must pass')
    for name in ('script_core/m_candidate_selection.py','script_core/backend.py',
                 'script_core/prompts.py','script_core/schemas.py'):
        if io.sha(ROOT/name) != io.sha(selection_validation/'runtime'/name):
            raise ValueError('Selection validation code mismatch: '+name)
    ids = io.read(prior/'cohort.json')
    if len(ids) != 300 or len(set(ids)) != 300:
        raise ValueError('Full original 300-question cohort required')
    started = time.monotonic()
    out.mkdir(parents=True)
    for name in ('cohort.json','evaluation_only.json','qa_expected_artifacts.json','qa_config.json'):
        shutil.copy2(prior/name,out/name)
    cfg=io.read(out/'qa_config.json')
    cfg['query']['temporal_qa_evidence']={'M':'scene_planned_script','N':'role_bound_scene_script'}[variant]
    cfg['query']['memory_cache_directory']=''
    io.write(out/'qa_config.json',cfg)
    shutil.copytree(prior/'inputs',out/'inputs')
    for name in ('script','script_core'):
        shutil.copytree(ROOT/name,out/'runtime'/name,ignore=shutil.ignore_patterns('__pycache__'))
    for source in ROOT.glob('*.py'):
        shutil.copy2(source,out/'runtime'/source.name)
    # Seal both evidence of validation and its full raw-call location/hash manifest.
    (out/'selection_validation').mkdir(exist_ok=True)
    for name in ('report.json','cases.json','experiment.json','frozen_hashes.json','semantic_review.json'):
        shutil.copy2(selection_validation/name,out/'selection_validation'/name)
    policy = deepcopy(io.read(prior/'experiment.json'))
    policy.update(created_at=io.now(),version=VERSION,selection_version=selection_version,
        experiment_label=variant,arm=variant+'_scene_planned',
        runner_entry=f'script/run_{variant.lower()}_scene_experiment.py',
        prior_attempt=str(prior),reused_benchmark_answers=0,
        cpu_preparation_seconds=time.monotonic()-started,
        candidate_selection_validation=str(selection_validation),
        candidate_selection_validation_sha256=io.sha(selection_validation/'report.json'),
        candidate_selection_review_sha256=io.sha(selection_validation/'semantic_review.json'),
        historical_cost_policy='Prior run may continue; dated snapshot retained separately, never mixed with new measurements.',
        candidate_selection_policy='All-option claim brief, then joint source-quoted evidence-level classification; two model calls.',
        new_retrieval=True,new_extraction=True,directory_rebuilt=True,answers_reused=False,
        functional_gate_policy='Selection semantic regression gate plus fresh complete-pipeline technical gates; autonomous sufficiency limitations remain reported.')
    for key in ('prior_preparation_seconds','prior_runtime_seconds','prior_failed_query_seconds',
                'prior_archived_query_seconds','prior_archived_successful_queries'):
        policy.pop(key,None)
    io.write(out/'historical_cost_snapshot.json',dict(captured_at=io.now(),prior_run=str(prior),
        queue_status=io.read(prior/'queue_status.json'),report=io.read(prior/'report.json'),
        selection_development_reports=[dict(path=str(p),sha256=io.sha(p),cost=io.read(p).get('cost_summary'))
            for pattern in ('N-option-scene-selection-*/report.json','MN-joint-selection-*/report.json')
            for p in sorted(prior.parent.glob(pattern))]))
    io.write(out/'experiment.json',policy)
    io.write(out/'validation/cpu_source_preflight.json',dict(passed=True,questions=300,
        source_snapshot=str(prior),frozen_sources_verified=True,answers_reused=False))
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p)
        for p in out.rglob('*') if p.is_file()})
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    verify(out)
    report(out)


def prepare_after_failed_gate(prior, out):
    """New manual version with no successful answers reused; retain failed calls/costs."""
    verify(prior)
    if out.exists() or io.read(prior/'queue_status.json')['state']!='failed':
        raise ValueError('Need a failed prior gate and a fresh output directory')
    gate_path = prior/'validation/functional.json'
    prior_report=io.read(prior/'report.json')
    from script_core.m_scene_memory import VERSION
    prior_policy=io.read(prior/'experiment.json')
    if prior_report.get('successful',0) and prior_policy['version']==VERSION:
        raise ValueError('Successful answers require a new method version; never mix protocols')
    launch = io.read(prior/'launch.json')
    for pid in (launch['worker_pid'],io.read(prior/'controller_launch.json')['pid']):
        if Path('/proc',str(pid)).exists():
            raise ValueError('Prior M process is still present')
    started = time.monotonic()
    for name in io.read(prior/'frozen_hashes.json'):
        target = out/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(prior/name,target)
    # The new runtime is explicit and separately sealed. Never mutate the failed
    # attempt, reset its costs, or reuse its functional/benchmark answers.
    for name in ('script_core','script'):
        shutil.rmtree(out/'runtime'/name)
        shutil.copytree(ROOT/name,out/'runtime'/name,ignore=shutil.ignore_patterns('__pycache__'))
    for source in ROOT.glob('*.py'):shutil.copy2(source,out/'runtime'/source.name)
    policy = io.read(out/'experiment.json')
    prior_policy = io.read(prior/'experiment.json')
    query_seconds=failed_query_seconds=0.
    for uid in io.read(prior/'cohort.json'):
        folder=prior/'runs'/uid
        seconds=sum(c['actual_cost'] for c in leaf_calls(folder));query_seconds+=seconds
        path=folder/'result.json'
        result=io.read(path) if path.exists() else {}
        if result.get('error') or not result.get('answer'):failed_query_seconds+=seconds
    policy.update(created_at=io.now(),prior_attempt=str(prior),version=VERSION,
        closed_candidate_set=True,candidate_order_policy='Initial descending relevance ranking; never extend outside the set',
        navigation_audit='partial_search/navigation_trace.json',
        prior_preparation_seconds=prior_policy.get('prior_preparation_seconds',0.)+prior_policy['cpu_preparation_seconds'],
        prior_runtime_seconds=prior_policy.get('prior_runtime_seconds',0.)+io.read(prior/'setup.json')['seconds']+
            io.read(prior/'functional/cost_summary.json')['seconds']+query_seconds,
        prior_failed_query_seconds=prior_policy.get('prior_failed_query_seconds',0.)+failed_query_seconds,
        prior_archived_query_seconds=prior_policy.get('prior_archived_query_seconds',prior_policy.get('prior_failed_query_seconds',0.))+query_seconds,
        prior_archived_successful_queries=prior_policy.get('prior_archived_successful_queries',0)+prior_report.get('successful',0),
        reused_benchmark_answers=0,
        cpu_preparation_seconds=time.monotonic()-started,
        gate_repair='Closed ranked candidate set, explicit exhaustion and temporal attribute presentation; semantic sufficiency remains diagnostic.',
        functional_gate_policy='Require source/decoder/planner and explicit-gap execution contracts. Publish autonomous sufficiency failures as model limitations; do not claim semantic validation.')
    io.write(out/'experiment.json',policy)
    io.write(out/'frozen_hashes.json',{str(p.relative_to(out)):io.sha(p)
        for p in out.rglob('*') if p.is_file() and p.name!='frozen_hashes.json'})
    verify(out)
    io.write(out/'queue_status.json',dict(state='prepared',updated_at=io.now()))
    report(out)


def report(out):
    ids = io.read(out/'cohort.json')
    labels = io.read(out/'evaluation_only.json')
    policy = io.read(out/'experiment.json') if (out/'experiment.json').exists() else {}
    variant=policy.get('experiment_label','M')
    completed, successful, correct, errors = 0, 0, 0, 0
    calls, inputs, qa_times, visits, candidate_counts = [], [], [], [], []
    stop_reasons={};outside_candidates=0
    for uid in ids:
        folder = out/'runs'/uid
        calls.extend(leaf_calls(folder))
        path = folder/'result.json'
        failure = folder/'failure.json'
        if not path.exists() and not failure.exists():
            continue
        completed += 1
        result = io.read(path if path.exists() else failure)
        answer = result.get('answer')
        if result.get('error') or not answer:
            errors += 1
            continue
        successful += 1
        correct += int(str(answer['selected_option']) == str(labels[uid]))
        qa = [c for c in leaf_calls(folder) if c['operation']==variant.lower()+'_answer' and not c.get('error')]
        inputs.append(sum(c.get('input_tokens',0) for c in qa))
        qa_times.append(sum(c['actual_cost'] for c in qa))
        search=io.read(folder/'partial_search/result.json')
        visits.append(len(search.get('opened_scenes',[])))
        order=search.get('retrieval',{}).get('candidate_order')
        if order is not None:
            candidate_counts.append(len(order))
            outside_candidates+=len(set(search.get('opened_scenes',[]))-set(order))
        reason=search.get('stop_reason','unknown')
        stop_reasons[reason]=stop_reasons.get(reason,0)+1
    by_operation = {}
    for call in calls:
        item = by_operation.setdefault(call['operation'],dict(calls=0,seconds=0.,input_tokens=0,output_tokens=0))
        item['calls'] += 1
        item['seconds'] += call['actual_cost']
        for k in ('input_tokens','output_tokens'):
            item[k] += call.get(k,0)
    result = dict(updated_at=io.now(),experiment_label=variant, planned=len(ids), completed=completed, successful=successful,
        pending=len(ids)-completed, correct=correct, errors=errors, complete=successful==len(ids),
        accuracy_among_completed=correct/completed if completed else None,
        mean_input_tokens=statistics.mean(inputs) if inputs else None,
        mean_qa_seconds=statistics.mean(qa_times) if qa_times else None,
        mean_opened_scenes=statistics.mean(visits) if visits else None,
        mean_candidate_scenes=statistics.mean(candidate_counts) if candidate_counts else None,
        outside_candidate_openings=outside_candidates,stop_reasons=stop_reasons,
        measured_seconds_by_operation=by_operation,
        measured_query_seconds=sum(c['actual_cost'] for c in calls),
        forced_temporal_coverage=False, reader_protocol='K-constrained-prediction-letter-v1',
        token_scope=f'Final {variant.lower()}_answer input only; all directory, retrieval, extraction and planning calls listed separately.',
        timing_policy='Shared GPU observations; not a controlled speed comparison.',
        comparison_policy=f'{variant} reconstructs source retrieval and evidence; historical L scores are not a same-runtime control.')
    if policy.get('resume_successful_ids') is not None:
        result.update(reused_benchmark_answers=len(policy['resume_successful_ids']),
            navigation_decoder=policy['navigation_decoder'],
            failed_decoder_operation=policy.get('failed_decoder_operation','m_transition_next'),
            resume_protocol='Valid prefix inherited byte-for-byte; remaining queries use repaired navigation/candidate JSON decoder.',
            archived_failed_query_seconds=policy['prior_failed_query_seconds'])
    for key in ('cpu_preparation_seconds','prior_preparation_seconds','prior_runtime_seconds','prior_attempt',
                'prior_failed_query_seconds','prior_interrupted_query_seconds','prior_archived_query_seconds','prior_archived_successful_queries'):
        result[key] = policy.get(key, None if key=='prior_attempt' else 0.)
    if 'continuation_reason' in policy:
        result['continuation_reason'] = policy['continuation_reason']
    if (out/'setup.json').exists():
        result['model_setup_seconds'] = io.read(out/'setup.json').get('seconds')
    if (out/'functional/cost_summary.json').exists():
        result['functional_seconds'] = io.read(out/'functional/cost_summary.json')['seconds']
    if (out/'validation/functional.json').exists():
        gate=io.read(out/'validation/functional.json')
        result['functional_technical_contracts_passed']=gate['passed']
        result['semantic_navigation_probe_passed']=gate.get('semantic_navigation_probe_passed')
        result['semantic_diagnostics']=gate.get('semantic_diagnostics',[])
        result['functional_gate_scope']=gate.get('scope')
        if 'saved_decoder_operation' in gate:
            result['saved_decoder_operation']=gate['saved_decoder_operation']
    if (out/'selection_validation/report.json').exists():
        result['candidate_selection_regressions_passed']=io.read(out/'selection_validation/report.json')['passed']
        result['candidate_selection_source_review_passed']=io.read(out/'selection_validation/semantic_review.json')['passed']
    io.write(out/'report.json', result)
    return result


def synthetic_case(question, options, texts, descriptions, uid):
    captions = [dict(id=f'C{i}',interval=[i,i+1],text=t) for i,t in enumerate(texts)]
    packet = dict(question=dict(question_id=uid,video_id=uid,question=question,options=options),
                  captions=captions,frames=[],feature_hash='synthetic-not-benchmark')
    directory = dict(captions=captions,scenes=[dict(id=f'S{i}',interval=[i,i+1],center=[1.,0.],
        caption_ids=[f'C{i}'],chronological_order=[i],description=t) for i,t in enumerate(descriptions)],pointers=[])
    return packet, directory


def functional(out, backend, cfg):
    from script_core.budget import BudgetLedger, Meter
    from script_core import i_evidence_planner as planner
    from script_core.scheme4 import answer
    from script_core import m_scene_memory
    is_n=cfg['query']['temporal_qa_evidence']=='role_bound_scene_script'
    planner_operation='n_evidence_plan' if is_n else 'm_evidence_plan'
    checks, failures, semantic_diagnostics = [], [], []
    # A new activity and different wording from the development cases. This is
    # a semantic retrieval check, not a claim of general benchmark improvement.
    from script_core import m_candidate_selection as selection
    selection_inputs=dict(question='What task is C carrying out?',options=[
        'Checking messages on a phone.','Measuring and marking a piece of wood.',
        'Painting a panel.','Watering flowers.','Sharpening a knife.'],
        directory=[dict(id='S0',interval=[0,10],description='C walks through a workshop doorway.'),
            dict(id='S1',interval=[10,20],description='C lays a ruler along a wooden board and draws a pencil mark at the measured length.'),
            dict(id='S2',interval=[20,30],description='An empty road with parked cars and no people.')],source_type='caption')
    selection_ledger=BudgetLedger(450,answer_reserve=0,calls_path=out/'functional/selection/calls.jsonl')
    selection_meter=Meter(backend,selection_ledger,cfg,out/'functional/selection/raw_calls')
    try:
        proposal=selection.review_scenes(selection_inputs,selection_meter)
        selected=selection.normalize_selection(proposal,selection_inputs)
        candidates=m_scene_memory.validate_candidates(selected,selection_inputs)
        passed=bool(candidates) and candidates[0]=='S1' and 'S2' not in candidates
        io.write(out/'functional/selection/result.json',dict(inputs=selection_inputs,model_output=proposal,
            actual_selection=selected,candidate_order=candidates,passed=passed))
        checks.append(dict(case='new_activity_candidate_semantics',passed=passed,candidates=candidates))
        if not passed:
            failures.append(dict(case='new_activity_candidate_semantics',candidates=candidates))
    finally:
        io.write(out/'functional/selection/cost_summary.json',selection_ledger.summary())
        selection_meter.close()
    payload=dict(observations=[dict(caption_id='C0',frame_index=0,sample_interval=[0,1],
        evidence=dict(original_text='The box is red.'))])
    memory=m_scene_memory.merged.materialize(payload)
    temporal=dict(entities=[dict(id='E0',label='box',attributes=[dict(field='color',value='red',
        source_caption_ids=['C0'],observed_at=[0])])],states=[],relations=[])
    original=dict(head='Synthetic source',prompts=['  Description: '+m_scene_memory.merged.render(memory)[0]+
        '\n\n###\n\n Questions: What color?\nTemporal entity memory (caption):\n'+json.dumps(temporal)])
    attribute_pool=m_scene_memory.build_pool(memory,original,io.parts)
    attribute_text,attribute_audit=m_scene_memory.render(attribute_pool,planner.keep_all(attribute_pool))
    attr_ok='text-inferred hypothesis' in attribute_text and 'records attribute "color" with value "red"' in attribute_text
    io.write(out/'functional/temporal_attribute.json',dict(pool=attribute_pool,audit=attribute_audit,text=attribute_text,passed=attr_ok))
    checks.append(dict(case='temporal_attribute_presentation',passed=attr_ok))
    if not attr_ok:failures.append(dict(case='temporal_attribute_presentation'))
    ledger = BudgetLedger(600,answer_reserve=0,calls_path=out/'functional/planner/calls.jsonl')
    meter = Meter(backend,ledger,cfg,out/'functional/planner/raw_calls')
    try:
        probe = planner.functional_probe(io.parts)
        large = dict(version=planner.VERSION,records=[dict(id=f'R{i}',
            text=('C wipes the table using a cloth.' if i==0 else
                  'After wiping, C puts the used cloth into the sink.' if i==129 else
                  f'Unused storage locker number {i} is blue.'),
            qualification='source assertion',kinds=['caption'],sources=[dict(region='probe')],
            occurrences=[dict(interval=[i,i+1])]) for i in range(130)])
        large['pool_sha256'] = planner.digest(large['records'])
        for name, pool, question, required, skipped in [
            ('I_H_features',probe['pool'],probe['question'],probe['required_ids'],probe['skipped_ids']),
            ('130_records',large,dict(question='Which tool does C use to wipe the table?',
                options=['a cloth','a brush','a knife','a spoon','paper']),['R0'],['R1'])]:
            inputs = planner.planner_input(question,pool,'M-functional-'+name)
            output,_ = meter.call(planner_operation,dict(inputs=inputs,schema=planner.plan_schema(pool)))
            io.write(out/'functional/planner'/(name+'_output.json'),output)
            plan = planner.decode_plan(output['raw'],pool)
            text,audit = planner.render(pool,plan)
            retained = set(plan['keep']+plan['uncertain']+[r for g in plan['merge_groups'] for r in g])
            passed = set(required)<=retained and set(skipped)<=set(plan['skip'])
            checks.append(dict(case=name,passed=passed,plan=plan,audit=audit))
            if not passed:
                failures.append(dict(case=name,error='Synthetic retention/filtering failed'))
    finally:
        io.write(out/'functional/planner/cost_summary.json',ledger.summary())
        meter.close()
    cases = [
        synthetic_case('What does C mainly use to wipe the table throughout the video?',
            ['a cloth','a brush','a sponge','paper','a glove'],
            ['#C C wipes the table with a cloth.','A blue chair is beside the wall.'],
            ['C wipes the table with a cloth.','An unrelated chair.'],'single'),
        synthetic_case('How does the condition of the book change from before to after C wipes it?',
            ['wet to dry','wet to oily','dusty to dry','dusty to oily','wet to dusty'],
            ['#C The book is wet before C wipes it.','#C The book is dry after C wipes it.'],
            ['The book before wiping.','The book after wiping.'],'comparison'),
        synthetic_case('Which dog does C walk?',
            ['black dog','white dog','brown dog','spotted dog','grey dog'],
            ['An empty chair is blue.','A table stands next to a wall.'],
            ['An empty blue chair.','A stationary table and wall.'],'empty')]
    gate_cfg = deepcopy(cfg)
    gate_cfg['query']['temporal_sparse_visual'] = False
    # Test the expansion EXECUTION contract with an explicit, known missing
    # distinction. Separately retain the autonomous semantic diagnosis below;
    # inability to recognize a distinction is model behavior to measure, not a
    # parser/source/runtime failure to hide or repair with forced coverage.
    packet,directory=cases[1]
    request=dict(question=packet['question']['question'],options=packet['question']['options'],
        captions=[packet['captions'][1]],previous_scene_captions=[],entities=[],observations=[],
        relations=[],prior_requests=[],directory=[{k:s[k] for k in ('id','interval','description')}
            for s in directory['scenes']],allowed_scene_ids=['S0'],opened_scenes=['S1'],
        current_scene='S1',previous_reviews=[],source_type='caption',sufficiency_blocked=True,
        candidate_boundary_enforced=True,initial_candidate_order=['S1','S0'],remaining_candidate_ids=['S0'],
        sufficiency_feedback=[dict(option_id='O2',missing_detail=
            'The condition BEFORE wiping is unobserved; C1 states only the AFTER condition.')])
    gap_ledger=BudgetLedger(150,answer_reserve=0,calls_path=out/'functional/gap/calls.jsonl')
    gap_meter=Meter(backend,gap_ledger,gate_cfg,out/'functional/gap/raw_calls')
    try:
        step,_=gap_meter.json('m_resolve_gap',request,
            ('resolution_status','reason','gap','action','scene_id','caption_ids','entity_ids','focus','fields'))
        m_scene_memory.validate_step(step,request)
        passed=step['action']=='read_scene' and step['scene_id']=='S0' and not step['sufficient']
        checks.append(dict(case='explicit_gap_expansion_contract',passed=passed,decision=step))
        if not passed:failures.append(dict(case='explicit_gap_expansion_contract',decision=step))
    finally:
        io.write(out/'functional/gap/cost_summary.json',gap_ledger.summary())
        gap_meter.close()
    for packet, directory in cases:
        uid = packet['question']['question_id']
        folder = out/'functional'/uid
        directory['cost_summary'] = BudgetLedger(600,answer_reserve=0).summary()
        io.write(folder/'directory.json', directory)
        try:
            result = answer(packet, [], gate_cfg, backend, folder)
        except Exception as exc:
            io.write(out/'validation/functional.json',dict(passed=False,checks=checks,
                failures=failures+[dict(case=uid,error=f'{type(exc).__name__}: {exc}')],
                updated_at=io.now(),benchmark_labels_used=False))
            raise
        io.write(folder/'gate_result.json', result)
        if result.get('error') or not result.get('answer'):
            failures.append(dict(case=uid,error=result.get('error', 'missing answer')))
        else:
            search = io.read(folder/'partial_search/result.json')
            opened = search['opened_scenes']
            candidates=search['retrieval']['candidate_order']
            if opened!=candidates[:len(opened)] or not (folder/'partial_search/navigation_trace.json').exists():
                failures.append(dict(case=uid,error='Closed candidate order or navigation audit failed'))
            if result['coverage_check']['enforced'] or result['coverage_check']['met'] is not None:
                failures.append(dict(case=uid,error='Forced temporal coverage leaked into M'))
            if uid=='single' and opened!=['S0']:
                failures.append(dict(case=uid,error='Single sufficient scene did not stop',opened=opened))
            if uid=='single' and candidates!=['S0']:
                failures.append(dict(case=uid,error='Unrelated chair entered initial candidate set',candidates=candidates))
            if uid=='empty' and (opened or candidates or search['stop_reason']!='no_relevant_candidates'):
                failures.append(dict(case=uid,error='No-match case searched unrelated sources',opened=opened,candidates=candidates))
            if uid=='comparison':
                semantic_diagnostics.append(dict(case=uid,
                    passed=set(opened)=={'S0','S1'} and str(result['answer']['selected_option'])=='0',
                    opened=opened,expected_opened=['S0','S1'],answer=result['answer'],
                    limitation='Autonomous early stopping can mistake a partial option match for full discrimination.'))
            if uid=='single' and str(result['answer']['selected_option'])!='0':
                failures.append(dict(case=uid,error='Synthetic answer incorrect'))
            checks.append(dict(case=uid,candidates=candidates,opened=opened,answer=result['answer'],
                forced_coverage=False,raw_calls=len(leaf_calls(folder))))
    gate = dict(passed=not failures,failures=failures,checks=checks,updated_at=io.now(),
        semantic_navigation_probe_passed=all(x['passed'] for x in semantic_diagnostics),
        semantic_diagnostics=semantic_diagnostics,benchmark_labels_used=False,
        scope='Technical planning, source, gap execution and complete-pipeline contracts; autonomous semantic sufficiency is a separately reported diagnostic.',
        gate_policy='Semantic diagnostic failures remain visible limitations and are not claimed repaired; benchmark measures actual M decisions.')
    io.write(out/'validation/functional.json',gate)
    if failures:
        raise ValueError('M real-model functional gate failed: '+str(failures))
    return gate


def worker(out, *, functional_gate=None):
    verify(out)
    from script.run_evidence_planner_experiment import verify_model
    from script_core.budget import ModelWorker
    from script_core.scheme4 import answer
    import torch
    if (out/'setup.json').exists():
        raise ValueError('M already attempted; preserve old attempt instead of retrying')
    verify_model(out)
    cfg = io.read(out/'qa_config.json')
    variant=io.read(out/'experiment.json').get('experiment_label','M')
    backend = ModelWorker(cfg)
    started = time.monotonic()
    io.write(out/'setup.json',dict(state='loading',started_at=io.now()))
    try:
        prepared = backend.call('prepare_vlm',{},600)
        io.write(out/'setup.json',dict(state='ready',seconds=time.monotonic()-started,details=prepared))
        gate_started = time.monotonic()
        try:
            (functional_gate or functional)(out,backend,cfg)
        finally:
            io.write(out/'functional/cost_summary.json',dict(seconds=time.monotonic()-gate_started,
                calls=[c for name in ('single','comparison','empty') for c in leaf_calls(out/'functional'/name)],
                planner_cost=io.read(out/'functional/planner/cost_summary.json')
                    if (out/'functional/planner/cost_summary.json').exists() else None,
                gap_cost=io.read(out/'functional/gap/cost_summary.json')
                    if (out/'functional/gap/cost_summary.json').exists() else None,
                selection_cost=io.read(out/'functional/selection/cost_summary.json')
                    if (out/'functional/selection/cost_summary.json').exists() else None,
                decoder_cost=io.read(out/'functional/decoder/cost_summary.json')
                    if (out/'functional/decoder/cost_summary.json').exists() else None))
        print(variant+' REAL TECHNICAL CONTRACTS PASSED; semantic diagnostic is reported separately',flush=True)
        inherited = set(io.read(out/'experiment.json').get('resume_successful_ids', []))
        for index, uid in enumerate(io.read(out/'cohort.json'),1):
            if uid in inherited:
                continue  # Explicitly sealed successful prefix, never an implicit retry/cache hit.
            folder = out/'runs'/uid
            io.write(out/'progress.json',dict(stage='query',index=index,question_id=uid,updated_at=io.now()))
            packet = io.read(out/'inputs'/(uid+'.json'))
            if io.sha(Path(packet['feature_uri']))!=packet['feature_hash']:
                raise ValueError('Cached M features changed: '+uid)
            for frame in packet['frames']:
                if io.sha(Path(frame['path']))!=frame['sha256']:
                    raise ValueError('Cached M frame changed: '+frame['path'])
            features = torch.load(packet['feature_uri'],map_location='cpu',weights_only=True).tolist()
            try:
                result = answer(packet,features,cfg,backend,folder)
                if result.get('error') or not result.get('answer'):
                    raise ValueError(variant+' query failed: '+str(result.get('error')))
                if io.read(folder/'partial_temporal/result.json').get('memory_variant')!=variant:
                    raise ValueError('Experiment used the wrong memory variant')
                search=io.read(folder/'partial_search/result.json')
                order=search['retrieval']['candidate_order'];opened=search['opened_scenes']
                if opened!=order[:len(opened)] or not (folder/'partial_search/navigation_trace.json').exists():
                    raise ValueError('M candidate boundary/order or navigation audit violated')
            except Exception as exc:
                io.write(folder/'failure.json',dict(error=dict(type=type(exc).__name__,message=str(exc)),answer=None))
                report(out)
                raise
            summary = report(out)
            print(variant,index,uid,'completed',summary['completed'],'errors',summary['errors'],flush=True)
    finally:
        backend.close(graceful=True)
        report(out)


def queue(out):
    verify(out)
    from script.gpu_queue import acquire
    with (out/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (out/'launch.json').exists():
            raise ValueError('M worker already attempted; no automatic retries')
        def status(state,**extra):
            io.write(out/'queue_status.json',dict(state=state,controller_pid=os.getpid(),updated_at=io.now(),**extra))
        try:
            with acquire(io.read(out/'experiment.json')['resource_policy'],
                         lambda details:status('waiting_for_gpu',**details)) as (device,fds):
                env = dict(os.environ,CUDA_VISIBLE_DEVICES=device['uuid'],CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
                    OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',PYTHONUNBUFFERED='1')
                runner=io.read(out/'experiment.json').get('runner_entry','script/run_m_scene_experiment.py')
                command = [sys.executable,'-u',str(out/'runtime'/runner),
                           'worker','--out',str(out)]
                with (out/'worker.log').open('a') as log:
                    process = subprocess.Popen(command,cwd=out/'runtime',env=env,stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,pass_fds=fds)
                launch = dict(worker_pid=process.pid,gpu=device['index'],gpu_uuid=device['uuid'],
                              command=command,started_at=io.now())
                io.write(out/'launch.json',launch)
                while process.poll() is None:
                    status('running',**launch)
                    time.sleep(5)
                if process.returncode or not report(out)['complete']:
                    raise RuntimeError(io.read(out/'experiment.json').get('experiment_label','M')+
                                       ' worker failed or incomplete; exit='+str(process.returncode))
                status('complete',**launch)
        except Exception as exc:
            status('failed',error_type=type(exc).__name__,error=str(exc))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','queue','worker','report'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--parent',type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    if args.mode=='prepare':
        if not args.parent:
            parser.error('prepare requires --parent completed L')
        prepare(args.parent.resolve(),out)
    elif args.mode=='queue':
        queue(out)
    elif args.mode=='worker':
        worker(out)
    else:
        print(json.dumps(report(out),ensure_ascii=False))


if __name__=='__main__':
    main()
