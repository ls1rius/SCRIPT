"""Extract the original EVA-CLIP-8B visual features with reusable CPU caches."""
import argparse
import json
from pathlib import Path
import torch
from PIL import Image
from transformers import AutoModel, CLIPImageProcessor
from tqdm import tqdm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="checkpoints/BAAI/EVA-CLIP-8B")
    parser.add_argument("--frames", default="data/EgoSchema/egoschema_frames")
    parser.add_argument("--output", default="data/EgoSchema/egoschema_features")
    parser.add_argument("--video-ids", nargs="+")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    args = parser.parse_args()
    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("EVA feature extraction must run inside a PBS GPU allocation.")
    root, output = Path(args.frames), Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    ids = args.video_ids or sorted(p.name for p in root.iterdir() if p.is_dir())
    pending = []
    for uid in ids:
        paths = sorted((root / uid).glob("*.jpg"), key=lambda p: int(p.stem))
        if not paths or [int(p.stem) for p in paths] != list(range(len(paths))):
            raise ValueError(f"Missing or non-contiguous frames for {uid}")
        target = output / f"{uid}.pt"
        info_path = output / f"{uid}.json"
        if target.exists() and info_path.exists():
            info = json.loads(info_path.read_text())
            cached = torch.load(target, map_location="cpu", weights_only=True)
            if info.get("model") == str(Path(args.model).resolve()) and cached.shape[0] == len(paths) and torch.isfinite(cached).all():
                print(f"FEATURE_CACHE {uid} shape={list(cached.shape)}", flush=True)
                continue
        pending.append((uid, paths))
    if not pending:
        return
    dtype = torch.float16 if args.device == "cuda" else torch.float32
    # These are the same CLIP normalization and resize settings as the original repository.
    processor = CLIPImageProcessor(size={"shortest_edge": 224}, crop_size={"height": 224, "width": 224})
    model = AutoModel.from_pretrained(args.model, torch_dtype=dtype, trust_remote_code=True,
                                      low_cpu_mem_usage=True).to(args.device).eval()
    for uid, paths in pending:
        batches = []
        for start in tqdm(range(0, len(paths), args.batch_size), desc=uid):
            images = []
            for path in paths[start:start + args.batch_size]:
                with Image.open(path) as image:
                    images.append(image.convert("RGB"))
            pixels = processor(images=images, return_tensors="pt").pixel_values.to(args.device, dtype=dtype)
            with torch.inference_mode():
                batch = model.encode_image(pixels)
            batches.append(batch.float().cpu())
        features = torch.cat(batches)
        if not torch.isfinite(features).all():
            raise ValueError(f"Non-finite EVA features for {uid}")
        temporary = output / f"{uid}.pt.tmp"
        torch.save(features, temporary)
        temporary.replace(output / f"{uid}.pt")
        info = {"model": str(Path(args.model).resolve()), "shape": list(features.shape),
                "fps": 1, "dtype": str(features.dtype), "frame_indices": list(range(len(paths)))}
        (output / f"{uid}.json").write_text(json.dumps(info, indent=2) + "\n")
        print(f"FEATURES {uid} shape={list(features.shape)}", flush=True)


if __name__ == "__main__":
    main()
