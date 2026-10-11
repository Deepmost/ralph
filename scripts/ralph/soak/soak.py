#!/usr/bin/env python3
"""
Ralph 泡测守护器（soak supervisor）。

用途：让 Ralph 引擎在无人干预下连续运行数小时。引擎本身有几个会终止整轮的硬行为
（连续双失败 sys.exit(2)、达最大迭代 sys.exit(1)、队列耗尽 sys.exit(0)），本守护器
在其外层做自动重启、心跳、预算/时长熔断、资源采样与任务队列补给。

设计要点（纯标准库，macOS / Linux）：
  - 子进程以独立进程组启动，退出/熔断时可整组优雅终止（先 SIGINT 再 SIGKILL）；
  - 实时解析子进程输出：迭代进度、费用、越权回滚、双失败、任务耗尽；
  - 每 N 秒写 heartbeat.json（供外部探活）与 resource.csv（RSS/CPU/进程数）；
  - 时长与费用达上限即优雅停止；
  - 可选 task-pool：队列可运行任务低于阈值时自动补给，避免 archive 清空进度；
  - 结束时写 summary.json 汇总。

用法：
  python3 scripts/ralph/soak/soak.py --hours 10 --budget-usd 20 --max-iterations 400
  python3 scripts/ralph/soak/soak.py --print-plan          # 只打印将要执行的命令
  python3 scripts/ralph/soak/soak.py --once                # 只跑一轮（调试）
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ── 路径配置 ──

SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/ralph/soak
RALPH_SCRIPT_DIR = SCRIPT_DIR.parent                   # scripts/ralph
PROJECT_ROOT = RALPH_SCRIPT_DIR.parent.parent          # 仓库根
TASKS_DIR = RALPH_SCRIPT_DIR / "tasks"
DONE_DIR = TASKS_DIR / "done"
STATE_FILE = RALPH_SCRIPT_DIR / "state.json"
DEFAULT_RALPH_PY = RALPH_SCRIPT_DIR / "ralph.py"

# ── 解析正则 ──

_ITER_RE = re.compile(r"迭代\s+(\d+)\s*/\s*(\d+)")
_COST_RE = re.compile(r"费用:\s*\$([0-9eE.+-]+)")
_BLOCKED_RE = re.compile(r"(?m)^blocked\s*:\s*true\b")


# ══════════════════════════════════════════════════════════════
#  纯函数（可单测）
# ══════════════════════════════════════════════════════════════

def parse_iteration(line: str) -> int | None:
    """从引擎日志行解析当前迭代号，如 `  迭代 12/60 - 开发 Agent` → 12。"""
    m = _ITER_RE.search(line)
    return int(m.group(1)) if m else None


def parse_cost(line: str) -> float | None:
    """从引擎日志行解析单轮费用，如 `[Turn] 费用: $0.0007` 或科学计数法。"""
    m = _COST_RE.search(line)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def backoff_delay(attempt: int, base: float = 5.0, cap: float = 60.0) -> float:
    """指数退避：第 attempt 次重启等待 min(cap, base * 2^(attempt-1)) 秒。"""
    if attempt < 1:
        attempt = 1
    return min(cap, base * (2 ** (attempt - 1)))


def is_exhausted(line: str) -> bool:
    return "所有任务已完成" in line


def is_max_iterations(line: str) -> bool:
    return "已达最大迭代次数" in line


def is_double_failure(line: str) -> bool:
    return "连续两次失败" in line


def is_guard_rollback(line: str) -> bool:
    return "PM 改动越界（" in line


def has_runnable_task(text: str) -> bool:
    """判断任务文件内容是否为可运行任务（含 id 且未被 blocked）。"""
    head = text[:2000]
    if "\nid:" not in head and not head.startswith("id:"):
        return False
    return _BLOCKED_RE.search(head) is None


def count_runnable(tasks_dir: Path) -> int:
    """统计 tasks/ 下可运行（非 blocked）任务数。"""
    if not tasks_dir.exists():
        return 0
    n = 0
    for f in tasks_dir.glob("*.md"):
        try:
            if has_runnable_task(f.read_text(encoding="utf-8", errors="replace")):
                n += 1
        except Exception:
            continue
    return n


# ══════════════════════════════════════════════════════════════
#  进程树资源采样（macOS / Linux 通用）
# ══════════════════════════════════════════════════════════════

def _proc_table() -> dict[int, tuple[int, int, float]]:
    """返回 {pid: (ppid, rss_kb, cpu_pct)}。"""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss=,pcpu="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return {}
    table: dict[int, tuple[int, int, float]] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss = int(parts[2])
            cpu = float(parts[3]) if len(parts) > 3 else 0.0
        except ValueError:
            continue
        table[pid] = (ppid, rss, cpu)
    return table


def tree_stats(root_pid: int) -> tuple[int, int, float]:
    """返回以 root_pid 为根的进程树 (进程数, 总 RSS_KB, 总 CPU%)。"""
    table = _proc_table()
    if root_pid not in table:
        return 0, 0, 0.0
    children: dict[int, list[int]] = {}
    for pid, (ppid, _rss, _cpu) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = [root_pid]
    count = 0
    total_rss = 0
    total_cpu = 0.0
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        if p in table:
            _ppid, rss, cpu = table[p]
            count += 1
            total_rss += rss
            total_cpu += cpu
        stack.extend(children.get(p, []))
    return count, total_rss, total_cpu


# ══════════════════════════════════════════════════════════════
#  运行时状态
# ══════════════════════════════════════════════════════════════

class Stats:
    def __init__(self) -> None:
        self.iterations = 0
        self.cost = 0.0
        self.guard_rollbacks = 0
        self.double_failures = 0
        self.exhausted_events = 0
        self.max_iter_events = 0
        self.restarts = 0
        self.exit_codes: list[int] = []
        self.peak_rss_kb = 0
        self.child_line_count = 0

    def to_dict(self) -> dict:
        return {
            "iterations": self.iterations,
            "cost_total": round(self.cost, 6),
            "guard_rollbacks": self.guard_rollbacks,
            "double_failures": self.double_failures,
            "exhausted_events": self.exhausted_events,
            "max_iter_events": self.max_iter_events,
            "restarts": self.restarts,
            "exit_codes": self.exit_codes,
            "peak_rss_mb": round(self.peak_rss_kb / 1024, 1),
            "child_lines": self.child_line_count,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════
#  守护器主体
# ══════════════════════════════════════════════════════════════

class Supervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.run_id = args.run_id or f"soak-{time.strftime('%Y%m%d-%H%M%S')}"
        self.stats = Stats()
        self.start_time = time.time()
        self.stop_reason: str | None = None
        self._stop_event = threading.Event()

        # 复用同一 --out 目录重跑时，先把上一次运行的产物整轮归档，
        # 否则 master.log / metrics.jsonl / resource.csv 的追加会跨运行污染统计。
        self._rotate_foreign_run_artifacts()
        self.deployed = self._load_deployed()

        self.master = (self.out / "master.log").open("a", buffering=1, encoding="utf-8")
        res_path = self.out / "resource.csv"
        new_res = not res_path.exists()
        self.resource = res_path.open("a", buffering=1, encoding="utf-8")
        if new_res:
            self.resource.write("ts,elapsed_s,child_pid,procs,rss_mb,cpu_pct\n")

        # Langfuse 开关校验（凭据缺失时仅告警，不阻断长跑）
        if args.langfuse and self._langfuse_config() is None:
            self.log("Langfuse 已启用但未找到凭据（~/.pi/agent/langfuse.json 或 LANGFUSE_* 环境变量）",
                     "WARN")

    # ── 日志 ──

    def log(self, msg: str, level: str = "INFO") -> None:
        line = f"{now_iso()} [{level}] {msg}"
        print(line, flush=True)
        self.master.write(line + "\n")

    # ── 整轮产物隔离 ──

    def _read_prior_run_id(self) -> str | None:
        """从上次产物中推断上一次运行的 run-id。"""
        for name, key in (("metrics.jsonl", None), ("heartbeat.json", "run_id"),
                          ("summary.json", "run_id")):
            p = self.out / name
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                if name == "metrics.jsonl":
                    for ln in text.splitlines():
                        if ln.strip():
                            return json.loads(ln).get("run_id")
                else:
                    return json.loads(text).get(key)
            except Exception:
                continue
        return None

    def _rotate_foreign_run_artifacts(self) -> None:
        """若 out 目录存在属于别的 run-id 的产物，整轮移动到 previous-<old>/。"""
        prior = self._read_prior_run_id()
        if not prior or prior == self.run_id:
            return
        dest = self.out / f"previous-{prior}"
        dest.mkdir(parents=True, exist_ok=True)
        names = ["master.log", "metrics.jsonl", "resource.csv", "heartbeat.json",
                 "summary.json", "deployed.txt"]
        for f in list(self.out.glob("restart-*.log")):
            names.append(f.name)
        for name in names:
            src = self.out / name
            if src.exists():
                try:
                    src.rename(dest / name)
                except Exception:
                    pass

    # ── 部署记录（task-pool 补给用） ──

    def _load_deployed(self) -> set[str]:
        p = self.out / "deployed.txt"
        if not p.exists():
            return set()
        return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}

    def _record_deployed(self, name: str) -> None:
        self.deployed.add(name)
        with (self.out / "deployed.txt").open("a", encoding="utf-8") as fh:
            fh.write(name + "\n")

    # ── Langfuse / 子进程环境 ──

    def _langfuse_config(self):
        """读取 Langfuse 凭据（复用 eval_langfuse）。不可用时返回 None。"""
        try:
            sys.path.insert(0, str(RALPH_SCRIPT_DIR))
            import eval_langfuse  # noqa: E402
            return eval_langfuse.load_config()
        except Exception:
            return None

    def _child_env(self) -> dict:
        """构造子进程环境：按开关打开/关闭 Langfuse 上报并注入环境标签。"""
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        if self.args.langfuse:
            env.pop("RALPH_NO_LANGFUSE", None)   # 确保上报开启
            env["RALPH_LANGFUSE_ENV"] = self.args.langfuse_env
        else:
            env["RALPH_NO_LANGFUSE"] = "1"
        return env

    # ── 存储体积（长跑膨胀观测） ──

    def _sizes(self) -> dict:
        def size(p: Path) -> int:
            try:
                return p.stat().st_size
            except Exception:
                return 0
        pat_dir = PROJECT_ROOT / "docs"
        pat_lines = 0
        pat_files = 0
        if pat_dir.exists():
            for f in sorted(pat_dir.glob("patterns-*.md")):
                pat_files += 1
                try:
                    pat_lines += len(f.read_text(encoding="utf-8", errors="replace").splitlines())
                except Exception:
                    pass
        return {
            "state_json_bytes": size(STATE_FILE),
            "progress_bytes": size(RALPH_SCRIPT_DIR / "progress.txt"),
            "agents_md_bytes": size(PROJECT_ROOT / "AGENTS.md"),
            "patterns_files": pat_files,
            "patterns_lines": pat_lines,
        }

    # ── 命令构建 ──

    def build_cmd(self) -> list[str]:
        a = self.args
        cmd = [a.python, str(Path(a.ralph_py).resolve()), "pi",
               "--max-iterations", str(a.max_iterations),
               "--run-id", self.run_id]
        if not a.with_dashboard:
            cmd.append("--no-dashboard")
        else:
            cmd += ["--port", str(a.port)]
        if a.no_validator:
            cmd.append("--no-validator")
        if a.no_pm:
            cmd.append("--no-pm")
        if a.no_guard:
            cmd.append("--no-guard")
        cmd += list(a.extra)
        return cmd

    # ── 补给 ──

    def maybe_refill(self) -> None:
        a = self.args
        runnable = count_runnable(TASKS_DIR)
        if runnable >= a.min_queue:
            return
        if a.no_refill or not a.task_pool:
            if runnable == 0:
                self.log(f"队列仅剩 {runnable} 个可运行任务，且未配置 task-pool", "WARN")
            return
        pool = Path(a.task_pool)
        if not pool.is_dir():
            self.log(f"task-pool 不存在: {pool}", "WARN")
            return
        copied = 0
        for f in sorted(pool.glob("*.md")):
            if copied >= a.refill_batch:
                break
            if f.name in self.deployed:
                continue
            if (TASKS_DIR / f.name).exists() or (DONE_DIR / f.name).exists():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if not has_runnable_task(text):
                continue
            shutil.copy2(f, TASKS_DIR / f.name)
            self._record_deployed(f.name)
            copied += 1
        if copied:
            self.log(f"补给 {copied} 个任务（当前可运行 {runnable}），队列现为 "
                     f"{count_runnable(TASKS_DIR)}")

    # ── 心跳与采样 ──

    def write_heartbeat(self, child: subprocess.Popen | None) -> None:
        elapsed = time.time() - self.start_time
        state = read_state()
        rt = state.get("runtime", {}) if isinstance(state, dict) else {}
        tasks = state.get("tasks", []) if isinstance(state, dict) else []
        pending = sum(1 for t in tasks if t.get("status") == "pending")
        done = sum(1 for t in tasks if t.get("status") == "done")
        blocked = sum(1 for t in tasks if t.get("status") == "blocked")
        hb = {
            "ts": now_iso(),
            "run_id": self.run_id,
            "environment": self.args.langfuse_env,
            "started_at": datetime.fromtimestamp(
                self.start_time, timezone.utc).astimezone().isoformat(timespec="seconds"),
            "elapsed_s": int(elapsed),
            "hours_limit": self.args.hours,
            "budget_usd": self.args.budget_usd,
            "child_pid": child.pid if child and child.poll() is None else None,
            "restarts": self.stats.restarts,
            "iteration": rt.get("iteration", self.stats.iterations),
            "phase": rt.get("phase", ""),
            "cost_total": round(self.stats.cost, 6),
            "tasks": {"pending": pending, "done": done, "blocked": blocked,
                      "runnable": count_runnable(TASKS_DIR)},
            "peak_rss_mb": round(self.stats.peak_rss_kb / 1024, 1),
            "stop_reason": self.stop_reason,
            "sizes": self._sizes(),
        }
        (self.out / "heartbeat.json").write_text(
            json.dumps(hb, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # 时间序列：供 analyze.py 计算断档/增长趋势
        try:
            with (self.out / "metrics.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(hb, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def sample_resources(self, child: subprocess.Popen | None) -> None:
        if child is None or child.poll() is not None:
            return
        procs, rss_kb, cpu = tree_stats(child.pid)
        self.stats.peak_rss_kb = max(self.stats.peak_rss_kb, rss_kb)
        self.resource.write(
            f"{now_iso()},{int(time.time() - self.start_time)},{child.pid},"
            f"{procs},{round(rss_kb / 1024, 1)},{round(cpu, 1)}\n")

    # ── 子进程输出处理 ──

    def handle_line(self, line: str) -> None:
        self.stats.child_line_count += 1
        it = parse_iteration(line)
        if it is not None:
            self.stats.iterations = max(self.stats.iterations, it)
        c = parse_cost(line)
        if c is not None:
            self.stats.cost += c
        if is_guard_rollback(line):
            self.stats.guard_rollbacks += 1
        if is_double_failure(line):
            self.stats.double_failures += 1
        if is_exhausted(line):
            self.stats.exhausted_events += 1
        if is_max_iterations(line):
            self.stats.max_iter_events += 1

    def terminate(self, child: subprocess.Popen, grace: float = 15.0) -> None:
        if child.poll() is not None:
            return
        self.log(f"终止子进程组 pid={child.pid}（先 SIGINT）")
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGINT)
        except Exception:
            child.terminate()
        try:
            child.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        self.log(f"子进程未在 {grace:.0f}s 内退出，SIGKILL")
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        except Exception:
            child.kill()
        try:
            child.wait(timeout=10)
        except Exception:
            pass

    def run_child(self) -> int:
        cmd = self.build_cmd()
        self.log(f"启动子进程: {' '.join(cmd)}")
        restart_log = (self.out / f"restart-{self.stats.restarts}.log").open(
            "a", buffering=1, encoding="utf-8")
        restart_log.write(f"# {now_iso()} {' '.join(cmd)}\n")

        child = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
            env=self._child_env(),
        )

        q: "queue.Queue[str | None]" = queue.Queue()

        def reader() -> None:
            try:
                for ln in child.stdout:  # type: ignore[union-attr]
                    q.put(ln.rstrip("\n"))
            except Exception:
                pass
            q.put(None)

        threading.Thread(target=reader, daemon=True).start()

        last_hb = 0.0
        last_sample = 0.0
        done_reading = False
        while True:
            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                line = ""
            if line is None:
                done_reading = True
            elif line:
                handle = f"[child] {line}"
                self.master.write(f"{now_iso()} {handle}\n")
                restart_log.write(line + "\n")
                self.handle_line(line)

            now = time.time()
            elapsed = now - self.start_time
            if now - last_hb >= self.args.heartbeat_interval:
                self.write_heartbeat(child)
                last_hb = now
            if now - last_sample >= self.args.sample_interval:
                self.sample_resources(child)
                last_sample = now

            # 熔断
            if elapsed >= self.args.hours * 3600:
                self.stop_reason = "time_limit"
                self.log(f"达到时长上限 {self.args.hours}h，优雅停止")
                self.terminate(child)
                break
            if self.stats.cost >= self.args.budget_usd:
                self.stop_reason = "budget"
                self.log(f"达到预算上限 ${self.args.budget_usd}，优雅停止")
                self.terminate(child)
                break
            if self._stop_event.is_set():
                self.stop_reason = self.stop_reason or "signal"
                self.log("收到停止信号，优雅停止")
                self.terminate(child)
                break

            if child.poll() is not None and done_reading:
                break

        # 收尾：排空剩余输出
        while True:
            try:
                line = q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                continue
            self.master.write(f"{now_iso()} [child] {line}\n")
            restart_log.write(line + "\n")
            self.handle_line(line)

        rc = child.wait()
        restart_log.close()
        self.write_heartbeat(None)
        self.log(f"子进程退出，exit_code={rc}")
        self.stats.exit_codes.append(rc)
        return rc

    # ── 决策 ──

    def decide_restart(self, rc: int) -> bool:
        """返回 True 表示应重启子进程，False 表示应结束整体运行。"""
        a = self.args
        if self.stop_reason:
            return False
        if a.once:
            self.stop_reason = "once"
            return False

        runnable = count_runnable(TASKS_DIR)
        if rc == 0:
            # 正常收尾：队列耗尽（引擎归档）或 --no-validator 场景
            if runnable > 0:
                self.log(f"退出码 0 但仍有 {runnable} 个可运行任务，重启继续", "WARN")
                return True
            if a.task_pool and not a.no_refill:
                self.log("队列已空，尝试从 task-pool 补给后继续")
                self.maybe_refill()
                if count_runnable(TASKS_DIR) > 0:
                    return True
            self.stop_reason = "completed"
            self.log("所有任务已完成，结束长跑")
            return False
        if rc == 1:
            self.log("达最大迭代次数，重启新一轮迭代", "WARN")
            return True
        if rc == 2:
            self.log("Agent 连续双失败导致引擎终止，守护重启", "WARN")
            return True
        if rc == 130:
            self.log("子进程报告用户中断（exit 130），守护重启", "WARN")
            return True
        self.log(f"子进程异常退出（exit {rc}），守护重启", "WARN")
        return True

    # ── 主循环 ──

    def run(self) -> int:
        a = self.args
        self.log(f"守护器启动 run_id={self.run_id}")
        self.log(f"  hours={a.hours} budget_usd={a.budget_usd} max_iterations={a.max_iterations} "
                 f"out={self.out}")
        self.log(f"  task_pool={a.task_pool} min_queue={a.min_queue} "
                 f"no_refill={a.no_refill}")
        self.log(f"  langfuse={'on' if a.langfuse else 'off'} env={a.langfuse_env} "
                 f"report={'on' if a.report else 'off'} release={self.run_id}")
        self.write_heartbeat(None)

        while True:
            if time.time() - self.start_time >= a.hours * 3600:
                self.stop_reason = "time_limit"
                break
            if self.stats.cost >= a.budget_usd:
                self.stop_reason = "budget"
                break
            if self.stats.restarts > a.max_restarts:
                self.stop_reason = "restart_limit"
                self.log(f"重启次数超过上限 {a.max_restarts}，停止", "ERROR")
                break

            self.maybe_refill()
            if count_runnable(TASKS_DIR) == 0 and not a.task_pool:
                # 没有可运行任务且无补给来源：若上一轮是完成态则停止，否则继续重启
                pass

            rc = self.run_child()
            if not self.decide_restart(rc):
                break

            self.stats.restarts += 1
            delay = backoff_delay(self.stats.restarts, a.restart_base_delay,
                                  a.restart_max_delay)
            self.log(f"第 {self.stats.restarts} 次重启前等待 {delay:.0f}s（退避）")
            if self._wait_interruptible(delay):
                self.stop_reason = self.stop_reason or "signal"
                break
            self.write_heartbeat(None)

        self.finish()
        return 0 if self.stop_reason in ("completed", "time_limit", "once") else 1

    def _wait_interruptible(self, seconds: float) -> bool:
        """等待 seconds 秒；若收到停止信号返回 True。"""
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_event.is_set():
                return True
            time.sleep(0.2)
        return False

    def finish(self) -> None:
        elapsed = time.time() - self.start_time
        summary = {
            "run_id": self.run_id,
            "started_at": datetime.fromtimestamp(
                self.start_time, timezone.utc).astimezone().isoformat(timespec="seconds"),
            "ended_at": now_iso(),
            "elapsed_s": int(elapsed),
            "elapsed_human": self._fmt(elapsed),
            "hours_limit": self.args.hours,
            "budget_usd": self.args.budget_usd,
            "stop_reason": self.stop_reason,
            **self.stats.to_dict(),
            "run_dir": str(self.out),
        }
        (self.out / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.log(f"长跑结束，原因={self.stop_reason}，总时长={self._fmt(elapsed)}，"
                 f"迭代={self.stats.iterations}，成本=${self.stats.cost:.4f}，"
                 f"重启={self.stats.restarts}，汇总={self.out / 'summary.json'}")

        # 自动生成评测报告（任务完成/熔断时都产出，量化长时表现）
        if self.args.report:
            self._write_report()
        else:
            self.log("已跳过自动评测报告（--no-report）")

        self.master.close()
        self.resource.close()

    def _write_report(self) -> None:
        """调用 analyze 生成 Markdown 报告与指标 JSON。失败不影响主流程。"""
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            import analyze  # noqa: E402
            m = analyze.generate_report(
                runtime=self.out,
                ralph_dir=RALPH_SCRIPT_DIR,
                project_root=PROJECT_ROOT,
                out=Path(self.args.report_out),
                json_out=Path(self.args.metrics_out),
                langfuse_enabled=self.args.langfuse,
                environment=self.args.langfuse_env,
                release=self.run_id,
                judge=self.args.judge,
                judge_model=self.args.judge_model,
                judge_sandbox=Path(self.args.judge_sandbox) if self.args.judge_sandbox else None,
            )
            self.log(f"评测报告已生成：{self.args.report_out}"
                     f"（判定：{m.get('verdict')}）；指标 JSON：{self.args.metrics_out}")
        except Exception as e:  # noqa: BLE001
            self.log(f"评测报告生成失败：{e}", "ERROR")

    @staticmethod
    def _fmt(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h}h{m}m{s}s"


# ══════════════════════════════════════════════════════════════
#  参数解析
# ══════════════════════════════════════════════════════════════

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ralph 泡测守护器（10 小时无人干预）")
    p.add_argument("--hours", type=float, default=10.0, help="总时长上限（小时），默认 10")
    p.add_argument("--budget-usd", type=float, default=20.0, help="累计成本上限（美元），默认 20")
    p.add_argument("--max-iterations", type=int, default=400,
                   help="传给引擎的单轮最大迭代数，默认 400")
    p.add_argument("--run-id", default=None, help="整轮运行标识（跨重启复用），默认时间戳")
    p.add_argument("--max-restarts", type=int, default=50, help="最大重启次数，默认 50")
    p.add_argument("--restart-base-delay", type=float, default=5.0, help="退避基数秒")
    p.add_argument("--restart-max-delay", type=float, default=60.0, help="退避上限秒")
    p.add_argument("--heartbeat-interval", type=float, default=60.0, help="心跳间隔秒")
    p.add_argument("--sample-interval", type=float, default=60.0, help="资源采样间隔秒")
    p.add_argument("--task-pool", default=None, help="任务补给池目录（可选）")
    p.add_argument("--min-queue", type=int, default=10, help="可运行任务低于此值时补给")
    p.add_argument("--refill-batch", type=int, default=50, help="每次补给任务数上限")
    p.add_argument("--no-refill", action="store_true", help="禁用 task-pool 补给")
    p.add_argument("--python", default=sys.executable, help="Python 解释器路径")
    p.add_argument("--ralph-py", default=str(DEFAULT_RALPH_PY), help="ralph.py 路径")
    p.add_argument("--out", default=str(SCRIPT_DIR / "runtime"), help="输出目录（日志/心跳/汇总）")
    p.add_argument("--langfuse", action=argparse.BooleanOptionalAction, default=True,
                   help="开启 Langfuse 上报（默认开；--no-langfuse 关闭）")
    p.add_argument("--langfuse-env", default="ralph", help="Langfuse environment 标签，默认 ralph")
    p.add_argument("--report", action=argparse.BooleanOptionalAction, default=True,
                   help="结束时自动生成评测报告（默认开；--no-report 关闭）")
    p.add_argument("--report-out", default=str(PROJECT_ROOT / "docs" / "soak-report.md"),
                   help="Markdown 评测报告输出路径")
    p.add_argument("--metrics-out", default=str(PROJECT_ROOT / "docs" / "soak-metrics.json"),
                   help="指标 JSON 输出路径")
    p.add_argument("--judge", action="store_true",
                   help="结束时启用 LLM-as-judge（事实正确率/证据充分度，会额外调用模型）")
    p.add_argument("--judge-model", default=None, help="裁判模型")
    p.add_argument("--judge-sandbox", default=None, help="裁判读取的产出目录，默认仓库根")
    p.add_argument("--with-dashboard", action="store_true", help="启用监控面板（默认关闭）")
    p.add_argument("--port", type=int, default=17331, help="监控面板端口（仅 --with-dashboard）")
    p.add_argument("--no-validator", action="store_true", help="透传 --no-validator")
    p.add_argument("--no-pm", action="store_true", help="透传 --no-pm")
    p.add_argument("--no-guard", action="store_true", help="透传 --no-guard")
    p.add_argument("--extra", action="append", default=[], help="追加透传给引擎的参数，可重复")
    p.add_argument("--once", action="store_true", help="只运行一轮子进程后结束（调试）")
    p.add_argument("--print-plan", action="store_true", help="只打印将要执行的命令，不运行")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.print_plan:
        tmp = Supervisor.__new__(Supervisor)  # 不触碰文件系统
        tmp.args = args
        tmp.run_id = args.run_id or "soak-<timestamp>"
        print("# 守护器计划")
        print(f"  时长上限: {args.hours}h    预算上限: ${args.budget_usd}")
        print(f"  输出目录: {Path(args.out).resolve()}")
        print(f"  Langfuse: {'on' if args.langfuse else 'off'} (env={args.langfuse_env})"
              f"    报告: {'on' if args.report else 'off'} -> {args.report_out}")
        print(f"  每条引擎命令:\n    {' '.join(tmp.build_cmd())}")
        print("移除 --print-plan 即开始实跑。")
        return 0

    sup = Supervisor(args)
    if not Path(args.ralph_py).exists():
        sup.log(f"ralph.py 不存在: {args.ralph_py}", "ERROR")
        return 2

    def _on_signal(signum, _frame):
        sup._stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except Exception:
            pass

    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
