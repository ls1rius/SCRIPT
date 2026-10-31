"""Independent optimized scheme 4: bounded planning and source-linked events.

Derived from adaptive_entity's sampled-evidence pipeline, retaining the same
question, final QA template, budgets and optional local visual verification.
"""
from copy import deepcopy
from pathlib import Path

from . import adaptive_entity as base
from . import optimized_memory as storage
from .accounting import metered_cpu
from .budget import BudgetLedger, Meter, ResourceExhausted, ToolFailure, exploration_exhausted
from .config import digest
from .optimized_sampling import HybridSampler, needs_window, required_scope
from .state import write_json

VERSION = storage.VERSION


def extraction_input(memory, sources, scene_id, query, plan):
    return dict(question=query['question'], options=query['options'], gap=plan['gap'],
        focus=plan['focus'], fields=plan['fields'], depth=plan['depth'],
        captions=storage.source_view(sources), entities=base.entity_profiles(memory, scene_id),
        events=[dict(id=e['id'], action=e['action'], source_caption_ids=e['source_caption_ids'], roles=e['roles'])
                for e in memory['events'] if set(e['source_caption_ids']) & {c['id'] for c in sources}],
        page_size=3, source_type='caption', entity_stage=True)


def extract_events(memory, sources, scene_id, query, plan, meter, key):
    inp = extraction_input(memory, sources, scene_id, query, plan)
    choices = storage.event_spans(sources)
    inp['captions'] = [dict(c, spans=[dict(ref=ref, text=s['text']) for ref, s in choices.items()
                                    if s['caption_id'] == c['id']]) for c in sources]
    inp['gap'] = plan['gap'] or 'Establish source-supported event instances using committed entity IDs'
    report, call = meter.json('optimized_events', inp, ('entities', 'relations', 'events', 'unresolved'))
    return storage.commit_delta(memory, report, sources, scene_id,
        dict(request_key=key+':events', fields=plan['fields'], focus=plan['focus'],
             depth=plan['depth'], query_hash=digest(query)), call['call_id'], plan['fields'], plan['depth'])


def structure_satisfied(depth, view):
    return depth == 'direct' or (depth == 'entity' and bool(view['entities'])) or (
        depth in ('event', 'causal') and bool(view['events']) and
        (depth != 'causal' or bool(view['transitions'])))


def extract_batch(memory, sources, scene_id, query, plan, meter, requests):
    # Hash the requested evidence and fields, not the exact wording of a gap;
    # new provenance/identity revision or genuinely new fields can be revisited.
    key = digest(dict(sources=sources, focus=plan['focus'], fields=sorted(plan['fields']), depth=plan['depth'],
                      identity_revision=memory['identity_revision']))
    if key in requests:
        return False
    inp = extraction_input(memory, sources, scene_id, query, plan)
    for attempt in range(2):
        report, call = meter.json('optimized_extract', inp, ('entities', 'relations', 'events', 'unresolved'))
        try:
            before = digest([memory['entities'], memory['observations'], memory['events'], memory['relation_records']])
            staged = deepcopy(memory)
            storage.commit_delta(staged, report, sources, scene_id, dict(request_key=key,
                fields=plan['fields'], focus=plan['focus'], depth=plan['depth'], query_hash=digest(query)),
                call['call_id'], plan['fields'], plan['depth'])
            if plan['depth'] in ('event', 'causal'):
                extract_events(staged, sources, scene_id, query, plan, meter, key)
            memory.clear()
            memory.update(staged)
            requests.add(key)
            after = digest([memory['entities'], memory['observations'], memory['events'], memory['relation_records']])
            return before != after
        except ValueError as exc:
            if attempt:
                raise
            inp['validation_feedback'] = str(exc)
    return False


