#!/bin/bash
# Submit the standard smoke test with no arguments, or a custom demo.py command.
set -euo pipefail
VIDEOTREE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$VIDEOTREE_ROOT"
mkdir -p workdirs/videotree-smoke/jobs
QUERY_TAG="$(TZ=Asia/Singapore date +%Y%m%d-%H%M%S)-$$"
LOG_PATH="$PWD/workdirs/videotree-smoke/jobs/query-$QUERY_TAG.log"
# Create the file now so tail -F can be started while the job is queued.
touch "$LOG_PATH"
if (( $# )); then
    VIDEOTREE_ARGS_FILE="$PWD/workdirs/videotree-smoke/jobs/query-$QUERY_TAG.json"
    python - "$VIDEOTREE_ARGS_FILE" "$@" <<'PY'
import json
from pathlib import Path
import sys
Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:], ensure_ascii=False) + '\n')
PY
    JOB_ID="$(qsub -o "$LOG_PATH" \
        -v "VIDEOTREE_ROOT=$VIDEOTREE_ROOT,VIDEOTREE_ARGS_FILE=$VIDEOTREE_ARGS_FILE" script/gpu_smoke.pbs)"
else
    JOB_ID="$(qsub -o "$LOG_PATH" -v "VIDEOTREE_ROOT=$VIDEOTREE_ROOT" script/gpu_smoke.pbs)"
fi
printf '已提交 GPU 作业：%s\n' "$JOB_ID"
printf '当前状态（Q=排队，R=运行，F=结束）：\n'
qstat "$JOB_ID" || true
printf '\n查看排队原因或结束状态：\n  qstat -xf %s\n' "$JOB_ID"
printf '\n查看日志（排队时日志可能为空）：\n  tail -F %q\n' "$LOG_PATH"
if (( $# )); then
    printf '\n成功完成后，结果路径会显示在日志末尾的 SUMMARY 行。\n'
else
    printf '\n成功完成后更新以下结果文件：\n'
    printf '  %s\n' "$PWD/workdirs/videotree-smoke/summary.json"
    printf '  %s\n' "$PWD/workdirs/videotree-smoke/custom-query/summary.json"
fi
printf '当前命令只提交一次任务并返回；排队或运行时无需重复提交。\n'
