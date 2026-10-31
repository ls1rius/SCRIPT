"""Online E/F final evidence using the evaluated extraction and rendering code."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from .accounting import metered_cpu
from .config import digest
from .state import write_json
from .state_segment_qa import sources

MODES = {'joint_entities': 'E', 'lazy_script': 'F'}


def source_rows(packet, selected, bundle):
    by_caption = {}
    for unit in bundle['units']:
        for cid in unit['source_caption_ids']:
            if cid in by_caption:
                raise ValueError('Ambiguous entity-memory caption source')
            by_caption[cid] = unit
    rows = []
    for index in sorted(selected):
        caption = packet['captions'][index]
        unit = by_caption[caption['id']]
        if (caption['text'].strip() != unit['text'].strip()
                or caption['interval'] not in unit['source_intervals']):
            raise ValueError('Entity-memory source text or sample time changed')
        rows.append(dict(caption_id=caption['id'], frame_index=index, source_id=unit['id'],
            sample_interval=deepcopy(caption['interval']), original_text=caption['text']))
    if {r['caption_id'] for r in rows} != set(bundle['read_caption_ids']):
        raise ValueError('Entity-memory selected source coverage changed')
    return rows


def is_infrastructure(error):
    return any(word in str(error).lower() for word in (
        'cuda', 'out of memory', 'worker', 'timeout', 'deadline', 'exceeded reserved'))


def extractor_version(cfg):
    from . import lazy_script_memory as core
    root = Path(__file__).resolve().parent
    files = ['lazy_script_memory.py', 'joint_entity_memory.py', 'semantic_memory.py',
             'schemas.py', 'prompts.py', 'backend.py', 'batch_extraction.py']
    from .video import file_hash
    return core.VERSION + ':' + digest(dict(models=cfg['models'],
        sources={name: file_hash(root/name) for name in files},
        execution=file_hash(root.parent/'script/lazy_script_execution.py')))


@metered_cpu('entity_evidence_materialization')
def project(original, packet, selected, context, meter, target, cfg, cache_directory=None):
    """Replace Description only; never generate an answer or read new evidence."""
    from script.controlled_memory_io import fixed, parts
    from . import joint_entity_memory as joint, lazy_script_memory as lazy
    from .qa_evidence_view import compact_description
    from script.run_joint_entity_experiment import extract_source as extract_joint
    from script.run_lazy_script_experiment import extract_source as extract_fields
    from script.lazy_script_execution import SourceExtractor, ensure_batched, extraction_counts, EXECUTION_VERSION

    mode = cfg['query']['temporal_qa_evidence']
    variant = MODES[mode]
    target = Path(target)
    bundle = sources(packet, selected, context)
    if variant == 'F':
        # Source-bundle IDs depend on the selected subset. A persistent caption
        # occurrence needs a stable text-only ID across different questions.
        for unit in bundle['units']:
            unit['id'] = 'S' + digest(unit['text'])
    rows = source_rows(packet, selected, bundle)
    write_json(target/'semantic_sources.json', bundle)
    protocol = SimpleNamespace(is_infrastructure=is_infrastructure)
    if variant == 'E':
        results = {}
        for unit in bundle['units']:
            results[unit['id']] = extract_joint(unit, meter, joint, protocol)
            write_json(target/'entity_source_results'/f'{unit["id"]}.json', results[unit['id']])
        memory, audit = joint.materialize(rows, results,
            cfg['query']['entity_max_identity_gap_seconds'], cfg['query']['state_segment_max_gap_seconds'])
        description, render_audit = joint.render(memory)
        audit.update(render=render_audit,
            joint_model_calls=sum(c['operation'] == joint.OPERATION for c in meter.ledger.calls),
            role_reviews=sum(v['role_review'] for v in results.values()),
            retained_original_rows=audit['original_clauses_retained'])
        write_json(target/'joint_entity_memory.json', memory)
    else:
        root = Path(cfg['query']['memory_cache_directory'] or cache_directory or target/'memory_cache')
        database = root/'F'/'script.sqlite3'
        version = extractor_version(cfg)
        video = digest(dict(video_id=packet['question']['video_id'], captions=packet['captions']))
        with lazy.ScriptDB(database, version) as store:
            plan, plan_hit = store.plan(video, packet['question'], bundle['units'], meter)
            if plan['source_ids'] != [u['id'] for u in bundle['units']]:
                raise ValueError('F planner changed upstream selected source scope')
            write_json(target/'field_plan.json', dict(plan, plan_cache_hit=plan_hit))
            extractor = SourceExtractor(meter, lazy, protocol, extract_fields,
                cfg['query']['field_extraction_batch_size'])
            results, cache = ensure_batched(store, video, bundle['units'], rows, plan['fields'], extractor)
            description, memory, audit = store.view(video, packet['question'], rows, results, compact_description)
            audit.update(cache=cache, database=str(database.resolve()), extractor_version=version,
                fields=plan['fields'], plan_cache_hit=plan_hit,
                execution_version=EXECUTION_VERSION, execution_batch_size=extractor.batch_size,
                planning_model_calls=sum(c['operation'] == lazy.PLAN for c in meter.ledger.calls),
                **extraction_counts(meter.ledger.calls, lazy.EXTRACT))
        write_json(target/'lazy_script_memory.json', memory)

    failed = (sum(v.get('error') is not None for v in results.values()) if variant == 'E'
              else audit['cache'].get('status_failed', 0))
    audit.update(mode=mode, variant=variant, selected_source_rows=len(rows), visible_source_rows=len(rows),
        failed_source_fields=failed, source_coverage=dict(read_captions=len(rows), covered_captions=len(rows)),
        scope='Selected captions only; accepted visual observations remain in the original QA block.')
    prefix, _, suffix = parts(original)
    qa = deepcopy(original)
    qa['prompts'][0] = prefix + description + suffix
    if not description.strip() or fixed(qa) != fixed(original):
        raise ValueError('E/F changed fixed QA regions or produced empty evidence')
    write_json(target/'entity_evidence_audit.json', audit)
    return qa, memory, audit
