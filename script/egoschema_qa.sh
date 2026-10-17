#!/bin/bash
set -euo pipefail
python main_qa.py --output_filename standard_qa.json \
    --output_base_path workdirs/videotree-smoke \
    --tree_node_idx workdirs/videotree-smoke/depth_expansion_res.json \
    --model checkpoints/Qwen/Qwen2.5-VL-7B-Instruct \
    --prompt_type qa_standard --temperature 0.0 "$@"
