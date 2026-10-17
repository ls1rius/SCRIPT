"""Extract timestamp-indexed 1 FPS frames for selected EgoSchema examples."""
import argparse
import json
import math
from pathlib import Path
import cv2
from tqdm import tqdm


def extract_video(source, destination, expected_count=None):
    destination.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"Cannot read video: {source}")
    source_fps = capture.get(cv2.CAP_PROP_FPS)
    source_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if source_fps <= 0 or source_frames <= 0:
        raise ValueError(f"Invalid video metadata: {source}")
    available = math.ceil(source_frames / source_fps)
    count = expected_count if expected_count is not None else available
    manifest = destination / "frames.json"
    if manifest.exists():
        prior = json.loads(manifest.read_text())
        if prior.get("count") == count and all((destination / f"{i}.jpg").is_file() for i in range(count)):
            capture.release()
            return prior
    try:
        for second in range(count):
            frame_number = min(round(second * source_fps), source_frames - 1)
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ok, image = capture.read()
            if not ok:
                raise RuntimeError(f"Could not decode {source} at {second}s")
            if not cv2.imwrite(str(destination / f"{second}.jpg"), image):
                raise RuntimeError(f"Could not save frame {second}")
    finally:
        capture.release()
    info = {"source": str(source.resolve()), "source_fps": source_fps,
            "source_frames": source_frames, "fps": 1, "count": count,
            "indexing": "zero-based: frame i is sampled at i seconds"}
    manifest.write_text(json.dumps(info, indent=2) + "\n")
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data/EgoSchema/egoschema")
    parser.add_argument("--output", default="data/EgoSchema/egoschema_frames")
    parser.add_argument("--video-ids", nargs="*")
    parser.add_argument("--num-examples", type=int, default=3)
    args = parser.parse_args()
    root = Path(args.data_root)
    captions = json.loads((root / "lavila_subset.json").read_text())
    ids = args.video_ids or list(captions)[:args.num_examples]
    if any(uid not in captions for uid in ids):
        captions.update(json.loads((root / "lavila_fullset.json").read_text()))
    for uid in tqdm(ids):
        if uid not in captions:
            raise ValueError(f"No EgoSchema captions for {uid}")
        result = extract_video(root / "videos" / f"{uid}.mp4", Path(args.output) / uid, len(captions[uid]))
        print(f"FRAMES {uid} count={result['count']}", flush=True)


if __name__ == "__main__":
    main()
