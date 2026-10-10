#!/usr/bin/env python3
"""用 MARBLE 的协调评估 prompt，给 Ralph 的「共享产物」打分。

Ralph 没有 Agent 间自由对话，它通过共享文件协作（progress.txt / 任务文件 /
adjustments.json / AGENTS.md）。本脚本把这些产物整理成 MARBLE 评估器能吃的
「通信日志」和「规划数据」，再调用 MARBLE 原始 prompt（Communication /
Planning / KPI）打分，产出与 MultiAgentBench 同口径的：

    communication_score, planning_score, milestones(agent_kpis)

用法:
    python3 score_ralph_coordination.py RUN_DIR [RUN_DIR ...] \
        --label A=path --model openai/deepseek-v4.1-flash --out report.md

RUN_DIR 需含 progress.txt / tasks/ / adjustments.json（Ralph 运行归档即符合）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MARBLE = Path("/Users/deepmost/Documents/whaty-project/MARBLE")
RALPH_DIR = HERE.parent.parent  # scripts/ralph

MAX_COMM_CHARS = 12000


# ── MARBLE 打分逻辑（复制以保证同口径）────────────────────────────────
def parse_score(text: str) -> int:
    content = (text or "").strip()
    if content.startswith("```json"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    js, je = content.find("{"), content.rfind("}") + 1
    if js >= 0 and je > js:
        try:
            data = json.loads(content[js:je])
            if isinstance(data, dict) and "rating" in data:
                score = int(data["rating"])
                if 1 <= score <= 5:
                    return score
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            pass
    nums = re.findall(r"\b[1-5]\b", content)
    return int(nums[0]) if nums else 3


def fill(template: str, **kw) -> str:
    """只替换已知占位符，避开 prompt 里 JSON 示例的单花括号（MARBLE 用 .format 会炸）。"""
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out.replace("{{", "{").replace("}}", "}")


def load_prompts(marble_dir: Path) -> dict:
    path = marble_dir / "marble/evaluator/evaluator_prompts.json"
    return json.loads(path.read_text(encoding="utf-8"))["Graph"]


# ── 读 Ralph 产物 ────────────────────────────────────────────────────
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---", re.S)


def _frontmatter(text: str) -> dict:
    m = _FM_RE.search(text)
    out: dict = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def read_run(run_dir: Path) -> dict:
    progress = (run_dir / "progress.txt").read_text(encoding="utf-8", errors="replace") \
        if (run_dir / "progress.txt").exists() else ""
    adjustments = (run_dir / "adjustments.json").read_text(encoding="utf-8", errors="replace") \
        if (run_dir / "adjustments.json").exists() else ""

    tasks = []
    for sub in ("tasks", "tasks/done"):
        d = run_dir / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            fm = _frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            body = f.read_text(encoding="utf-8", errors="replace")
            tasks.append({
                "file": f.name,
                "state": "done" if "done" in sub else "pending",
                "id": fm.get("id", f.stem),
                "title": fm.get("title", ""),
                "retryCount": fm.get("retryCount", "0"),
                "blocked": fm.get("blocked", "false"),
                "has_validation_failure": "验证失败记录" in body,
            })

    patterns = ""
    pd = RALPH_DIR.parent / "docs"
    # 运行归档里通常没有 docs/patterns，用 progress 里的 Codebase Patterns 段代替
    m = re.search(r"## Codebase Patterns\n(.*?)(?=\n## |\Z)", progress, re.S)
    if m:
        patterns = m.group(1).strip()
    return {"dir": str(run_dir), "progress": progress, "adjustments": adjustments,
            "tasks": tasks, "patterns": patterns}


def _role_profiles() -> str:
    out = []
    for role, fn in (("Developer", "DEVELOPER.md"), ("Validator", "VALIDATOR.md"), ("PM", "PM.md")):
        p = RALPH_DIR / fn
        if p.exists():
            first = [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
            out.append(f"- {role}: {first[0] if first else fn}")
    return "\n".join(out)


def build_communications(run: dict) -> str:
    """把共享产物整理成 role-tagged 的通信日志。"""
    prog = run["progress"]
    # 拆出 Developer 进度段（## 开头）与 Validator 结构反馈（### 开头）
    dev_blocks, validator_blocks = [], []
    for part in re.split(r"\n(?=## )", prog):
        if part.startswith("## Codebase Patterns"):
            continue
        if part.startswith("## ") and not part.startswith("### "):
            dev_blocks.append(part.strip())
    for m in re.finditer(r"### 结构反馈[^\n]*\n(.*?)(?=\n### |\n## |\Z)", prog, re.S):
        validator_blocks.append(m.group(0).strip())

    adj = run["adjustments"].strip()
    if adj in ("", "{}", "[]"):
        adj_txt = "（无 PM 队列调整）"
    else:
        adj_txt = adj

    sections = [
        "Ralph 的 Agent 之间没有自由对话，而是通过共享文件协作。以下按作者角色整理共享产物。",
        "\n### [Developer → 共享] 进度记录（节选）\n" +
        ("\n\n".join(dev_blocks)[: MAX_COMM_CHARS // 2] if dev_blocks else "（无）"),
        "\n### [Validator → 共享] 结构反馈 / 验证失败记录\n" +
        ("\n\n".join(validator_blocks) if validator_blocks else "（无）"),
        "\n### [PM → 任务队列] adjustments.json\n" + adj_txt,
        "\n### [所有角色 → 共享] Codebase Patterns（沉淀）\n" + (run["patterns"] or "（无）"),
    ]
    text = "\n".join(sections)
    return text[:MAX_COMM_CHARS]


def build_planning(run: dict) -> dict:
    tasks = run["tasks"]
    done = [t for t in tasks if t["state"] == "done"]
    pending = [t for t in tasks if t["state"] == "pending"]
    retried = [t for t in tasks if str(t["retryCount"]) not in ("", "0")]
    blocked = [t for t in tasks if str(t["blocked"]).lower() in ("true", "1")]

    agent_tasks = "任务队列（执行顺序 = 文件名序号）：\n" + "\n".join(
        f"- [{t['state']}] id={t['id']} 《{t['title']}》 retry={t['retryCount']}"
        + (" [BLOCKED]" if str(t['blocked']).lower() in ('true','1') else "")
        for t in tasks) or "（无）"

    summary = (f"本轮共 {len(tasks)} 个任务：完成 {len(done)}、待办 {len(pending)}、"
               f"重试过 {len(retried)}、阻塞 {len(blocked)}。")
    if run["adjustments"].strip() not in ("", "{}", "[]"):
        summary += " PM 对任务队列做过调整（见 adjustments.json）。"

    results = "各任务终态：\n" + "\n".join(
        f"- id={t['id']} 《{t['title']}》→ {t['state']}"
        + (f"(retry={t['retryCount']})" if str(t['retryCount']) not in ('','0') else "")
        + (" [BLOCKED]" if str(t['blocked']).lower() in ('true','1') else "")
        + (" [曾验证失败]" if t['has_validation_failure'] else "")
        for t in tasks) or "（无）"
    return {"summary": summary, "agent_profiles": _role_profiles(),
            "agent_tasks": agent_tasks, "results": results}


def build_kpi_task(run: dict) -> tuple[str, str]:
    task = "Ralph 自治编码闭环需完成的任务：\n" + "\n".join(
        f"- {t['id']} 《{t['title']}》" for t in run["tasks"]) or "（无）"
    agent_results = "各角色的产物与结果：\n" + "\n".join(
        f"- [{t['state']}] id={t['id']} 《{t['title']}》"
        + (" [BLOCKED]" if str(t['blocked']).lower() in ('true','1') else "")
        for t in run["tasks"])
    if run["patterns"]:
        agent_results += "\n- PM/共享沉淀 Codebase Patterns: " + run["patterns"][:800]
    if run["adjustments"].strip() not in ("", "{}", "[]"):
        agent_results += "\n- PM 队列调整: " + run["adjustments"][:800]
    return task, agent_results


# ── LLM 调用 ─────────────────────────────────────────────────────────
def make_caller(model: str):
    try:
        import marble_opencode_shim  # noqa: F401  (opencode-go 路由)
    except Exception:
        pass
    import litellm

    def call(prompt: str) -> str:
        # MARBLE 原用 max_tokens=512，但对 reasoning 模型会被思考占满导致正文为空，
        # 故提到 2048（口径差异已在报告注明）。
        r = litellm.completion(model=model, messages=[{"role": "user", "content": prompt}],
                               max_tokens=2048, temperature=0.0)
        return r.choices[0].message.content or ""
    return call


def score_run(run: dict, prompts: dict, call, repeats: int = 1) -> dict:
    comm_prompt = fill(prompts["Communication"]["prompt"],
                       task="Ralph 多 Agent 自治编码闭环：Developer 实现、Validator 验证、PM 治理任务队列。",
                       communications=build_communications(run))
    planning = build_planning(run)
    plan_prompt = fill(prompts["Planning"]["prompt"], **planning)
    kpi_task, kpi_results = build_kpi_task(run)
    kpi_prompt = fill(prompts["KPI"]["prompt"], task=kpi_task, agent_results=kpi_results)

    comm_scores, plan_scores = [], []
    milestones = []
    for _ in range(max(1, repeats)):
        comm_scores.append(parse_score(call(comm_prompt)))
        plan_scores.append(parse_score(call(plan_prompt)))
        k_raw = call(kpi_prompt)
        js, je = k_raw.find("["), k_raw.rfind("]") + 1
        if js >= 0 and je > js and not milestones:
            try:
                parsed = json.loads(k_raw[js:je])
                if isinstance(parsed, list):
                    milestones = parsed
            except json.JSONDecodeError:
                pass

    return {"communication_score": round(sum(comm_scores) / len(comm_scores), 2),
            "planning_score": round(sum(plan_scores) / len(plan_scores), 2),
            "communication_range": comm_scores,
            "planning_range": plan_scores,
            "milestones": milestones if isinstance(milestones, list) else [],
            "total_milestones": len(milestones) if isinstance(milestones, list) else 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--marble-dir", default=str(DEFAULT_MARBLE))
    ap.add_argument("--model", default="openai/deepseek-v4.1-flash")
    ap.add_argument("--labels", nargs="*", default=[], help="label=dir 覆盖显示名")
    ap.add_argument("--out", default=None)
    ap.add_argument("--repeats", type=int, default=3, help="每个 run 重复打分次数（取均值，降 judge 噪声）")
    args = ap.parse_args()

    label_map = {}
    for item in args.labels:
        if "=" in item:
            lab, d = item.split("=", 1)
            label_map[str(Path(d).resolve())] = lab

    prompts = load_prompts(Path(args.marble_dir))
    call = make_caller(args.model)

    rows = []
    for d in args.run_dirs:
        rd = Path(d)
        run = read_run(rd)
        res = score_run(run, prompts, call, repeats=args.repeats)
        label = label_map.get(str(rd.resolve()), rd.name)
        rows.append((label, res))
        print(f"[{label}] communication={res['communication_score']} "
              f"{res['communication_range']} planning={res['planning_score']} "
              f"{res['planning_range']} milestones={res['total_milestones']}")

    lines = ["# Ralph 协调产物质性（MARBLE 口径）\n",
             f"- 模型（judge）：{args.model}", f"- 运行数：{len(rows)}",
             f"- 每运行重复：{args.repeats}（取均值）\n",
             "| 运行 | communication(均值) | 范围 | planning(均值) | 范围 | milestones |",
             "|---|---|---|---|---|---|"]
    for label, res in rows:
        lines.append(f"| {label} | {res['communication_score']} | {res['communication_range']} | "
                     f"{res['planning_score']} | {res['planning_range']} | {res['total_milestones']} |")
    lines.append("\n> communication/planning 为 MARBLE 的 1–5 分；milestones 为 KPI prompt 识别出的里程碑数。"
                 "\n> 注意：MARBLE 原 prompt 面向 Agent 对话，这里把 Ralph 的**共享文件产物**映射为通信日志，"
                 "属适配用法；分值为 LLM-judge，重复取均值以降低噪声。")
    text = "\n".join(lines) + "\n"
    print("\n" + text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
