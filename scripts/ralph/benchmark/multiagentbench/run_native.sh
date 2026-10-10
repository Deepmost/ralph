#!/usr/bin/env bash
# 用 opencode-go shim 原生跑 MARBLE 的一个配置。
# 用法: run_native.sh CONFIG_YAML
set -euo pipefail

MARBLE_DIR="${MARBLE_DIR:-/Users/deepmost/Documents/whaty-project/MARBLE}"
SHIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${1:?usage: run_native.sh CONFIG_YAML}"

# MARBLE 期望 cwd 是包目录 marble/（评估器按相对路径读 evaluator_prompts.json，
# 工作区/结果目录也相对于此）。导入 marble 需要仓库根在 PYTHONPATH 上。
cd "$MARBLE_DIR/marble"
PYTHONPATH="$SHIM_DIR:$MARBLE_DIR" \
OPENCODE_SESSION="${OPENCODE_SESSION:-marble-native}" \
"$MARBLE_DIR/.venv/bin/python" -c "
import logging, sys
logging.basicConfig(level=logging.INFO)
import marble_opencode_shim  # noqa: F401  (base/key + header injection)
sys.argv = ['marble', '--config_path', '${CONFIG}']
from marble.main import main
main()
"
