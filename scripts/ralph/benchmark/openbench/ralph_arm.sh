#!/usr/bin/env bash
# OpenBench → Ralph harness 适配 wrapper
#
# 由 obench 的 manifest 调用：
#   bash ralph_arm.sh {workspace} {prompt}
# 运行目录（cwd）已经是 {workspace}，即 OpenBench 为该 cell 复刻的任务工作区。
#
# 思路：Ralph 引擎把 PROJECT_ROOT 固定在 SCRIPT_DIR 的上两级，并把 pi 的
# cwd 设为 PROJECT_ROOT。所以要让它编辑 OpenBench 的工作区，必须把工具链
# 注入到 {workspace}/scripts/ralph，跑完再把注入物清理干净，供 checker 评。
#
# 环境变量（可选）：
#   RALPH_MAX_ITERS   最大迭代次数（默认 2：给验证失败一次重试机会）
#   RALPH_ENABLE_PM   设为 1 则启用 PM 治理（单任务对照默认关闭）
#   RALPH_NO_LANGFUSE 默认 1，关闭 Langfuse 上报，保持对照干净
set -uo pipefail

WS="${1:?usage: ralph_arm.sh WORKSPACE PROMPT}"
PROMPT="${2:?usage: ralph_arm.sh WORKSPACE PROMPT}"
WS="$(cd "$WS" && pwd)"

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_ROOT="$(cd "$SELF_DIR/../../../.." && pwd)"
RALPH_SRC="$RALPH_ROOT/scripts/ralph"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then echo "[ralph_arm] python3 not found" >&2; exit 127; fi

TOOL="$WS/scripts/ralph"
DOCS_EXISTED=0; [ -d "$WS/docs" ] && DOCS_EXISTED=1
CREATED_AGENTS=0
CREATED_GIT=0
SCRIPTS_EXISTED=0; [ -d "$WS/scripts" ] && SCRIPTS_EXISTED=1

# ── 0. 隔离 pi 配置：用户自定义扩展（如 pi-cache-lab）在 -p 非交互模式下可能加载失败
#      并让 pi 直接退出。用独立 agent 目录，只带入 auth 与模型目录、写干净的 settings。──
PI_ISO="$(mktemp -d "${TMPDIR:-/tmp}/ralph_pi_agent.XXXXXX")"
REAL_AGENT="$HOME/.pi/agent"
[ -f "$REAL_AGENT/auth.json" ] && cp "$REAL_AGENT/auth.json" "$PI_ISO/auth.json"
[ -f "$REAL_AGENT/models-store.json" ] && cp "$REAL_AGENT/models-store.json" "$PI_ISO/models-store.json"
printf '{"defaultProvider":"%s","defaultModel":"%s"}\n' \
  "${RALPH_PI_PROVIDER:-opencode-go}" "${RALPH_PI_MODEL:-deepseek-v4.1-flash}" > "$PI_ISO/settings.json"
export PI_CODING_AGENT_DIR="$PI_ISO"
export PI_TELEMETRY=0
unset PI_PACKAGE_DIR PI_CODING_AGENT_SESSION_DIR 2>/dev/null || true

cleanup() {
  rm -rf "$TOOL" "$PI_ISO"
  [ "$SCRIPTS_EXISTED" = 0 ] && rmdir "$WS/scripts" 2>/dev/null
  [ "$CREATED_AGENTS" = 1 ] && rm -f "$WS/AGENTS.md"
  [ "$CREATED_GIT" = 1 ] && rm -rf "$WS/.git"
  [ "$DOCS_EXISTED" = 0 ] && rmdir "$WS/docs" 2>/dev/null
  rm -f "$WS"/docs/patterns-*.md 2>/dev/null
}
trap cleanup EXIT

# ── 1. 注入 Ralph 工具链 ──
if [ -e "$TOOL" ]; then
  echo "[ralph_arm] $TOOL 已存在，拒绝覆盖工作区" >&2
  exit 11
fi
mkdir -p "$TOOL/tasks/done"
for f in ralph.py DEVELOPER.md VALIDATOR.md PM.md; do
  cp "$RALPH_SRC/$f" "$TOOL/$f" || { echo "[ralph_arm] 缺少 $RALPH_SRC/$f" >&2; exit 12; }
done

# ── 2. AGENTS.md（Ralph 启动前置要求）──
if [ ! -e "$WS/AGENTS.md" ] && [ ! -e "$WS/CLAUDE.md" ]; then
  printf '# OpenBench workspace\n\n本工作区由 Ralph harness 接管，当前任务见 `scripts/ralph/tasks/`。\n' > "$WS/AGENTS.md"
  CREATED_AGENTS=1
fi

# ── 3. 最小 git 仓库（DEVELOPER.md 要求提交，OpenBench 工作区默认无 .git）──
if [ ! -d "$WS/.git" ]; then
  git init -q "$WS" 2>/dev/null
  git -C "$WS" -c user.email=ralph@bench.local -c user.name=Ralph add -A >/dev/null 2>&1 || true
  git -C "$WS" -c user.email=ralph@bench.local -c user.name=Ralph commit -q -m "benchmark baseline" >/dev/null 2>&1 || true
  CREATED_GIT=1
fi

# ── 4. 由 instruction 生成单个 Ralph 任务 ──
TASK_FILE="$TOOL/tasks/ob001-openbench.md"
{
  printf '%s\n' '---'
  printf '%s\n' 'id: "ob001"'
  printf '%s\n' 'title: "OpenBench task"'
  printf '%s\n' 'priority: 1'
  printf '%s\n' '---'
  printf '\n## 任务描述\n\n'
  printf '%s\n' "$PROMPT"
  printf '\n## 验收标准\n\n'
  printf '%s\n' '- [ ] 完成任务描述中的全部要求，并满足其 “Done when” 条件'
  printf '%s\n' '- [ ] 不破坏既有测试；除任务明确要求外不修改测试文件'
} > "$TASK_FILE"

# ── 5. 运行 Ralph（Developer + Validator；PM 默认关闭）──
MAXIT="${RALPH_MAX_ITERS:-2}"
PM_FLAG="--no-pm"
[ "${RALPH_ENABLE_PM:-0}" = "1" ] && PM_FLAG=""
export RALPH_NO_LANGFUSE="${RALPH_NO_LANGFUSE:-1}"
export RALPH_RUN_ID="obench-$(date +%s)-$$"

cd "$WS"
"$PY" "$TOOL/ralph.py" pi --no-dashboard --max-iterations "$MAXIT" $PM_FLAG
RC=$?
echo "[ralph_arm] ralph exit=$RC"

# harness “跑完”即可，任务成败交给 checker；只有崩溃(>1)才算 harness 自身失败
if [ "$RC" = 0 ] || [ "$RC" = 1 ]; then exit 0; fi
exit "$RC"
