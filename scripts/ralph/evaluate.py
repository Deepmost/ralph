#!/usr/bin/env python3
"""
Ralph 项目评测总入口。

把整个项目的四个评测面聚合成一份报告：

  ① 确定性质量/安全 —— 单元测试（PM 越权防腐对抗性用例）
  ② 在线可观测       —— Langfuse（调用 / 工具 / 延迟 / 错误）
  ③ 对照实验         —— A/B（完整闭环 vs 单 Agent vs 无 PM vs 无防腐）
  ④ 口径定义         —— docs/agent-eval.md（本脚本只引用，不复制）

用法：
  python3 scripts/ralph/evaluate.py                 # 打印到终端
  python3 scripts/ralph/evaluate.py --out docs/eval-report.md
  python3 scripts/ralph/evaluate.py --days 30 --environment ralph
  python3 scripts/ralph/evaluate.py --skip-langfuse  # 无网/无凭据时
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import eval_langfuse  # noqa: E402
import ab_experiment  # noqa: E402

TESTS_DIR = SCRIPT_DIR / "tests"


# ══════════════════════════════════════════════════════════════
#  面 ①：确定性质量/安全（单元测试）
# ══════════════════════════════════════════════════════════════

def run_unit_tests() -> dict:
    if not TESTS_DIR.exists():
        return {"available": False}
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(TESTS_DIR), "-p", "test_*.py"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT))
    out = proc.stdout + proc.stderr
    total, failures = 0, 0
    for line in out.splitlines():
        if line.startswith("Ran ") and " test" in line:
            try:
                total = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        if line.startswith("FAILED"):
            try:
                failures = int(line.split("=")[-1].split(")")[1].strip().split()[0])
            except (IndexError, ValueError):
                failures = 1
    return {
        "available": True,
        "total": total,
        "failures": failures,
        "passed": max(total - failures, 0),
        "ok": proc.returncode == 0,
    }


# ══════════════════════════════════════════════════════════════
#  报告
# ══════════════════════════════════════════════════════════════

def build_project_report(unit: dict, obs: dict, ab_report: str,
                         window: dict, filters: dict) -> str:
    L: list[str] = []
    A = L.append
    A("# Ralph 项目评测报告")
    A("")
    A(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    A(f"- 可观测窗口：`{window.get('from','-')}` ~ `{window.get('to','-')}`")
    active = {k: v for k, v in filters.items() if v}
    A(f"- 过滤条件：`{active or '无（全部）'}`")
    A("- 口径定义：见 [`docs/agent-eval.md`](agent-eval.md)")
    A("")
    A("本报告由 `scripts/ralph/evaluate.py` 聚合四个评测面生成。")
    A("")

    # ① 确定性质量/安全
    A("## ① 确定性质量 / 安全（单元测试）")
    A("")
    if unit.get("available"):
        status = "✅ 通过" if unit["ok"] else "❌ 失败"
        A(f"- 用例总数：{unit['total']}，通过 {unit['passed']}，失败 {unit['failures']} — **{status}**")
    else:
        A("- 未找到测试文件 `scripts/ralph/tests/test_pm_guard.py`")
    A("")

    # ② 在线可观测
    A("## ② 在线可观测（Langfuse）")
    A("")
    if obs:
        A("| 指标 | 值 |")
        A("|---|---|")
        A(f"| observation / trace / session | {obs.get('observations')} / {obs.get('traces')} / {obs.get('sessions')} |")
        A(f"| 类型分布 | {obs.get('by_type')} |")
        A(f"| 错误率 | {obs.get('error_count')} / {obs.get('error_rate')}% |")
        A(f"| LLM 调用 | {obs.get('generations')} |")
        A(f"| 延迟 p50/p95/p99 (s) | {obs.get('gen_latency_p50')} / {obs.get('gen_latency_p95')} / {obs.get('gen_latency_p99')} |")
        A(f"| 工具调用 / 失败率 | {obs.get('tools')} / {obs.get('tool_error_rate')}% |")
        A(f"| 工具分布 | {obs.get('tool_names')} |")
        A(f"| 角色分布 | {obs.get('users')} |")
    else:
        A("- 未采集（无凭据 / 无网络 / 已跳过）")
    A("")

    # ③ 对照实验
    A("## ③ 对照实验（A/B）")
    A("")
    A(ab_report)
    A("")

    A("---")
    A("")
    A("## 评测面索引")
    A("")
    A("| 面 | 载体 | 命令 |")
    A("|---|---|---|")
    A("| ① 确定性质量/安全 | `tests/` | `python3 -m unittest discover -s scripts/ralph/tests` |")
    A("| ② 在线可观测 | `eval_langfuse.py` | `python3 scripts/ralph/eval_langfuse.py --environment ralph` |")
    A("| ③ 对照实验 | `ab_experiment.py` | `python3 scripts/ralph/ab_experiment.py --report` |")
    A("| ④ 口径定义 | `docs/agent-eval.md` | （文档） |")
    A("| **总入口** | `evaluate.py` | `python3 scripts/ralph/evaluate.py --out docs/eval-report.md` |")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ralph 项目评测总入口")
    p.add_argument("--days", type=int, default=7, help="可观测回看天数")
    p.add_argument("--environment", default="ralph", help="Langfuse environment 过滤")
    p.add_argument("--skip-langfuse", action="store_true", help="跳过可观测面（无网/无凭据）")
    p.add_argument("--out", default=None, help="报告输出路径")
    p.add_argument("--json", dest="json_out", default=None, help="原始指标 JSON 输出路径")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    unit = run_unit_tests()

    frm = eval_langfuse._iso(args.days)
    to = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    obs: dict = {}
    if not args.skip_langfuse:
        cfg = eval_langfuse.load_config()
        if cfg is None:
            print("跳过可观测面：未找到 Langfuse 凭据", file=sys.stderr)
        else:
            try:
                rows = eval_langfuse.fetch_observations(
                    cfg, frm, to, args.environment, None, None, 100, 5)
                obs = eval_langfuse.compute_metrics(rows)
            except Exception as e:
                print(f"可观测面采集失败：{e}", file=sys.stderr)

    ab_report = ab_experiment.build_report(ab_experiment.load_runs())

    report = build_project_report(
        unit, obs, ab_report,
        {"from": frm, "to": to},
        {"environment": args.environment},
    )
    print(report)

    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"\n报告已写入：{args.out}", file=sys.stderr)
    if args.json_out:
        import json
        Path(args.json_out).write_text(
            json.dumps({"unit": unit, "observability": obs,
                        "window": {"from": frm, "to": to}}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"指标 JSON 已写入：{args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
