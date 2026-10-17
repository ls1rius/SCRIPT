"""Coverage is computed from actual inputs; directory coverage never proves absence."""
from .config import digest
from .state import obj


def intersect(a, b):
    start, end = max(a[0], b[0]), min(a[1], b[1])
    return [start, end] if start < end else None


def complement(scope, intervals):
    covered = sorted(i for x in intervals if (i := intersect(scope, x)))
    cursor, gaps = scope[0], []
    for start, end in covered:
        if start > cursor:
            gaps.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < scope[1]:
        gaps.append([cursor, scope[1]])
    return gaps


def units(scope, seconds=6.):
    cursor = scope[0]
    out = []
    while cursor < scope[1]:
        end = min(cursor+seconds, scope[1])
        out.append([cursor, end])
        cursor = end
    return out


def checked_intervals(memory, requirement_id):
    return [c['interval'] for c in memory.tables['coverage'].values()
            if requirement_id in c['target_requirement_ids'] and c['level'] == 'protocol_checked'
            and c['visibility'] == 'clear' and c['result'] != 'unknown'
            and memory.is_valid(c['verifier_ref'])]


def coverage_record(id_, core, requirement_ids, evidence, verification_ref, report, cfg, source_catalog=None):
    pts = sorted(set(f['pts'] for f in evidence['frame_inputs'] if f.get('visual_usable', True)))
    clipped = [p for p in pts if core[0] <= p < core[1]]
    gaps = [b-a for a, b in zip([core[0], *clipped], [*clipped, core[1]])]
    max_gap = max(gaps) if clipped else None
    size = report.get('min_target_short_side')
    clear = report.get('visibility') == 'clear' and not report.get('unresolved_conflicts')
    source_pts = sorted({f['pts'] for f in source_catalog or [] if core[0] <= f['pts'] < core[1]
                         and not f.get('missing', False)})
    source_limited = bool(source_pts and max_gap is not None and max_gap > 1/cfg['observation']['base_fps']+1e-6
                          and set(source_pts) <= set(clipped))
    sufficient = (clipped and (max_gap <= 1/cfg['observation']['base_fps']+1e-6 or source_limited)
                  and clear and isinstance(size, (int, float))
                  and size >= cfg['observation']['min_target_short_side']
                  and report.get('result') in ('change_seen', 'no_change_seen')
                  and report.get('task_verified') is True and verification_ref)
    return obj(id_, interval=core, target_requirement_ids=requirement_ids,
               evidence_ids=[evidence['id']], actual_pts=pts, max_sample_gap=max_gap,
               min_target_short_side=size, visibility=report.get('visibility', 'unknown'),
               level='protocol_checked' if sufficient else 'sampled',
               result=report.get('result', 'unknown'), protocol_hash=digest(cfg['coverage'] | cfg['observation']),
               verifier_ref=verification_ref if sufficient else None, source_frame_rate_limited=source_limited)


def midpoint_observed(evidence, midpoint, cfg):
    return (evidence['interval'][0] <= midpoint < evidence['interval'][1]
            and any(abs(f['pts']-midpoint) <= 1/cfg['observation']['base_fps']+1e-6
                    for f in evidence['frame_inputs'] if f.get('visual_usable', True)))


def midpoint_attempted(evidence, midpoint, cfg):
    """An unreadable attempt is scheduling history, never proof of coverage."""
    return (evidence['interval'][0] <= midpoint < evidence['interval'][1]
            and any(abs(f['pts']-midpoint) <= 1/cfg['observation']['base_fps']+1e-6
                    for f in evidence['frame_inputs']))
