#!/usr/bin/env python3
"""
LLM-as-judge：对 Agent 产出的代码按 rubric 评「事实正确率」与「证据充分度」。

确定性 grader（单元测试）优先，本模块只补足开放结果的质量判断（见 docs/agent-eval.md §2.3/§2.4）。

实现方式：复用 pi agent（与 Ralph 同一后端）作为裁判模型，强约束输出 JSON。

用法：
  python3 scripts/ralph/judge.py --tasks scripts/ralph/benchmark/example-tasks \
      --sandbox scripts/ralph/benchmark/sandbox
  python3 scripts/ralph/judge.py --run scripts/ralph/benchmark/runs/<run-id>
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_TASKS = SCRIPT_DIR / "benchmark" / "example-tasks"

MAX_FILE_CHARS = 8000
MAX_TOTAL_CHARS = 60000


# ══════════════════════════════════════════════════════════════
#  纯函数（可单元测试）
# ══════════════════════════════════════════════════════════════

def _gather_files(root: Path) -> list[tuple[str, str]]:
    """收集目录下的文本文件（相对路径 → 内容），跳过二进制/占位。"""
    out: list[tuple[str, str]] = []
    if not root.exists():
        return out
    total = 0
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.name == ".gitkeep":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue  # 二进制跳过
        rel = str(f.relative_to(root))
        text = text[:MAX_FILE_CHARS]
        total += len(text)
        if total > MAX_TOTAL_CHARS:
            break
        out.append((rel, text))
    return out


def build_judge_prompt(tasks_dir: Path, sandbox_dir: Path) -> str:
    """构造裁判 prompt：任务验收标准 + 产出文件。"""
    tasks = _gather_files(tasks_dir)
    files = _gather_files(sandbox_dir)

    parts: list[str] = []
    parts.append("你是严格的代码评审裁判。下面给出若干开发任务的验收标准，以及 AI Agent 的产出文件。")
    parts.append("\n## 任务（含验收标准）\n")
    for name, text in tasks:
        parts.append(f"### {name}\n{text}\n")
    parts.append("\n## Agent 产出文件\n")
    if files:
        for name, text in files:
            parts.append(f"===== {name} =====\n{text}\n")
    else:
        parts.append("（无产出文件）\n")

    parts.append(
        "\n## 评分要求\n"
        "只输出一个 JSON 对象，不要任何额外文字或代码块标记，字段：\n"
        '- "factual_correctness": 0~1 的小数，产出是否在事实与逻辑上满足验收标准\n'
        '- "groundedness": 0~1 的小数，是否有真实证据支撑（确实实现/测试通过），而非伪造或空洞\n'
        '- "reason": 一句话中文理由\n'
    )
    return "\n".join(parts)


def parse_judge_output(text: str) -> dict | None:
    """从裁判输出里提取 JSON 对象。兼容被代码块包裹或含多余文字的情况。"""
    if not text:
        return None
    # 去掉 ```json ... ``` 包裹
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = m.group(0) if m else None
    if candidate is None:
        return None
    try:
        data = json.loads(candidate)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for k in ("factual_correctness", "groundedness"):
        try:
            out[k] = max(0.0, min(1.0, float(data.get(k))))
        except (TypeError, ValueError):
            out[k] = None
    out["reason"] = str(data.get("reason", ""))[:300]
    return out


# ══════════════════════════════════════════════════════════════
#  调用 pi 裁判
# ══════════════════════════════════════════════════════════════

def run_pi(prompt: str, model: str | None = None, timeout: int = 180) -> str:
    cmd = ["pi", "--mode", "text", "--print", "--no-session", "--approve"]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              cwd=str(PROJECT_ROOT), timeout=timeout)
        return proc.stdout
    except Exception as e:  # noqa: BLE001
        print(f"pi 调用失败：{e}", file=sys.stderr)
        return ""


def judge(tasks_dir: Path, sandbox_dir: Path, model: str | None = None) -> dict:
    prompt = build_judge_prompt(tasks_dir, sandbox_dir)
    raw = run_pi(prompt, model=model)
    result = parse_judge_output(raw)
    if result is None:
        return {"factual_correctness": None, "groundedness": None,
                "reason": "裁判输出无法解析", "raw": raw[:300]}
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LLM-as-judge（事实正确率 / 证据充分度）")
    p.add_argument("--tasks", default=str(DEFAULT_TASKS), help="任务目录")
    p.add_argument("--sandbox", default=None, help="产出目录（与 --run 二选一）")
    p.add_argument("--run", default=None, help="运行归档目录（自动取其中的 sandbox）")
    p.add_argument("--model", default=None, help="裁判使用的模型")
    p.add_argument("--json", dest="json_out", default=None, help="结果 JSON 输出路径")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tasks_dir = Path(args.tasks)
    if args.run:
        sandbox = Path(args.run) / "sandbox"
    elif args.sandbox:
        sandbox = Path(args.sandbox)
    else:
        print("需要 --sandbox 或 --run", file=sys.stderr)
        return 2

    result = judge(tasks_dir, sandbox, model=args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
