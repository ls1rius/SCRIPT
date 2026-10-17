"""Original PTS video access; no timestamp inferred from an average frame rate."""
import hashlib
from pathlib import Path

from .config import digest
from .segmentation import sample_indices
from .state import obj, write_json


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(4*1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def visual_quality(image):
    """Reject essentially black pixels, without treating static scenes as missing."""
    extrema = image.convert('RGB').getextrema()
    maximum = max(high for _, high in extrema)
    return {'visual_usable': maximum > 2,
            'visual_quality': {'rule': 'rgb_max_gt_2_v1', 'maximum_channel_value': maximum}}


def decode_packets(container, stream):
    """Recover at packet boundaries; callers retain every reported error and source timestamp."""
    import av
    for packet in container.demux(stream):
        timestamp = float(packet.pts*packet.time_base) if packet.pts is not None and packet.time_base else None
        try:
            yield timestamp, packet.decode(), None
        except av.error.FFmpegError as exc:
            yield timestamp, [], str(exc)


def inspect_video(item, cfg):
    import av
    import math
    uri = Path(item['uri']).resolve()
    if not uri.is_file() or not item.get('video_id'):
        raise ValueError('build_failed: video_id and existing video URI are required')
    try:
        container = av.open(str(uri))
    except av.error.FFmpegError as exc:
        raise ValueError(f'build_failed: cannot open video: {exc}') from exc
    with container:
        if not container.streams.video:
            raise ValueError('build_failed: no video stream')
        stream = container.streams.video[0]
        duration = (float(stream.duration*stream.time_base) if stream.duration is not None
                    else float(container.duration/av.time_base) if container.duration is not None else None)
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise ValueError('build_failed: invalid video duration')
        cutoff = float(item.get('cutoff', duration))
        if not math.isfinite(cutoff) or not 0 < cutoff <= duration:
            raise ValueError('build_failed: invalid legal cutoff')
        good, packet_pts, errors = {}, set(), []
        for timestamp, decoded, error in decode_packets(container, stream):
            if timestamp is not None and 0 <= timestamp < cutoff:
                packet_pts.add(timestamp)
            if error:
                errors.append({'pts': timestamp, 'error': error})
            for frame in decoded:
                if frame.pts is None:
                    errors.append({'pts': None, 'error': 'Frame lacks presentation timestamp'})
                    continue
                pts = float(frame.pts*stream.time_base)
                if 0 <= pts < cutoff:
                    good[pts] = True
            # Packet decode ordering can differ from presentation ordering; finish buffered frames.
            if timestamp is not None and timestamp >= cutoff+2:
                break
        if not good:
            raise ValueError('build_failed: no decodable frames in legal prefix')
        catalog = sorted(set(good) | packet_pts)
        frames = [{'frame_id': f'{item["video_id"]}:F{i:09d}', 'pts': pts,
                   'missing': pts not in good, 'pts_source': 'frame' if pts in good else 'packet'}
                  for i, pts in enumerate(catalog)]
        missing = []
        start = None
        for entry in frames:
            if entry['missing'] and start is None:
                start = entry['pts']
            elif not entry['missing'] and start is not None:
                missing.append([start, entry['pts']])
                start = None
        if start is not None:
            missing.append([start, cutoff])
        unknown = any(e['pts'] is None for e in errors)
        if unknown:
            # Missing timestamps cannot be localized safely; preserve an explicit uncertain range.
            missing = [[0., cutoff]]
        subtitle = item.get('subtitle_uri')
        caption = item.get('caption_uri')
        return obj(f'{item["video_id"]}:manifest', video_id=item['video_id'], uri=str(uri),
                   media_hash=file_hash(uri), duration=duration, cutoff=cutoff, time_base=str(stream.time_base),
                   frames=frames, subtitle_uri=str(Path(subtitle).resolve()) if subtitle else None,
                   subtitle_hash=file_hash(subtitle) if subtitle else None,
                   caption_uri=str(Path(caption).resolve()) if caption else None,
                   caption_hash=file_hash(caption) if caption else None,
                   build_status='partial' if unknown or any(b-a > 2. for a, b in missing) else 'complete',
                   missing_intervals=missing, decode_errors=errors, config_hash=digest(cfg))


def load_captions(manifest):
    """Import unverified timed visual descriptions, keeping them separate from subtitles/pixels."""
    import json
    import math
    uri = manifest.get('caption_uri')
    if not uri or file_hash(uri) != manifest.get('caption_hash'):
        raise ValueError('Provided captions require an intact caption_uri and content hash')
    source = json.loads(Path(uri).read_text())
    if isinstance(source, dict) and set(source)-{'spans', 'producer'}:
        raise ValueError('Caption files accept only spans and producer metadata')
    rows = source['spans'] if isinstance(source, dict) else source
    if not isinstance(rows, list):
        raise ValueError('Captions must be a list of timed text spans')
    result, previous = [], -1.
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'start', 'end', 'text'}:
            raise ValueError('Caption spans accept only start, end and text')
        a, b = row['start'], row['end']
        if (type(a) not in (int, float) or type(b) not in (int, float)
                or not math.isfinite(a) or not math.isfinite(b) or not 0 <= a < b
                or a < previous or not isinstance(row['text'], str)):
            raise ValueError('Captions must be monotonic finite timed text')
        previous = a
        if b > manifest['cutoff'] and manifest['cutoff'] < manifest['duration']:
            # A description generated from a span crossing a restricted prefix can leak the future.
            continue
        end = min(b, manifest['cutoff'])
        if a < end:
            result.append({'start': a, 'end': end, 'text': row['text']})
    return {'data': result, 'producer': source.get('producer') if isinstance(source, dict) else None}