def run(packet, directory, frames, backend, cfg, target, settings, memory_path=None, features=None):
    from .prompts import CONTRACTS
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    extraction_hash = digest(dict(version=VERSION, models=cfg['models'],
        features=packet.get('feature_hash', packet.get('frame_features')),
        contracts={k: v for k, v in CONTRACTS.items() if k.startswith('optimized_') or k == 'scene_correspondence_lineage'}))
    query = dict(packet['question'], id=packet['question']['question_id'])
    memory = storage.load_memory(memory_path, query['video_id'], packet['captions'], extraction_hash, directory)
    reused = len(memory['entities'])
    ledger = BudgetLedger(settings['online_seconds'], answer_reserve=cfg['budget']['final_answer_reserve'],
                          calls_path=target/'calls.jsonl')
    meter = Meter(backend, ledger, cfg, target/'raw_calls')
    seen, opened, reviews, incomplete = set(), [], [], []
    requests = {r['request_key'] for r in memory['extraction_requests']}
    scope = dict(scope=required_scope(query['question']), reason='initial conservative scope')
    result = dict(arm='optimized_scheme4', method=VERSION, base_arm='adaptive_scheme4', base_scheme=4,
                  question_id=query['id'], answer=None, error=None, reused_entity_count=reused)
    supplemental = dict(enabled=False, triggered=False, observations=[], frame_ids=[], seconds=0.)
    sampler = None
    plans = 0
    depth = 'direct'
    selected_seconds = []

    @metered_cpu('checkpoint_write')
    def save():
        write_json(target/'network.json', memory)
        if memory_path:
            write_json(memory_path, memory)

    try:
        embedded, _ = meter.call('clip_text', dict(texts=[query['question']]), build=True)
        if features is None:
            if 'frame_features' in packet:
                features = packet['frame_features']
            else:
                import torch
                features = torch.load(packet['feature_uri'], map_location='cpu', weights_only=True).tolist()
        sampler = HybridSampler(directory, features, embedded['vectors'][0], query['question'])
        priority = base.source_priority(memory, query)
        result['retrieval'] = dict(scene_ranking=sampler.rank_scenes(),
            policy='question_and_gap_visual_lexical_at_both_levels', source_index_priority_caption_ids=priority)
        read_limit = settings.get('optimized_batch_captions', 8)
        max_scenes = settings['max_scene_visits']
        sampler.read_batch(seen, opened, count=settings.get('optimized_initial_captions', 6),
            max_scenes=max_scenes, temporal=needs_window(query['question']), source_priority=priority,
            scope=scope['scope'])
        gap_vectors = {}
        signatures = set()
        for round_ in range(settings.get('optimized_max_plans', 4)):
            selected = [c for c in packet['captions'] if c['id'] in seen]
            coverage = sampler.coverage(scope, seen)
            view = storage.context(memory, query, seen, coverage)
            profiles = [e for e in base.entity_profiles(memory) if set(e['source_caption_ids']) <= seen]
            inp = dict(question=query['question'], options=query['options'], captions=selected, source_type='caption',
                entities=profiles, memory=view, coverage=coverage, previous_depth=depth,
                allowed_scene_ids=[r['scene_id'] for r in sampler.rank_scenes()],
                scene_ranking=sampler.rank_scenes(),
                directory=[dict(id=s['id'], interval=s['interval'], description=s['description']) for s in directory['scenes']],
                previous_batches=reviews[-3:], remaining_plans=settings.get('optimized_max_plans', 4)-round_)
            plan, _ = meter.json('optimized_plan', inp, ('scope', 'depth', 'sufficient', 'reason', 'gap',
                'support_caption_ids', 'scene_ids', 'caption_ids', 'entity_ids', 'focus', 'fields', 'need_identity'))
            plans += 1
            # Scope/depth cannot be downgraded after discovering a more complex need.
            scopes = ['local', 'multi_scene', 'global', 'exhaustive']
            chosen = required_scope(query['question'], plan['scope'])
            scope = dict(scope=max((scope['scope'], chosen), key=scopes.index), reason=plan['reason'])
            depths = ['direct', 'entity', 'event', 'causal']
            depth = max((depth, plan['depth']), key=depths.index)
            if required_scope(query['question']) == 'exhaustive' and depth == 'direct':
                depth = 'event'
            plan['depth'] = depth
            coverage = sampler.coverage(scope, seen)
            reviews.append(dict(plan, read_caption_ids=[c['id'] for c in selected], coverage_check=coverage))
            has_support = bool(plan['support_caption_ids']) and set(plan['support_caption_ids']) <= seen
            if plan['sufficient'] and has_support and coverage['met']:
                # Direct questions can finish without constructing a graph.
                # Deeper questions must have at least the requested kind of facts.
                satisfied = structure_satisfied(depth, view)
                if satisfied:
                    result['stop_reason'] = 'sufficient'
                    break
            gap = plan['gap'].strip()
            if gap and gap != sampler.gap:
                if gap not in gap_vectors:
                    encoded, _ = meter.call('clip_text', dict(texts=[query['question'] + '\nMissing evidence: ' + gap]), build=True)
                    gap_vectors[gap] = encoded['vectors'][0]
                sampler.set_gap(gap, gap_vectors[gap])
            signature = digest(dict(gap=' '.join(gap.casefold().split()), seen=sorted(seen),
                depth=depth, fields=sorted(plan['fields']), facts=digest([memory['entities'], memory['events'], memory['observations'], memory['relation_records']])))
            if signature in signatures:
                result['stop_reason'] = 'no_new_evidence'
                break
            signatures.add(signature)
            changed = False
            extraction_ids = set(plan['caption_ids'])
            if not plan['sufficient'] or not coverage['met'] or not structure_satisfied(depth, view):
                added = sampler.read_batch(seen, opened, count=read_limit, max_scenes=max_scenes,
                    temporal=depth in ('event', 'causal') or needs_window(query['question'] + ' ' + gap),
                    preferred=plan['scene_ids'], scope=scope['scope'], source_priority=priority)
                extraction_ids.update(added)
                changed |= bool(added)
            if depth != 'direct':
                if not extraction_ids:
                    extraction_ids.update(c['id'] for c in selected)
                if not plan['fields']:
                    plan['fields'] = ['state'] if depth == 'entity' else ['action', 'state', 'time']
                if not plan['focus']:
                    plan['focus'] = [query['question'][:64]]
                for sid in opened:
                    sources = [c for c in packet['captions'] if c['id'] in extraction_ids and c['id'] in seen
                               and memory['ownership'][c['id']] == sid]
                    # Fixed-size source batches; one plan executes all batches.
                    for start in range(0, len(sources), 6):
                        changed |= extract_batch(memory, sources[start:start+6], sid, query, plan, meter, requests)
            if plan['need_identity'] and depth != 'direct':
                for sid in opened[-2:]:
                    changed |= storage.align_needed(memory, sid, query, gap, meter, plan['entity_ids'])
            save()
            if not changed:
                # A planner's local confidence cannot close a still-missing event
                # or causal structure. Read another bounded batch if one exists.
                current = storage.context(memory, query, seen, sampler.coverage(scope, seen))
                if not structure_satisfied(depth, current):
                    added = sampler.read_batch(seen, opened, count=read_limit, max_scenes=max_scenes,
                        temporal=True, preferred=plan['scene_ids'], scope=scope['scope'], source_priority=priority)
                    if added:
                        continue
                result['stop_reason'] = 'no_new_evidence'
                break
        else:
            result['stop_reason'] = 'plan_limit'
    except (ResourceExhausted, ToolFailure) as exc:
        if exploration_exhausted(exc):
            incomplete.append(dict(stage='exploration', reason=str(exc)))
            result['stop_reason'] = 'exploration_budget'
        else:
            result['error'] = dict(type=type(exc).__name__, message=str(exc))
    except Exception as exc:
        result['error'] = dict(type=type(exc).__name__, message=str(exc))

    try:
        if sampler is not None:
            selected_seconds = sorted(sampler.positions[c] for c in seen)
            coverage = sampler.coverage(scope, seen)
            result.update(scope_requirement=scope, coverage_check=coverage)
            if not coverage['met']:
                incomplete.append(dict(stage='coverage', missing=coverage['missing'], scope=scope['scope']))
            if result['error'] is None:
                if cfg['query'].get('temporal_sparse_visual', False):
                    from .sparse_visual import supplement
                    sampled = deepcopy(directory)
                    sampled['captions'] = [c for c in packet['captions'] if c['id'] in seen]
                    for scene in sampled['scenes']:
                        scene['caption_ids'] = [c for c in scene['caption_ids'] if c in seen]
                    supplemental = supplement(query, sampled, frames, opened, reviews, scope, meter, target)
                view = storage.context(memory, query, seen, coverage)
                if depth in ('event', 'causal') and not view['events']:
                    incomplete.append(dict(stage='requested_structure', reason='No sourced event instance was established'))
                if depth == 'causal' and not view['transitions']:
                    incomplete.append(dict(stage='requested_structure', reason='No sourced state transition was established'))
                role_rejections = sum(r['kind'] == 'event_role' for r in memory['rejected_fields'])
                if role_rejections:
                    incomplete.append(dict(stage='event_roles', reason='Conflicting event roles remain unknown',
                                           rejected_role_count=role_rejections))
                view['context_selection']['completion_issues'] = list(incomplete)
                # Do not silently prune event roles or ordinal history to fit a
                # token limit. The normal QA token check is still mandatory.
                from .compatibility import qa_inputs
                import json
                inp = qa_inputs(query, packet['captions'], selected_seconds)
                inp['prompts'][-1] += '\n\nSource-linked partial event/state memory:\n' + json.dumps(view, ensure_ascii=False, separators=(',', ':'))
                if supplemental['observations']:
                    inp['prompts'][-1] += '\nSupplemental sampled visual observations:\n' + json.dumps(supplemental['observations'], ensure_ascii=False)
                checked, _ = meter.call('temporal_qa_check', inp, final=True)
                write_json(target/'qa_context.json', dict(context=view, selected_seconds=selected_seconds,
                    read_caption_ids=sorted(seen), input_tokens=checked['input_tokens']))
                answer, _ = meter.call('videotree_qa', inp, final=True)
                result.update(answer=answer['data'], qa_input_tokens=checked['input_tokens'])
    except Exception as exc:
        result['error'] = dict(type=type(exc).__name__, message=str(exc))
    finally:
        try:
            save()
        finally:
            result.update(selected_seconds=selected_seconds, opened_scenes=opened, reviews=reviews,
                incomplete=incomplete, read_caption_ids=sorted(seen), read_caption_count=len(seen),
                total_caption_count=len(packet['captions']), planning_calls=plans, extraction_depth=depth,
                entity_count=len(memory['temporal_network']['entities']), event_count=len(memory['events']),
                observation_count=len(memory['observations']), state_count=len(memory['temporal_network']['states']),
                transition_count=len(memory['temporal_network']['transitions']), relation_count=len(memory['relation_records']),
                identity_comparisons=len(memory['cross_matches']), rejected_field_count=len(memory['rejected_fields']),
                extraction_request_count=len(memory['extraction_requests']), partial_memory=True,
                frame_selection_trace=sampler.trace if sampler else [], supplemental_visual=supplemental,
                cost_summary=ledger.summary())
            write_json(target/'result.json', result)
            meter.close()
    return result
