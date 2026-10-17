#!/bin/bash
set -euo pipefail
python adaptive_breath_expansion.py --output_filename relevance_score.json \
    --output_base_path workdirs/videotree-smoke \
    --model checkpoints/Qwen/Qwen2.5-VL-7B-Instruct \
    --prompt_type cap_score --frame_feat_path data/EgoSchema/egoschema_features \
    --disable_eval "$@"
