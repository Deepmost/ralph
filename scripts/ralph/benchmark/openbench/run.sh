#!/usr/bin/env bash
# OpenBench 对照运行入口：单 pi agent vs Ralph harness
#
# 用法: run.sh <doctor|smoke|full|report|all>
#
# 环境变量（可选）:
#   OBENCH_DIR   obench checkout 路径（默认 ~/Documents/whaty-project/openbench）
#   OB_MODEL     模型名（默认 deepseek-v4.1-flash）
#   OB_TRIALS    正式对比每 cell 重复次数（默认 3）
#   OB_TIMEOUT   每 cell 超时秒数（默认 900）
set -euo pipefail

OB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OBENCH_DIR="${OBENCH_DIR:-/Users/deepmost/Documents/whaty-project/openbench}"
MODEL="${OB_MODEL:-deepseek-v4.1-flash}"
TRIALS="${OB_TRIALS:-3}"
TIMEOUT="${OB_TIMEOUT:-900}"

OBENCH="$OBENCH_DIR/.venv/bin/obench"
[ -x "$OBENCH" ] || { echo "找不到 $OBENCH；先按 README 安装 obench" >&2; exit 1; }

TASKS="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
mkdir -p "$OB/runs"

doctor() {
  "$OBENCH" doctor --candidate "$OB/arms/pi-ocgo.toml" --candidate "$OB/arms/ralph.toml" \
    --model "$MODEL"
}

smoke() {
  echo "── 负控 null（应全 fail）──"
  "$OBENCH" legacy run --harness null --task fix-failing-test --trials 1 \
    --results-path "$OB/runs/smoke-null.jsonl"
  echo "── 单 pi 冒烟 ──"
  "$OBENCH" legacy run --candidate "$OB/arms/pi-ocgo.toml" \
    --task fix-failing-test --model "$MODEL" --trials 1 --timeout 600 \
    --results-path "$OB/runs/smoke-pi.jsonl"
  echo "── Ralph 冒烟 ──"
  "$OBENCH" legacy run --candidate "$OB/arms/ralph.toml" \
    --task fix-failing-test --model "$MODEL" --trials 1 --timeout 600 \
    --results-path "$OB/runs/smoke-ralph.jsonl"
}

full() {
  "$OBENCH" legacy run \
    --candidate "$OB/arms/pi-ocgo.toml" --candidate "$OB/arms/ralph.toml" \
    --task "$TASKS" --model "$MODEL" --trials "$TRIALS" --timeout "$TIMEOUT" \
    --results-path "$OB/runs/results.jsonl" --transcripts-dir "$OB/transcripts"
}

report() {
  "$OBENCH" report --efficiency --results-path "$OB/runs/results.jsonl"
}

case "${1:-all}" in
  doctor) doctor ;;
  smoke)  smoke ;;
  full)   full ;;
  report) report ;;
  all)    doctor && smoke && full && report ;;
  *) echo "用法: run.sh <doctor|smoke|full|report|all>" >&2; exit 2 ;;
esac
