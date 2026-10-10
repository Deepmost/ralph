#!/usr/bin/env python3
"""对抗性 A/B 实验的专用 runner。

解决的问题：对抗性套件的「期望值」绝不能被被测 Agent 读到。因此本 runner：

  1. 把主仓库复制到临时目录（--no-isolate 下 Developer 会 git commit，避免污染主干）；
  2. **在副本里删除泄露文件**（`benchmark/ADVERSARIAL.md`、`adversarial-tasks/expectations.json`）；
  3. 在副本里跑 `ab_experiment.py --run --no-isolate`；
  4. 把新增归档回收回主仓库；
  5. 在主仓库用 `adversarial_grader.py`（含真实 expectations.json）**补算对抗指标**写入 metrics.json；
  6. 打印报告。

用法:
    python3 adversarial_experiment.py --configs A,B --max-iterations 60
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/ralph/benchmark
RALPH_DIR = SCRIPT_DIR.parent                          # scripts/ralph
PROJECT_ROOT = RALPH_DIR.parent.parent                 # repo root
SUITE = SCRIPT_DIR / "adversarial-tasks"
RUNS_DIR = SCRIPT_DIR / "runs"

sys.path.insert(0, str(SCRIPT_DIR))
import adversarial_grader  # noqa: E402

LEAK_FILES = [
    "scripts/ralph/benchmark/ADVERSARIAL.md",       # 直接列了每题期望终态
    "scripts/ralph/benchmark/adversarial-tasks/expectations.json",
    "scripts/ralph/benchmark/adversarial_grader.py",  # 含正例清单与期望实现断言
    "scripts/ralph/benchmark/adversarial_experiment.py",
    "scripts/ralph/benchmark/README.md",            # 描述对抗套件设计
    "docs/agent-eval.md",                           # 提及对抗指标口径
]
# 运行副本里若残留这些词，说明还有泄露
LEAK_KEYWORDS = ["false_done", "overclaim", "expectations.json", "ADVERSARIAL.md"]
LEAK_KEYWORD_EXCLUDE = {"scripts/ralph/ab_experiment.py"}  # runner 自身可含（仅机制，无答案）


def _make_pi_iso() -> str:
    iso = tempfile.mkdtemp(prefix="ralph_pi_iso_")
    real = Path.home() / ".pi/agent"
    for name in ("auth.json", "models-store.json"):
        src = real / name
        if src.exists():
            shutil.copy2(src, Path(iso) / name)
    (Path(iso) / "settings.json").write_text(json.dumps({
        "defaultProvider": os.environ.get("RALPH_PI_PROVIDER", "opencode-go"),
        "defaultModel": os.environ.get("RALPH_PI_MODEL", "deepseek-v4.1-flash"),
    }), encoding="utf-8")
    return iso


def _scan_leaks(repo: Path) -> list[tuple[str, str]]:
    """扫描运行副本里是否残留泄露关键词（排除 runner 自身）。"""
    hits = []
    for f in repo.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(repo))
        if rel in LEAK_KEYWORD_EXCLUDE or rel.startswith(".git/"):
            continue
        if f.suffix not in (".py", ".md", ".json", ".txt", ".yaml", ".yml", ".toml"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for kw in LEAK_KEYWORDS:
            if kw in text:
                hits.append((rel, kw))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default="A,B")
    ap.add_argument("--max-iterations", type=int, default=60)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    if not (SUITE / "expectations.json").exists():
        print(f"缺少 {SUITE/'expectations.json'}", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="ralph_adv_run_"))
    repo = tmp / "repo"
    print(f"复制仓库到临时目录：{repo}")
    subprocess.run(["rsync", "-a", "--exclude", ".venv", "--exclude", "__pycache__",
                    f"{PROJECT_ROOT}/", f"{repo}/"], check=True)

    # 关键：删除泄露文件
    for rel in LEAK_FILES:
        p = repo / rel
        if p.exists():
            p.unlink()
            print(f"  de-leak: 删除副本内 {rel}")
    # 旧归档同样含答案（metrics.json/任务终态），整体移除（新归档会重新生成并回收）
    runs_dir = repo / "scripts/ralph/benchmark/runs"
    if runs_dir.exists():
        shutil.rmtree(runs_dir, ignore_errors=True)
        print("  de-leak: 清空副本内 scripts/ralph/benchmark/runs")

    leaks = _scan_leaks(repo)
    if leaks:
        print("⚠️  运行副本仍可能泄露（请人工确认）：")
        for f, kw in leaks[:20]:
            print(f"    {f}: 命中 {kw}")

    iso = _make_pi_iso()
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = iso
    env["RALPH_NO_LANGFUSE"] = "1"

    before = {p.name for p in RUNS_DIR.iterdir() if p.is_dir()} if RUNS_DIR.exists() else set()
    before_existing = {p.name for p in (repo / "scripts/ralph/benchmark/runs").iterdir()
                       if p.is_dir()} if (repo / "scripts/ralph/benchmark/runs").exists() else set()

    cmd = [sys.executable, str(repo / "scripts/ralph/ab_experiment.py"),
           "--run", "--no-isolate", "--configs", args.configs,
           "--benchmark-dir", str(repo / "scripts/ralph/benchmark/adversarial-tasks"),
           "--max-iterations", str(args.max_iterations), "--repeats", str(args.repeats)]
    print(f"运行：{' '.join(cmd)}\n")
    t0 = time.time()
    rc = subprocess.run(cmd, cwd=str(repo), env=env).returncode
    print(f"\n运行结束 rc={rc}，耗时 {time.time()-t0:.0f}s\n")

    # 回收新增归档
    src_runs = repo / "scripts/ralph/benchmark/runs"
    new_dirs = []
    if src_runs.exists():
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        for d in sorted(src_runs.iterdir()):
            if d.is_dir() and d.name not in before_existing:
                dst = RUNS_DIR / d.name
                shutil.copytree(d, dst, dirs_exist_ok=True)
                new_dirs.append(dst)

    # 主仓库补算对抗指标
    print("补算对抗指标：")
    rows = []
    for d in new_dirs:
        adv = adversarial_grader.grade(d, SUITE)
        mfile = d / "metrics.json"
        m = json.loads(mfile.read_text(encoding="utf-8")) if mfile.exists() else {}
        m["adversarial"] = adv
        mfile.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append((m.get("config", "?"), d.name, adv))
        print(f"  {d.name}: 伪完成率={adv['false_done_rate']}% "
              f"overclaim={adv['overclaim_rate']}% blocked={adv['blocked_accuracy']}%")

    if not args.keep_temp:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(iso, ignore_errors=True)

    # 打印主仓库报告
    subprocess.run([sys.executable, str(RALPH_DIR / "ab_experiment.py"), "--report"])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
