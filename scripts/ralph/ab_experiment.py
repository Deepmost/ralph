#!/usr/bin/env python3
"""
Ralph A/B 对照实验编排器。

用途：比较四组配置在 benchmark 任务集上的表现，量化「多 Agent 闭环 + 越权防腐」的增益。

  A 完整闭环（基线）
  B 仅 Developer              --no-validator --no-pm
  C Developer + Validator     --no-pm
  D 完整闭环（关闭越权回滚）  --no-guard

三种模式：
  --plan    只打印将要执行的命令（默认，不改动任何文件）
  --run     实跑：默认在临时 git worktree 中隔离运行（Developer 提交落到临时分支，跑完销毁；
            主干零污染）；用 --no-isolate 就地运行
  --report  读取 runs/ 下的归档，输出 Markdown 对照表

用法：
  python3 scripts/ralph/ab_experiment.py --plan
  python3 scripts/ralph/ab_experiment.py --run --configs A,B,C,D --max-iterations 30
  python3 scripts/ralph/ab_experiment.py --report
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent
RALPH_PY = SCRIPT_DIR / "ralph.py"

BENCH_DIR = SCRIPT_DIR / "benchmark"
DEFAULT_TASKS = BENCH_DIR / "example-tasks"
SANDBOX_DIR = BENCH_DIR / "sandbox"
RUNS_DIR = BENCH_DIR / "runs"

sys.path.insert(0, str(SCRIPT_DIR))
import metrics  # noqa: E402

# 运行时会话会改动的文件/目录（实跑前备份、结束后恢复）
RUNTIME_DIRS = ["tasks", "archive", "tasks.bak"]
RUNTIME_FILES = ["adjustments.json", "progress.txt", "state.json"]

CONFIGS: dict[str, dict] = {
    "A": {"desc": "完整闭环（基线）", "flags": []},
    "B": {"desc": "仅 Developer", "flags": ["--no-validator", "--no-pm"]},
    "C": {"desc": "Developer + Validator", "flags": ["--no-pm"]},
    "D": {"desc": "完整闭环（关闭越权回滚）", "flags": ["--no-guard"]},
}


# ══════════════════════════════════════════════════════════════
#  运行时文件的备份 / 恢复 / 重置
# ══════════════════════════════════════════════════════════════

def _runtime_paths() -> list[Path]:
    return [SCRIPT_DIR / n for n in RUNTIME_DIRS] + [SCRIPT_DIR / n for n in RUNTIME_FILES]


def backup_runtime(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for p in _runtime_paths():
        if not p.exists():
            continue
        target = dest / p.name
        if p.is_dir():
            shutil.copytree(p, target)
        else:
            shutil.copy2(p, target)


def restore_runtime(src: Path) -> None:
    for p in _runtime_paths():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    if not src.exists():
        return
    for item in src.iterdir():
        target = SCRIPT_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def reset_runtime(benchmark_tasks: Path) -> None:
    """清空运行时状态，并把 benchmark 任务拷入 tasks/。"""
    tasks_dir = SCRIPT_DIR / "tasks"
    done_dir = tasks_dir / "done"
    # 清空 tasks/ 与 done/
    if tasks_dir.exists():
        shutil.rmtree(tasks_dir)
    done_dir.mkdir(parents=True, exist_ok=True)
    # 清空 archive / tasks.bak
    for name in ("archive", "tasks.bak"):
        shutil.rmtree(SCRIPT_DIR / name, ignore_errors=True)
    # 清理 benchmark 沙盒，避免上一组产出的代码污染下一组（保留占位文件）
    if SANDBOX_DIR.exists():
        for item in SANDBOX_DIR.iterdir():
            if item.name == ".gitkeep":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
    # 拷入 benchmark 任务
    for f in sorted(benchmark_tasks.glob("*.md")):
        shutil.copy2(f, tasks_dir / f.name)
    # 重置审计日志 / 进度 / 状态
    (SCRIPT_DIR / "adjustments.json").write_text("[]\n", encoding="utf-8")
    (SCRIPT_DIR / "progress.txt").write_text(
        f"# Ralph Progress Log\nStarted: {time.strftime('%Y-%m-%d %H:%M:%S')}\n---\n",
        encoding="utf-8",
    )
    state = SCRIPT_DIR / "state.json"
    if state.exists():
        state.unlink()


# ══════════════════════════════════════════════════════════════
#  单次运行
# ══════════════════════════════════════════════════════════════

def build_command(cfg: str, run_id: str, max_iterations: int) -> list[str]:
    return [
        sys.executable, str(RALPH_PY), "pi",
        "--max-iterations", str(max_iterations),
        "--no-dashboard",
        "--run-id", run_id,
        *CONFIGS[cfg]["flags"],
    ]


def run_config(cfg: str, run_id: str, args: argparse.Namespace) -> Path:
    """实跑一组配置，归档结果，返回归档目录。"""
    reset_runtime(Path(args.benchmark_dir))
    cmd = build_command(cfg, run_id, args.max_iterations)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*64}\n  配置 {cfg}（{CONFIGS[cfg]['desc']}）run-id={run_id}\n  {' '.join(cmd)}\n{'='*64}")
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, env=None)
    (run_dir / "run.log").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    print(proc.stdout[-2000:])

    # 归档运行时产物
    if (SCRIPT_DIR / "tasks").exists():
        shutil.copytree(SCRIPT_DIR / "tasks", run_dir / "tasks")
    if SANDBOX_DIR.exists():
        shutil.copytree(SANDBOX_DIR, run_dir / "sandbox")
    for name in ("adjustments.json", "progress.txt", "state.json"):
        src = SCRIPT_DIR / name
        if src.exists():
            shutil.copy2(src, run_dir / name)

    metrics = compute_run_metrics(run_dir, cfg, run_id)
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  归档: {run_dir}")
    return run_dir


# ══════════════════════════════════════════════════════════════
#  指标计算（本地归档）
# ══════════════════════════════════════════════════════════════

def _import_ralph():
    sys.path.insert(0, str(SCRIPT_DIR))
    import ralph  # noqa
    return ralph


def _count_guard_hits(run_dir: Path) -> int:
    """统计引擎真实的越权回滚次数。

    只用引擎自身的固定前缀 `PM 改动越界（` 匹配；不能用 `已从备份回滚`，
    因为该字符串也会出现在（被 Agent 运行的）单元测试输出里，造成误计。
    """
    log = run_dir / "run.log"
    if not log.exists():
        return 0
    return sum(1 for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
               if "PM 改动越界（" in ln)


def compute_run_metrics(run_dir: Path, cfg: str = "", run_id: str = "") -> dict:
    ralph = _import_ralph()
    tasks_dir = run_dir / "tasks"
    done_dir = tasks_dir / "done"

    def scan(d: Path) -> list[dict]:
        rows = []
        if d.exists():
            for f in sorted(d.glob("*.md")):
                m = ralph.parse_frontmatter(f)
                m["_id"] = str(m.get("id", f.stem))
                rows.append(m)
        return rows

    pending = scan(tasks_dir)
    done = scan(done_dir) if done_dir.exists() else []
    blocked = [t for t in (pending + done) if t.get("blocked")]
    done_ok = [t for t in done if not t.get("blocked")]
    first_pass = [t for t in done_ok if int(t.get("retryCount", 0) or 0) == 0]
    total = len(pending) + len(done)

    def rate(num: int, den: int) -> float:
        return round(num / den * 100, 1) if den else 0.0

    # PM 调整动作
    adj_actions: dict[str, int] = {}
    adj_file = run_dir / "adjustments.json"
    if adj_file.exists():
        try:
            for rec in json.loads(adj_file.read_text(encoding="utf-8")):
                a = str(rec.get("action", "?"))
                adj_actions[a] = adj_actions.get(a, 0) + 1
        except Exception:
            pass

    return {
        "config": cfg,
        "run_id": run_id,
        "total": total,
        "done_ok": len(done_ok),
        "blocked": len(blocked),
        "completion_rate": rate(len(done_ok), total),
        "first_pass_rate": rate(len(first_pass), len(done_ok)),
        "blocked_rate": rate(len(blocked), total),
        "pm_adjustments": adj_actions,
        "guard_hits": _count_guard_hits(run_dir),
        "evidence": metrics.evidence_metrics(run_dir),
        "tools": metrics.tool_metrics(run_dir),
        "cost": metrics.cost_metrics(run_dir),
    }


# ══════════════════════════════════════════════════════════════
#  报告
# ══════════════════════════════════════════════════════════════

def _infer_config(run_id: str) -> str:
    parts = run_id.split("-")
    if len(parts) >= 2 and parts[0] == "ralph" and parts[1] in CONFIGS:
        return parts[1]
    return ""


def load_runs() -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    runs = []
    for d in sorted(RUNS_DIR.iterdir()):
        if not d.is_dir():
            continue
        mfile = d / "metrics.json"
        if mfile.exists():
            runs.append(json.loads(mfile.read_text(encoding="utf-8")))
        else:
            runs.append(compute_run_metrics(d, cfg=_infer_config(d.name), run_id=d.name))
    return runs


def runs_by_config() -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    if RUNS_DIR.exists():
        for d in sorted(RUNS_DIR.iterdir()):
            if d.is_dir():
                cfg = _infer_config(d.name)
                if cfg:
                    groups.setdefault(cfg, []).append(d)
    return groups


def build_report(runs: list[dict]) -> str:
    L: list[str] = ["# Ralph A/B 对照实验结果", ""]
    if not runs:
        L.append("_暂无运行归档。先执行 `--run`。_")
        return "\n".join(L)

    L.append("| 配置 | run-id | 任务总数 | 完成 | 完成率 | 首次通过率 | 阻塞率 | 越权拦截 | 验证命令 | 工具错误率 | 成本($) | PM 调整 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        ev = r.get("evidence", {}) or {}
        tl = r.get("tools", {}) or {}
        cs = r.get("cost", {}) or {}
        L.append(
            f"| {r.get('config','?')} | {r.get('run_id','?')} | {r.get('total',0)} | "
            f"{r.get('done_ok',0)} | {r.get('completion_rate',0)}% | "
            f"{r.get('first_pass_rate',0)}% | {r.get('blocked_rate',0)}% | "
            f"{r.get('guard_hits',0)} | {ev.get('verification_commands',0)} | "
            f"{tl.get('tool_error_rate',0)}% | {cs.get('cost_total',0)} | {r.get('pm_adjustments',{})} |"
        )
    L.append("")

    # 与基线 A 的差值
    base = next((r for r in runs if r.get("config") == "A"), None)
    if base:
        L.append("## 相对基线 A 的差值")
        L.append("")
        L.append("| 配置 | Δ完成率 | Δ首次通过率 | Δ阻塞率 |")
        L.append("|---|---|---|---|")
        for r in runs:
            if r.get("config") == "A":
                continue
            L.append(
                f"| {r.get('config','?')} | "
                f"{round(r.get('completion_rate',0)-base.get('completion_rate',0),1)} | "
                f"{round(r.get('first_pass_rate',0)-base.get('first_pass_rate',0),1)} | "
                f"{round(r.get('blocked_rate',0)-base.get('blocked_rate',0),1)} |"
            )
        L.append("")

    # 一致性与可靠性：pass@k / pass^k（仅当某配置有多次运行）
    groups = runs_by_config()
    multi = {c: ds for c, ds in groups.items() if len(ds) >= 2}
    if multi:
        L.append("## 一致性与可靠性（多次运行）")
        L.append("")
        L.append("| 配置 | k | 任务数 | pass@k（≥1 次成功） | pass^k（k 次全成功） |")
        L.append("|---|---|---|---|---|")
        for c in sorted(multi):
            pm = metrics.compute_pass_metrics(multi[c])
            L.append(f"| {c} | {pm['k']} | {pm['tasks']} | {pm['pass_at_k']}% | {pm['pass_power_k']}% |")
        L.append("")

    L.append("> 口径见 `docs/agent-eval.md`；成本/延迟可结合 `eval_langfuse.py --release <run-id>` 获取。")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════
#  worktree 隔离
# ══════════════════════════════════════════════════════════════

def _in_git_repo() -> bool:
    try:
        r = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def run_isolated(args: argparse.Namespace) -> int:
    """
    在临时 git worktree 中执行实验，隔离 Developer 的提交。

    - 从当前 HEAD 新建分支 + worktree
    - 在 worktree 里以 --in-worktree 重新执行本脚本
    - 回收实验归档（runs/）到主仓库
    - 无论成败都移除 worktree 并删除临时分支（Developer 的提交随之丢弃）
    """
    repo = PROJECT_ROOT
    ts = time.strftime("%Y%m%d-%H%M%S")
    branch = f"ralph-ab-{ts}"
    wt_parent = Path(tempfile.mkdtemp(prefix="ralph-ab-wt-"))
    wt = wt_parent / "repo"

    # 工作区不干净时警告：worktree 取自 HEAD，未提交改动不会带过去
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("⚠️  工作区有未提交改动；worktree 取自 HEAD，这些改动不会进入隔离环境。", file=sys.stderr)

    print(f"创建隔离 worktree：branch={branch}  path={wt}")
    try:
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", branch, str(wt), "HEAD"],
                       check=True)
    except subprocess.CalledProcessError as e:
        print(f"创建 worktree 失败：{e}", file=sys.stderr)
        shutil.rmtree(wt_parent, ignore_errors=True)
        return 2

    try:
        wt_script = wt / "scripts" / "ralph" / "ab_experiment.py"
        # 默认任务集映射到 worktree 内路径；显式指定的目录原样透传
        if str(Path(args.benchmark_dir).resolve()) == str(Path(DEFAULT_TASKS).resolve()):
            bm = str(wt / "scripts" / "ralph" / "benchmark" / "example-tasks")
        else:
            bm = args.benchmark_dir
        cmd = [
            sys.executable, str(wt_script), "--run", "--in-worktree",
            "--configs", args.configs,
            "--repeats", str(args.repeats),
            "--max-iterations", str(args.max_iterations),
            "--benchmark-dir", bm,
        ]
        print(f"在 worktree 内执行：{' '.join(cmd)}\n")
        rc = subprocess.run(cmd, cwd=str(wt)).returncode

        # 回收 runs 归档到主仓库
        src_runs = wt / "scripts" / "ralph" / "benchmark" / "runs"
        if src_runs.exists():
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            for d in sorted(src_runs.iterdir()):
                if d.is_dir():
                    shutil.copytree(d, RUNS_DIR / d.name, dirs_exist_ok=True)
        return rc
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                       check=False)
        subprocess.run(["git", "-C", str(repo), "branch", "-D", branch], check=False)
        shutil.rmtree(wt_parent, ignore_errors=True)
        print(f"\n隔离 worktree 已移除，分支 {branch} 已删除（主干未被污染）。")


# ══════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ralph A/B 对照实验编排器")
    p.add_argument("--plan", action="store_true", help="只打印将要执行的命令（默认）")
    p.add_argument("--run", action="store_true", help="实跑对照实验")
    p.add_argument("--report", action="store_true", help="读取归档输出报告")
    p.add_argument("--configs", default="A,B,C,D", help="参与实验的配置，逗号分隔")
    p.add_argument("--max-iterations", type=int, default=30, help="每组最大迭代次数")
    p.add_argument("--benchmark-dir", default=str(DEFAULT_TASKS), help="benchmark 任务目录")
    p.add_argument("--repeats", type=int, default=1, help="每个配置重复次数（用于 pass@k/pass^k）")
    p.add_argument("--no-isolate", action="store_true",
                   help="不创建 worktree，就地运行（默认会在临时 worktree 中隔离运行）")
    p.add_argument("--in-worktree", action="store_true",
                   help=argparse.SUPPRESS)  # 内部使用：已在 worktree 内
    return p.parse_args()


def main() -> int:
    args = parse_args()
    configs = [c.strip().upper() for c in args.configs.split(",") if c.strip()]
    for c in configs:
        if c not in CONFIGS:
            print(f"未知配置: {c}（可选 {list(CONFIGS)}）", file=sys.stderr)
            return 2

    if args.report:
        print(build_report(load_runs()))
        return 0

    if not args.run:
        # plan 模式
        print("# A/B 对照实验计划\n")
        for c in configs:
            rid = f"ralph-{c}-<timestamp>"
            print(f"## 配置 {c} — {CONFIGS[c]['desc']}")
            print(f"    {' '.join(build_command(c, rid, args.max_iterations))}\n")
        print("实跑请加 `--run`：默认会在临时 git worktree 中隔离运行，"
              "Developer 的提交落在临时分支、跑完即销毁，主干零污染。"
              "如需就地运行用 `--no-isolate`。")
        return 0

    # 实跑：默认在临时 worktree 中隔离运行，避免 Developer 提交污染主干
    if not args.in_worktree and not args.no_isolate and _in_git_repo():
        rc = run_isolated(args)
        print(build_report(load_runs()))
        return rc

    # run 模式（就地或已处于 worktree 内）
    benchmark = Path(args.benchmark_dir)
    if not benchmark.exists():
        print(f"benchmark 目录不存在: {benchmark}", file=sys.stderr)
        return 2

    def do_runs() -> None:
        for c in configs:
            for rep in range(1, max(args.repeats, 1) + 1):
                run_id = f"ralph-{c}-{time.strftime('%Y%m%d-%H%M%S')}-r{rep}"
                run_config(c, run_id, args)

    if args.in_worktree:
        # worktree 本身已隔离，无需备份/恢复
        do_runs()
    else:
        backup = Path(tempfile.mkdtemp(prefix="ralph-ab-backup-"))
        print(f"备份运行时文件到: {backup}")
        backup_runtime(backup)
        try:
            do_runs()
        finally:
            restore_runtime(backup)
            print(f"\n运行时文件已恢复（备份保留在 {backup}）")

    print(build_report(load_runs()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
