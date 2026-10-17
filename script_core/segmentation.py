"""Exact contiguous SSE partitioning, original-frame boundaries and windows."""
from bisect import bisect_left
import math


def contiguous_partition(features, penalty=5., min_samples=2, tie_epsilon=1e-8):
    import numpy as np
    x = np.asarray(features, dtype=np.float64)
    if x.ndim != 2 or not len(x) or not np.isfinite(x).all():
        raise ValueError('Expected a finite, nonempty feature matrix')
    norm = np.linalg.norm(x, axis=1, keepdims=True)
    if (norm <= 0).any() or penalty < 0 or min_samples < 1:
        raise ValueError('Invalid features or segmentation parameters')
    x = x / norm
    n = len(x)
    if n < min_samples:
        return [(0, n)]
    p = np.vstack([np.zeros(x.shape[1]), np.cumsum(x, axis=0)])
    q = np.r_[0., np.cumsum((x*x).sum(axis=1))]
    values = [math.inf] * (n+1)
    counts = [n+1] * (n+1)
    prev = [-1] * (n+1)
    values[0], counts[0] = -penalty, 0
    for b in range(min_samples, n+1):
        for a in range(b-min_samples+1):
            if not math.isfinite(values[a]):
                continue
            z = p[b] - p[a]
            val = values[a] + max(0., q[b]-q[a]-float(z@z)/(b-a)) + penalty
            tie = abs(val-values[b]) < tie_epsilon
            if val < values[b]-tie_epsilon or (tie and (counts[a]+1, a) < (counts[b], prev[b])):
                values[b], counts[b], prev[b] = val, counts[a]+1, a
    out, b = [], n
    while b:
        a = prev[b]
        if a < 0:
            raise ValueError('No feasible partition')
        out.append((a, b))
        b = a
    return list(reversed(out))


def frame_boundaries(segments, sample_pts, frame_pts, cutoff):
    if not frame_pts or cutoff <= 0:
        raise ValueError('No legal original frames')
    if any(a >= b for a, b in zip(frame_pts, frame_pts[1:])):
        raise ValueError('Original frame PTS must be strictly increasing')
    boundaries = [0]
    for _, b in segments[:-1]:
        t = (sample_pts[b-1] + sample_pts[b]) / 2
        index = bisect_left(frame_pts, t-1e-12)
        if 0 < index < len(frame_pts) and index != boundaries[-1]:
            boundaries.append(index)
    boundaries.append(len(frame_pts))
    return [{'interval': [0. if a == 0 else frame_pts[a],
                          cutoff if b == len(frame_pts) else frame_pts[b]],
             'start_frame': a, 'end_frame_exclusive': b}
            for a, b in zip(boundaries, boundaries[1:])]


def windows(interval, length=8., stride=7.):
    a, end = interval
    if length <= 0 or stride <= 0 or stride > length:
        raise ValueError('Invalid window specification')
    result = []
    while a < end:
        b = min(end, a+length)
        result.append([a, b])
        if b == end:
            break
        a += stride
    return result


def nearest_indices(pts, targets, interval):
    lo, hi = bisect_left(pts, interval[0]), bisect_left(pts, interval[1])
    if lo == hi:
        return []
    out = []
    for t in targets:
        i = bisect_left(pts, t, lo, hi)
        choices = [j for j in (i-1, i) if lo <= j < hi]
        distance = min(abs(pts[k]-t) for k in choices)
        j = min((k for k in choices if abs(pts[k]-t) <= distance+1e-12), key=lambda k: pts[k])
        if j not in out:
            out.append(j)
    return sorted(out)


def sample_indices(pts, interval, fps=None, count=None):
    a, b = interval
    if count is not None:
        targets = [a+(i+.5)*(b-a)/count for i in range(count)]
    else:
        targets = [a+i/fps for i in range(math.ceil((b-a)*fps)) if a+i/fps < b]
    return nearest_indices(pts, targets, interval)


def impute_short_samples(manifest, features, sample_pts, sample_frame_ids, fps):
    """Fill <=2-second declared gaps for segmentation only; long gaps remain explicit."""
    pts = [f['pts'] for f in manifest['frames']]
    indices = sample_indices(pts, [0., manifest['cutoff']], fps=fps)
    by_pts = {p: (v, fid) for p, v, fid in zip(sample_pts, features, sample_frame_ids)}
    rows, missing = [], []
    for i in indices:
        entry = manifest['frames'][i]
        timestamp = entry['pts']
        if timestamp in by_pts:
            vector, frame_id = by_pts[timestamp]
            rows.append((timestamp, vector, frame_id, False))
        elif entry.get('missing'):
            gap = next((g for g in manifest['missing_intervals'] if g[0] <= timestamp < g[1]), None)
            if gap and gap[1]-gap[0] <= 2. and sample_pts:
                nearest = min(sample_pts, key=lambda t: (abs(t-timestamp), t))
                rows.append((timestamp, by_pts[nearest][0], None, True))
            else:
                missing.append(timestamp)
    rows.sort(key=lambda r: r[0])
    return {'features': [r[1] for r in rows], 'pts': [r[0] for r in rows],
            'frame_ids': [r[2] for r in rows], 'imputed': [r[3] for r in rows],
            'missing_sample_pts': missing}


def boundaries_at_times(times, frame_pts, cutoff):
    indices = sorted({0, len(frame_pts), *(bisect_left(frame_pts, t) for t in times if 0 < t < cutoff)})
    return [{'interval': [0. if a == 0 else frame_pts[a], cutoff if b == len(frame_pts) else frame_pts[b]],
             'start_frame': a, 'end_frame_exclusive': b} for a, b in zip(indices, indices[1:]) if a < b]


def comparison_partition(features, sample_pts, frame_pts, cutoff, cfg, seed=0):
    """Explicit alternatives; the primary exact-DP objective stays unchanged."""
    import numpy as np
    if cfg['solver'] == 'fixed_windows':
        length = cfg['fixed_window_seconds']
        boundaries = boundaries_at_times([i*length for i in range(1, math.ceil(cutoff/length))], frame_pts, cutoff)
        return boundaries, {'solver': 'fixed_windows', 'scene_count': len(boundaries)}
    if cfg['solver'] == 'kmeans_runs':
        from sklearn.cluster import KMeans
        x = np.asarray(features, dtype=np.float64)
        x /= np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-12)
        k = min(cfg['kmeans_k'], len(x))
        labels = KMeans(n_clusters=k, random_state=seed, n_init=10).fit_predict(x)
        starts = [0]+[i for i in range(1, len(labels)) if labels[i] != labels[i-1]]+[len(labels)]
        parts = list(zip(starts, starts[1:]))
        boundaries = frame_boundaries(parts, sample_pts, frame_pts, cutoff)
        return boundaries, {'solver': 'kmeans_runs', 'requested_k': cfg['kmeans_k'],
                            'actual_k': len(set(map(int, labels))), 'continuous_runs': len(parts)}
    raise ValueError('Unsupported comparison solver')
