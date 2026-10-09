#!/bin/bash
# Ralph - AI Agent 循环执行器（pi agent 后端）
# Usage: ./ralph.sh [--tool pi] [max_iterations] [--no-dashboard] [--port PORT]
#        [--run-id ID] [--no-validator] [--no-pm] [--no-guard]
#
# 此脚本是 ralph.py 的薄启动入口，负责检查 Python 环境并透传参数。

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 检查 Python 3.10+
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Error: Python 3.10+ not found. Please install Python 3.10+."
  exit 1
fi

# 解析参数，将 --tool 映射为 ralph.py 的位置参数
AGENT="pi"
PY_ARGS=()

while [[ $# -gt 0 ]]; do
  case $1 in
    --tool)
      AGENT="$2"
      shift 2
      ;;
    --tool=*)
      AGENT="${1#*=}"
      shift
      ;;
    --no-dashboard)
      PY_ARGS+=("$1")
      shift
      ;;
    --port)
      PY_ARGS+=("$1" "$2")
      shift 2
      ;;
    --run-id)
      PY_ARGS+=("$1" "$2")
      shift 2
      ;;
    --no-validator|--no-pm|--no-guard)
      PY_ARGS+=("$1")
      shift
      ;;
    *)
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        PY_ARGS+=("--max-iterations" "$1")
      fi
      shift
      ;;
  esac
done

exec "$PYTHON" "$SCRIPT_DIR/ralph.py" "$AGENT" "${PY_ARGS[@]}"