def read_frames(manifest, interval, output, fps=None, count=None, longest_side=768, crop=None):
    import av
    pts = [f['pts'] for f in manifest['frames']]
    if not 0 <= interval[0] < interval[1] <= manifest['cutoff']:
        raise ValueError('Decode interval exceeds legal prefix')
    indices = sample_indices(pts, interval, fps=fps, count=count)
    selected = {manifest['frames'][i]['pts']: manifest['frames'][i] for i in indices
                if not manifest['frames'][i].get('missing', False)}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    result, pending = [], {}
    for timestamp, entry in selected.items():
        key = digest([manifest['media_hash'], manifest['cutoff'], entry, longest_side, crop])
        path = output / f'{key}.png'
        fingerprint = path.with_suffix('.json')
        trusted = False
        if path.exists() and fingerprint.exists():
            import json
            try:
                saved = json.loads(fingerprint.read_text())
                trusted = saved.get('cache_key') == key and saved.get('content_hash') == file_hash(path)
            except (ValueError, OSError):
                trusted = False
        if trusted:
            from PIL import Image
            with Image.open(path) as image:
                w, h = image.size
                quality = visual_quality(image)
            result.append(dict(entry, width=w, height=h, crop=crop, path=str(path), content_hash=file_hash(path), **quality))
        else:
            pending[timestamp] = (entry, path)
    if pending:
        with av.open(manifest['uri']) as container:
            stream = container.streams.video[0]
            # Seek conservatively; exact PTS selection remains authoritative.
            if interval[0] > 1:
                container.seek(int(interval[0]/float(stream.time_base)), stream=stream, backward=True)
            for _, decoded, _ in decode_packets(container, stream):
              for frame in decoded:
                if frame.pts is None:
                    continue
                timestamp = float(frame.pts*stream.time_base)
                if timestamp >= interval[1]:
                    continue
                if timestamp not in pending:
                    continue
                entry, path = pending.pop(timestamp)
                image = frame.to_image().convert('RGB')
                if crop:
                    w, h = image.size
                    image = image.crop((round(crop[0]*w), round(crop[1]*h), round(crop[2]*w), round(crop[3]*h)))
                if longest_side:
                    scale = min(1., longest_side/max(image.size))
                    image = image.resize((max(1, round(image.width*scale)), max(1, round(image.height*scale))))
                # PNG compression changes file size/CPU time, not decoded evidence pixels.
                image.save(path, compress_level=1)
                pixel_hash = file_hash(path)
                write_json(path.with_suffix('.json'), {'cache_key': digest([manifest['media_hash'], manifest['cutoff'],
                           entry, longest_side, crop]), 'content_hash': pixel_hash})
                result.append(dict(entry, width=image.width, height=image.height, crop=crop, path=str(path),
                                   content_hash=pixel_hash, **visual_quality(image)))
                if not pending:
                    break
              if not pending:
                  break
    if pending:
        raise ValueError('Requested original frames could not be decoded')
    return sorted(result, key=lambda f: f['pts'])


def load_subtitles(manifest, interval):
    import json
    if not manifest['subtitle_uri']:
        return []
    if file_hash(manifest['subtitle_uri']) != manifest['subtitle_hash']:
        raise ValueError('Subtitle content changed since build')
    rows = json.loads(Path(manifest['subtitle_uri']).read_text())
    out, previous = [], -1.
    for row in rows:
        a, b = float(row['start']), float(row['end'])
        if a < previous or a < 0 or b <= a or not isinstance(row['text'], str):
            raise ValueError('Subtitles must be monotonic timed text spans')
        previous = a
        start, end = max(interval[0], a), min(interval[1], b, manifest['cutoff'])
        if start < end:
            out.append({'start': start, 'end': end, 'text': row['text']})
    return out


def shot_boundaries(manifest, cfg):
    """Full original-frame RGB histogram cuts, explicitly separate from feature-scene DP."""
    import av
    import numpy as np
    from .segmentation import boundaries_at_times
    previous, cuts, last_cut = None, [], 0.
    with av.open(manifest['uri']) as container:
        stream = container.streams.video[0]
        for _, decoded, _ in decode_packets(container, stream):
            for frame in decoded:
                if frame.pts is None:
                    continue
                pts = float(frame.pts*stream.time_base)
                if pts >= manifest['cutoff']:
                    break
                if pts < 0:
                    continue
                pixels = np.asarray(frame.to_image().convert('RGB').resize((96, 96))).reshape(-1, 3)
                hist = np.histogramdd(pixels, bins=8, range=[(0, 256)]*3)[0].ravel()
                hist = np.sqrt(hist/hist.sum())
                distance = float(np.linalg.norm(hist-previous)/np.sqrt(2)) if previous is not None else 0.
                if distance >= cfg['shot_histogram_threshold'] and pts-last_cut >= cfg['shot_min_seconds']:
                    cuts.append(pts)
                    last_cut = pts
                previous = hist
            if decoded and decoded[-1].pts is not None and float(decoded[-1].pts*stream.time_base) >= manifest['cutoff']:
                break
    boundaries = boundaries_at_times(cuts, [f['pts'] for f in manifest['frames']], manifest['cutoff'])
    return {'data': boundaries, 'metadata': {'solver': 'shot_histogram', 'cut_pts': cuts,
                                             'threshold': cfg['shot_histogram_threshold']}}
