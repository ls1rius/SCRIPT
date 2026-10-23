#!/usr/bin/env bash
# Run a real-video smoke on a workstation, without the old cluster's PBS setup.
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
export HF_HOME="$PROJECT_ROOT/checkpoints/.cache/huggingface"
export HF_XET_CACHE="$PROJECT_ROOT/checkpoints/.cache/xet"
export HF_MODULES_CACHE="$PROJECT_ROOT/checkpoints/.cache/modules"
export HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}"
export CUDA_DEVICE_ORDER=PCI_BUS_ID

if [[ -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
    # Select an idle physical device by UUID, including on mixed-GPU hosts.
    CUDA_VISIBLE_DEVICES="$(nvidia-smi --query-gpu=uuid,memory.used,utilization.gpu,memory.free --format=csv,noheader,nounits |
        awk -F, '$2 <= 500 && $3 <= 5 {print $1 "," $4}' |
        sort -t, -k2 -nr | head -1 | cut -d, -f1 | tr -d ' ')"
    if [[ -z "$CUDA_VISIBLE_DEVICES" ]]; then
        echo "No idle GPU available; existing jobs were left running." >&2
        exit 1
    fi
    export CUDA_VISIBLE_DEVICES
fi
PYTHON_BIN="${PYTHON_BIN:-$(conda info --base)/envs/vts/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Missing vts interpreter: $PYTHON_BIN" >&2
    exit 1
fi
MODE="${1:-script}"
if [[ $# -gt 0 ]]; then shift; fi
echo "Running $MODE with CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
case "$MODE" in
    script)
        exec "$PYTHON_BIN" -u script/run_local_four_arm.py --arm scheme3 "$@"
        ;;
    all)
        exec "$PYTHON_BIN" -u script/run_local_four_arm.py --arm all "$@"
        ;;
    videotree)
        exec "$PYTHON_BIN" -u demo.py --num-examples 1 \
            --video-id 0074f737-11cb-497d-8d07-77c3a8127391 \
            --output workdirs/videotree-local-smoke "$@"
        ;;
    *) echo "Usage: bash script/run_local_smoke.sh [all|script|videotree] [extra arguments]" >&2; exit 2 ;;
esac
