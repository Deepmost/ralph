#!/usr/bin/env python3
"""
Ralph 评测指标计算模块。

包含两类目前在引擎其他脚本中缺失的口径（见 docs/agent-eval.md）：

1. 一致性与可靠性：`pass@k` / `pass^k`
   - pass@k：同一任务 k 次运行中**至少一次**成功（能力）
   - pass^k：k 次**全部**成功（可靠性）
2. 证据层：运行中是否真的执行了验证/测试（过程证据），作为 groundedness 的确定性代理。

这些函数是纯计算，便于单元测试，不联网、不调 LLM。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ralph  # noqa: E402


# ══════════════════════════════════════════════════════════════
#  pass@k / pass^k
# ══════════════════════════════════════════════════════════════

def task_outcomes(run_dir: Path) -> dict[str, bool]:
    """读取一次运行归档，返回 {task_id: 是否成功}。

    成功定义：任务在 done/ 且未被标记 blocked。
    """
    tasks_dir = run_dir / "tasks"
    done_dir = tasks_dir / "done"
    outcomes: dict[str, bool] = {}
    for loc, d in (("pending", tasks_dir), ("done", done_dir)):
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            m = ralph.parse_frontmatter(f)
            tid = str(m.get("id", f.stem))
            blocked = bool(m.get("blocked", False))
            success = (loc == "done") and not blocked
            outcomes[tid] = outcomes.get(tid, False) or success
    return outcomes


def aggregate_outcomes(outcomes_list: list[dict[str, bool]]) -> dict[str, list[bool]]:
    """把多次运行的结果聚合成 {task_id: [每次是否成功]}。"""
    tasks: set[str] = set()
    for o in outcomes_list:
        tasks.update(o.keys())
    return {t: [o.get(t, False) for o in outcomes_list] for t in sorted(tasks)}


def pass_at_k(matrix: dict[str, list[bool]]) -> float:
    """pass@k：至少一次成功的任务占比（%）。"""
    if not matrix:
        return 0.0
    return round(sum(any(v) for v in matrix.values()) / len(matrix) * 100, 1)


def pass_power_k(matrix: dict[str, list[bool]]) -> float:
    """pass^k：k 次全部成功的任务占比（%）。空序列视为未成功。"""
    if not matrix:
        return 0.0
    return round(sum(bool(v) and all(v) for v in matrix.values()) / len(matrix) * 100, 1)


def compute_pass_metrics(run_dirs: list[Path]) -> dict:
    """对同一配置的多次运行归档计算 pass@k / pass^k。"""
    outcomes = [task_outcomes(d) for d in run_dirs]
    matrix = aggregate_outcomes(outcomes)
    return {
        "k": len(run_dirs),
        "tasks": len(matrix),
        "pass_at_k": pass_at_k(matrix),
        "pass_power_k": pass_power_k(matrix),
        "matrix": matrix,
    }


# ══════════════════════════════════════════════════════════════
#  证据层：验证行为
# ══════════════════════════════════════════════════════════════

# 引擎把工具调用格式化为 "[Tool] bash: <command>"，据此统计是否执行过验证命令
_VERIFY_RE = re.compile(
    r"\[Tool\] bash:.*\b(unittest|pytest|npm test|npm run test|typecheck|lint|jest|go test|cargo test)\b",
    re.IGNORECASE,
)


def evidence_metrics(run_dir: Path) -> dict:
    """统计一次运行中的验证证据（是否跑过测试/检查）。"""
    log = run_dir / "run.log"
    verification_commands = 0
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if _VERIFY_RE.search(line):
                verification_commands += 1
    return {
        "verification_commands": verification_commands,
        "has_verification": verification_commands > 0,
    }


# ══════════════════════════════════════════════════════════════
#  工具正确率（工具选择 / 调用错误）
# ══════════════════════════════════════════════════════════════

# 引擎把工具调用格式化为 "[Tool] <name>: ..."；错误为 "[Tool Error] <name>"
_TOOL_CALL_RE = re.compile(r"\[Tool\]\s+(\w+)\s*:")
_TOOL_ERROR_RE = re.compile(r"\[Tool Error\]\s+(\w+)")


def tool_metrics(run_dir: Path) -> dict:
    """
    统计一次运行的工具调用与错误。

    - 工具选择：各工具调用次数分布
    - 工具错误率：工具执行失败占比（ToolFailBench 意义上的失败信号）
    - used_write_or_edit：是否真正改动了文件（防止"只读不改就宣称完成"）
    """
    log = run_dir / "run.log"
    calls: dict[str, int] = {}
    errors = 0
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _TOOL_CALL_RE.search(line)
            if m:
                calls[m.group(1)] = calls.get(m.group(1), 0) + 1
            if _TOOL_ERROR_RE.search(line):
                errors += 1
    total = sum(calls.values())
    return {
        "tool_calls": dict(sorted(calls.items(), key=lambda kv: -kv[1])),
        "total_tool_calls": total,
        "tool_errors": errors,
        "tool_error_rate": round(errors / total * 100, 1) if total else 0.0,
        "used_write_or_edit": bool(calls.get("write") or calls.get("edit")),
    }


# ══════════════════════════════════════════════════════════════
#  成本聚合（从 run.log 的 [Turn] 费用 提取，绕过 Langfuse 列表接口限制）
# ══════════════════════════════════════════════════════════════

_COST_RE = re.compile(r"\[Turn\]\s*费用:\s*\$([0-9.]+)")


def cost_metrics(run_dir: Path) -> dict:
    """从一次运行的 stdio 日志里汇总模型费用（美元）。"""
    log = run_dir / "run.log"
    total = 0.0
    turns = 0
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _COST_RE.search(line)
            if m:
                total += float(m.group(1))
                turns += 1
    return {"cost_total": round(total, 6), "cost_turns": turns}
