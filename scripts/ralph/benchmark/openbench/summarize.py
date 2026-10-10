#!/usr/bin/env python3
"""汇总 OpenBench 对照结果：单 pi agent vs Ralph harness。

读取 results.jsonl（每行一个 cell），按 run_id 去重（保留最后一次），
输出 Markdown 对照表 + pass@k / pass^k 可靠性指标。

用法:
    python3 summarize.py [results.jsonl] [--out report.md]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import statistics
from pathlib import Path


def load(path: Path) -> list[dict]:
    by_rid: dict[str, dict] = {}
    order: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        rid = row.get("run_id") or f"{row.get('harness')}:{row.get('task')}:{row.get('trial')}"
        if rid not in by_rid:
            order.append(rid)
        by_rid[rid] = row  # last wins
    return [by_rid[r] for r in order]


def _arm(row: dict) -> str:
    return str(row.get("harness", "?")).split("@", 1)[0]


def _pi_cell_tokens_cost(path: Path) -> tuple[int, float]:
    """从 pi 臂的原始 JSONL transcript 累计 (totalTokens, cost.total)。"""
    tok = cost = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "turn_end":
            usage = (ev.get("message") or {}).get("usage") or {}
            tok += usage.get("totalTokens") or 0
            cost += (usage.get("cost") or {}).get("total") or 0
    return tok, cost


def _ralph_cell_cost(path: Path) -> float:
    """从 Ralph 臂 transcript 的 `[Turn] 费用: $x` 行累计单轮成本（剔除异常值）。"""
    vals = [float(x) for x in re.findall(r"费用: \$([0-9.]+)",
                                         path.read_text(encoding="utf-8", errors="replace"))]
    return sum(v for v in vals if v < 1.0)  # 单轮 >$1 视为 provider 误报


def cost_section(transcripts: Path, arms: list[str], tasks: list[str]) -> list[str]:
    # 文件名形如 pi-ocgo_<digest>_<task>_<model>_trialN.txt / ralph_<digest>_<task>_...
    per: dict[tuple[str, str], list[tuple[int, float]]] = collections.defaultdict(list)
    for f in glob.glob(str(transcripts / "**" / "*.txt"), recursive=True):
        base = os.path.basename(f)
        parts = base.split("_")
        if len(parts) < 3:
            continue
        arm, task = parts[0], parts[2]
        if arm not in arms or task not in tasks:
            continue
        p = Path(f)
        tok, cost = _pi_cell_tokens_cost(p) if arm.startswith("pi") else (0, _ralph_cell_cost(p))
        per[(arm, task)].append((tok, cost))

    out = ["## 成本 / token（读 transcript 推算）\n"]
    out.append("| 任务 | " + " | ".join(
        f"{a} token(中位) | {a} 成本$(中位)" for a in arms) + " |")
    out.append("|" + "---|" * (1 + 2 * len(arms)))
    totals: dict[str, float] = collections.defaultdict(float)
    for t in tasks:
        cells = []
        for a in arms:
            vals = per.get((a, t), [])
            tok = int(statistics.median([v[0] for v in vals])) if vals else 0
            cost = statistics.median([v[1] for v in vals]) if vals else 0.0
            totals[a] += cost
            cells.append(f"{tok:,}" if tok else "-")
            cells.append(f"{cost:.4f}")
        out.append(f"| {t} | " + " | ".join(cells) + " |")
    out.append("| **合计** | " + " | ".join(f"- | **{totals[a]:.4f}**" for a in arms) + " |")
    out.append("")
    out.append("> pi 臂 token 取自 pi JSON 事件流的 `usage.totalTokens`；两臂成本均取自 pi 的 "
               "`usage.cost.total`（Ralph 臂经其渲染日志解析，已剔除单轮 >$1 的 provider 误报）。")
    out.append("")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="?", default="runs/results.jsonl")
    ap.add_argument("--out", default=None)
    ap.add_argument("--transcripts", default=None,
                    help="transcript 目录；默认 <results>/../transcripts")
    args = ap.parse_args()

    rows = load(Path(args.results))
    if not rows:
        print("no rows")
        return 1

    tasks: list[str] = []
    arms: list[str] = []
    for r in rows:
        if r["task"] not in tasks:
            tasks.append(r["task"])
        if _arm(r) not in arms:
            arms.append(_arm(r))

    per: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for r in rows:
        per[(_arm(r), r["task"])].append(r)

    out: list[str] = []
    out.append("# OpenBench 对照：单 pi agent vs Ralph harness\n")
    out.append(f"- 结果文件：`{args.results}`")
    out.append(f"- 模型：{rows[0].get('model')}")
    out.append(f"- 任务数：{len(tasks)}，臂：{', '.join(arms)}\n")

    # 逐任务表
    out.append("## 逐任务\n")
    out.append("| 任务 | " +
               " | ".join(f"{a} 通过 | {a} score | {a} 均耗时(s)" for a in arms) + " |")
    out.append("|" + "---|" * (1 + 3 * len(arms)))
    for t in tasks:
        cells = []
        for a in arms:
            rs = per.get((a, t), [])
            n = len(rs)
            ok = sum(1 for r in rs if r.get("success"))
            sc = (sum(float(r.get("score") or 0) for r in rs) / n) if n else 0.0
            wt = (sum(float(r.get("wall_time_s") or 0) for r in rs) / n) if n else 0.0
            cells.append(f"{ok}/{n}" if n else "-")
            cells.append(f"{sc:.2f}" if n else "-")
            cells.append(f"{wt:.0f}" if n else "-")
        out.append(f"| {t} | " + " | ".join(cells) + " |")
    out.append("")

    # 总体表
    out.append("## 总体\n")
    out.append("| harness | cell 成功率 | 平均 score | 均耗时(s) | pass@k | pass^k |")
    out.append("|---|---|---|---|---|---|")
    for a in arms:
        rs = [r for r in rows if _arm(r) == a]
        n = len(rs)
        ok = sum(1 for r in rs if r.get("success"))
        sc = sum(float(r.get("score") or 0) for r in rs) / n if n else 0
        wt = sum(float(r.get("wall_time_s") or 0) for r in rs) / n if n else 0

        # pass@k / pass^k：按任务聚合，k = 该任务在此臂的 trial 数
        pass_at = pass_pow = tot = 0
        for t in tasks:
            trs = per.get((a, t), [])
            if not trs:
                continue
            tot += 1
            succ = [bool(r.get("success")) for r in trs]
            if any(succ):
                pass_at += 1
            if all(succ):
                pass_pow += 1
        pa = f"{pass_at}/{tot} ({pass_at / tot:.0%})" if tot else "-"
        pp = f"{pass_pow}/{tot} ({pass_pow / tot:.0%})" if tot else "-"
        out.append(f"| {a} | {ok}/{n} ({ok / n:.0%}) | {sc:.3f} | {wt:.0f} | {pa} | {pp} |")
    out.append("")
    out.append("> `pass@k`：每任务 k 次里至少成功 1 次；`pass^k`：k 次全成功。"
               "score 为 checker 的 `SCORE:` 部分分（无则为 0/1）。")

    tdir = Path(args.transcripts) if args.transcripts else (
        Path(args.results).resolve().parent.parent / "transcripts")
    if tdir.is_dir():
        out += cost_section(tdir, arms, tasks)

    text = "\n".join(out) + "\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
