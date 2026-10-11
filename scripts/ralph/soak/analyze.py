#!/usr/bin/env python3
"""
Ralph 长跑（soak）评测报告器。

读取守护器产物（summary.json / metrics.jsonl / master.log / heartbeat.json /
resource.csv）+ Ralph 运行时终态（tasks/、state.json、progress.txt、AGENTS.md、
docs/patterns-*.md）+ Langfuse observations，量化「本项目在长时任务上的表现」，
输出结构化 JSON 与 Markdown 报告，并按验收阈值给出判定。

可独立运行，也可由 soak.py 在长跑结束时自动调用。

用法：
  python3 scripts/ralph/soak/analyze.py --out docs/soak-report.md
  python3 scripts/ralph/soak/analyze.py --runtime scripts/ralph/soak/runtime \
      --json docs/soak-metrics.json --no-langfuse
  python3 scripts/ralph/soak/analyze.py --release soak-20261010-1200 --environment ralph
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/ralph/soak
RALPH_DIR = SCRIPT_DIR.parent                          # scripts/ralph
PROJECT_ROOT = RALPH_DIR.parent.parent                 # 仓库根
DEFAULT_RUNTIME = SCRIPT_DIR / "runtime"

# 复用同目录守护器的解析函数，避免口径分叉
sys.path.insert(0, str(SCRIPT_DIR))
import soak  # noqa: E402

sys.path.insert(0, str(RALPH_DIR))
import ralph  # noqa: E402


# ══════════════════════════════════════════════════════════════
#  验收阈值（默认对齐 doc/acceptance.md §5.1）
# ══════════════════════════════════════════════════════════════

# (metric_key, op, threshold, label)   op ∈ {">=", "<=", "<", ">"}
DEFAULT_THRESHOLDS: list[tuple[str, str, float, str]] = [
    ("uptime_pct", ">=", 100.0, "守护存活率 %"),
    ("max_gap_s", "<", 300.0, "心跳最大断档 (s)"),
    ("iterations", ">=", 150, "完成迭代数"),
    ("done_ok", ">=", 60, "完成任务数"),
    ("overclaim_rate", "<=", 0.0, "伪完成率 %"),
    ("guard_corruption", "<=", 0, "任务树/审计损坏次数"),
    ("peak_rss_mb", "<", 2048.0, "子进程峰值 RSS (MB)"),
    ("cost_total", "<", 20.0, "累计成本 ($)"),
    ("restarts_recovered", ">=", 1.0, "故障恢复次数"),
]


# ══════════════════════════════════════════════════════════════
#  通用读取
# ══════════════════════════════════════════════════════════════

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def load_heartbeats(runtime: Path) -> list[dict]:
    """时间序列优先读 metrics.jsonl；缺失时退回单条 heartbeat.json。"""
    rows: list[dict] = []
    jl = runtime / "metrics.jsonl"
    if jl.exists():
        for line in jl.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    if not rows:
        hb = read_json(runtime / "heartbeat.json", None)
        if isinstance(hb, dict):
            rows.append(hb)
    return rows


def filter_heartbeats(rows: list[dict], run_id: str | None) -> list[dict]:
    """只保留属于该 run_id 的心跳（无 run_id 字段的旧格式保留）。"""
    if not run_id:
        return rows
    return [h for h in rows if h.get("run_id") in (run_id, None, "")]


# ══════════════════════════════════════════════════════════════
#  master.log 解析（纯函数，便于单测）
# ══════════════════════════════════════════════════════════════

# 子串 → 指标键。全部用引擎/守护器的固定文案，避免误计。
_MARKERS: list[tuple[str, str]] = [
    ("启动子进程:", "child_starts"),
    ("连续两次失败", "double_failures"),
    ("PM 改动越界（", "guard_rollbacks"),
    ("已达最大迭代次数", "max_iter_events"),
    ("所有任务已完成", "exhausted_events"),
    ("总时长超时", "timeouts_total"),
    ("首字响应超时", "timeouts_first_token"),
    ("持续无输出超时", "timeouts_idle"),
    ("模型无响应", "agent_idle_failures"),
    ("模型返回错误", "agent_api_failures"),
    ("进程异常退出", "process_errors"),
]


def parse_master_lines(lines: list[str]) -> dict:
    """从 master.log 行集合汇总关键事件与进度。"""
    stats = {key: 0 for _sub, key in _MARKERS}
    max_iteration = 0
    cost = 0.0
    turns = 0
    for ln in lines:
        it = soak.parse_iteration(ln)
        if it is not None and it > max_iteration:
            max_iteration = it
        c = soak.parse_cost(ln)
        if c is not None:
            cost += c
            turns += 1
        for sub, key in _MARKERS:
            if sub in ln:
                stats[key] += 1
    stats["iterations_from_log"] = max_iteration
    stats["cost_from_log"] = round(cost, 6)
    stats["cost_turns"] = turns
    return stats


def parse_master_log(path: Path) -> dict:
    if not path.exists():
        return parse_master_lines([])
    return parse_master_lines(path.read_text(encoding="utf-8", errors="replace").splitlines())


# ── 本地工具 / 证据 / 阶段 / 治理 解析（纯函数） ──

_TOOL_CALL_RE = re.compile(r"\[Tool\]\s+(\w+)\s*:")
_TOOL_ERROR_RE = re.compile(r"\[Tool Error\]\s+(\w+)")
_VERIFY_RE = re.compile(
    r"\[Tool\] bash:.*\b(unittest|pytest|npm test|npm run test|typecheck|lint|jest|go test|cargo test)\b",
    re.IGNORECASE,
)
_PHASE_MARKERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"迭代\s+\d+/\d+\s*-\s*开发 Agent"), "developer"),
    (re.compile(r"迭代\s+\d+\s*-\s*Validator 验证"), "validator"),
    (re.compile(r"迭代\s+\d+\s*-\s*PM Agent 治理"), "pm"),
]
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:.+-]+)")


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


def parse_tool_stats(lines: list[str]) -> dict:
    """本地工具调用分布与错误率（从 master.log 的 [Tool]/[Tool Error] 行）。"""
    calls: dict[str, int] = {}
    errors = 0
    for ln in lines:
        m = _TOOL_CALL_RE.search(ln)
        if m:
            calls[m.group(1)] = calls.get(m.group(1), 0) + 1
        if _TOOL_ERROR_RE.search(ln):
            errors += 1
    total = sum(calls.values())
    return {
        "tool_calls": dict(sorted(calls.items(), key=lambda kv: -kv[1])),
        "total_tool_calls": total,
        "tool_errors": errors,
        "tool_error_rate": round(errors / total * 100, 1) if total else 0.0,
        "used_write_or_edit": bool(calls.get("write") or calls.get("edit")),
    }


def parse_evidence_stats(lines: list[str]) -> dict:
    """验证证据：运行中是否真的执行过测试/检查（groundedness 的确定性代理）。"""
    n = sum(1 for ln in lines if _VERIFY_RE.search(ln))
    return {"verification_commands": n, "has_verification": n > 0}


def parse_guard_corruption(lines: list[str], adjustments_path: Path | None) -> int:
    """真实检测越权防腐的**硬失败**（而非回滚成功）。

    - 回滚时无备份可用（"无 tasks 备份可用，无法回滚"）；
    - adjustments 审计日志损坏（非法 JSON 或非列表）。
    成功回滚不算损坏（由 guard_rollbacks 单独统计）。
    """
    corrupt = 0
    for ln in lines:
        if "无 tasks 备份可用，无法回滚" in ln:
            corrupt += 1
    if adjustments_path and Path(adjustments_path).exists():
        try:
            data = json.loads(Path(adjustments_path).read_text(encoding="utf-8"))
            if not isinstance(data, list):
                corrupt += 1
        except Exception:
            corrupt += 1
    return corrupt


def parse_adjustments(path: Path | None) -> dict:
    """PM 任务队列调整的动作计数（协作级 / 治理审计）。"""
    actions: dict[str, int] = {}
    total = resets = splits = news = 0
    data = read_json(Path(path), []) if path else []
    if isinstance(data, list):
        for rec in data:
            if not isinstance(rec, dict):
                continue
            a = str(rec.get("action", "?"))
            actions[a] = actions.get(a, 0) + 1
            total += 1
            if a == "reset":
                resets += 1
            elif a == "split":
                splits += 1
            elif a in ("add", "new"):
                news += 1
    return {
        "pm_adjustments": dict(sorted(actions.items(), key=lambda kv: -kv[1])),
        "pm_adjustments_total": total,
        "pm_resets": resets,
        "pm_splits": splits,
        "pm_new_tasks": news,
    }


def parse_phase_timings(lines: list[str]) -> dict:
    """从带时间戳的 master.log 估计三阶段耗时与单迭代耗时分布。"""
    events: list[tuple[float, str]] = []
    last_ts: float | None = None
    for ln in lines:
        if "[child]" not in ln:      # 只看引擎子进程输出，排除守护器自身日志
            continue
        m = _TS_RE.match(ln)
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        if ts is None:
            continue
        last_ts = ts
        for pat, phase in _PHASE_MARKERS:
            if pat.search(ln):
                events.append((ts, phase))
                break

    dur: dict[str, list[float]] = {"developer": [], "validator": [], "pm": []}
    for i, (ts, ph) in enumerate(events):
        end = events[i + 1][0] if i + 1 < len(events) else (last_ts if last_ts else ts)
        d = end - ts
        if 0 <= d < 3 * 3600:
            dur[ph].append(d)
    dev_times = [ts for ts, ph in events if ph == "developer"]
    iter_dur = [dev_times[i + 1] - dev_times[i] for i in range(len(dev_times) - 1)]
    iter_dur = [d for d in iter_dur if 0 <= d < 3 * 3600]

    return {
        "phase_developer_s_p50": _percentile(dur["developer"], 0.5),
        "phase_developer_s_p95": _percentile(dur["developer"], 0.95),
        "phase_validator_s_p50": _percentile(dur["validator"], 0.5),
        "phase_validator_s_p95": _percentile(dur["validator"], 0.95),
        "phase_pm_s_p50": _percentile(dur["pm"], 0.5),
        "phase_pm_s_p95": _percentile(dur["pm"], 0.95),
        "iter_seconds_p50": _percentile(iter_dur, 0.5),
        "iter_seconds_p95": _percentile(iter_dur, 0.95),
        "iter_seconds_n": len(iter_dur),
    }


def trend_series(heartbeats: list[dict]) -> dict:
    """从心跳时间序列估计成本/体积的增长斜率（真正的随时间趋势）。"""
    pts: list[dict] = []
    for h in heartbeats:
        t = _parse_ts(h.get("ts", ""))
        if t is None:
            continue
        sizes = h.get("sizes") or {}
        pts.append({
            "t": t,
            "cost": float(h.get("cost_total") or 0.0),
            "iter": int(h.get("iteration") or 0),
            "progress": int(sizes.get("progress_bytes", 0) or 0),
            "state": int(sizes.get("state_json_bytes", 0) or 0),
        })
    if len(pts) < 2:
        return {"cost_slope_usd_per_h": 0.0, "cost_per_iteration_trend": 0.0,
                "progress_slope_bytes_per_h": 0.0, "state_slope_bytes_per_h": 0.0,
                "points": len(pts)}

    def slope(key: str) -> float:
        dt = (pts[-1]["t"] - pts[0]["t"]) / 3600.0
        return round((pts[-1][key] - pts[0][key]) / dt, 2) if dt > 0 else 0.0

    d_iter = pts[-1]["iter"] - pts[0]["iter"]
    cpi = (pts[-1]["cost"] - pts[0]["cost"]) / d_iter if d_iter > 0 else 0.0
    return {
        "cost_first": round(pts[0]["cost"], 4),
        "cost_last": round(pts[-1]["cost"], 4),
        "cost_slope_usd_per_h": slope("cost"),
        "cost_per_iteration_trend": round(cpi, 4),
        "progress_slope_bytes_per_h": slope("progress"),
        "state_slope_bytes_per_h": slope("state"),
        "points": len(pts),
    }


# ══════════════════════════════════════════════════════════════
#  心跳断档 / 可用性（纯函数）
# ══════════════════════════════════════════════════════════════

def _parse_ts(s: str) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def heartbeat_gaps(heartbeats: list[dict]) -> dict:
    """计算心跳间隔断档。max_gap_s 为相邻心跳最大间隔（秒）。"""
    ts = [_parse_ts(h.get("ts", "")) for h in heartbeats]
    ts = [t for t in ts if t is not None]
    if len(ts) < 2:
        return {"heartbeats": len(heartbeats), "max_gap_s": 0.0,
                "downtime_s": 0.0, "uptime_pct": 100.0, "elapsed_s": 0.0}
    ts.sort()
    elapsed = ts[-1] - ts[0]
    # 正常间隔取中位数；间隔 > max(3*中位, 30s) 视为断档
    deltas = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    median = sorted(deltas)[len(deltas) // 2] if deltas else 0.0
    threshold = max(3 * median, 30.0)
    downtime = sum(d for d in deltas if d > threshold)
    max_gap = max(deltas) if deltas else 0.0
    uptime = 100.0 if elapsed <= 0 else max(0.0, 100.0 * (1 - downtime / elapsed))
    return {
        "heartbeats": len(heartbeats),
        "max_gap_s": round(max_gap, 1),
        "downtime_s": round(downtime, 1),
        "uptime_pct": round(uptime, 2),
        "elapsed_s": round(elapsed, 1),
    }


# ══════════════════════════════════════════════════════════════
#  任务终态
# ══════════════════════════════════════════════════════════════

def scan_tasks(tasks_dir: Path, done_dir: Path) -> dict:
    """扫描任务终态，复用 ralph 的 frontmatter 解析口径。"""
    pending = []
    done = []
    for f in sorted(tasks_dir.glob("*.md")):
        m = ralph.parse_frontmatter(f)
        m["_id"] = str(m.get("id", f.stem))
        pending.append(m)
    for f in sorted(done_dir.glob("*.md")):
        m = ralph.parse_frontmatter(f)
        m["_id"] = str(m.get("id", f.stem))
        done.append(m)

    blocked = [t for t in (pending + done) if t.get("blocked")]
    done_ok = [t for t in done if not t.get("blocked")]
    first_pass = [t for t in done_ok if int(t.get("retryCount", 0) or 0) == 0]
    total = len(pending) + len(done)

    retry_dist: dict[str, int] = {}
    for t in done + pending:
        rc = int(t.get("retryCount", 0) or 0)
        key = str(rc) if rc < 5 else "5+"
        retry_dist[key] = retry_dist.get(key, 0) + 1
    retry_dist = dict(sorted(retry_dist.items(), key=lambda kv: (kv[0] == "5+", kv[0])))

    def rate(num: int, den: int) -> float:
        return round(num / den * 100, 1) if den else 0.0

    return {
        "total": total,
        "pending": len(pending),
        "done": len(done),
        "done_ok": len(done_ok),
        "blocked": len(blocked),
        "completion_rate": rate(len(done_ok), total),
        "first_pass_rate": rate(len(first_pass), len(done_ok)),
        "blocked_rate": rate(len(blocked), total),
        "retry_distribution": retry_dist,
    }


# ══════════════════════════════════════════════════════════════
#  上下文 / 存储体积（长跑膨胀观测）
# ══════════════════════════════════════════════════════════════

def file_sizes(ralph_dir: Path, project_root: Path) -> dict:
    def size(p: Path) -> int:
        try:
            return p.stat().st_size
        except Exception:
            return 0

    patterns = project_root / "docs"
    pat_files = sorted(patterns.glob("patterns-*.md")) if patterns.exists() else []
    pat_lines = 0
    for f in pat_files:
        pat_lines += len(read_text(f).splitlines())

    return {
        "state_json_bytes": size(ralph_dir / "state.json"),
        "progress_bytes": size(ralph_dir / "progress.txt"),
        "adjustments_bytes": size(ralph_dir / "adjustments.json"),
        "agents_md_bytes": size(project_root / "AGENTS.md"),
        "patterns_files": len(pat_files),
        "patterns_lines": pat_lines,
    }


# ══════════════════════════════════════════════════════════════
#  资源趋势（纯函数）
# ══════════════════════════════════════════════════════════════

def resource_trend(heartbeats: list[dict]) -> dict:
    """从心跳序列估计峰值 RSS 与增长斜率（MB/小时）。"""
    peaks = [(h.get("peak_rss_mb"), _parse_ts(h.get("ts", "")))
             for h in heartbeats if isinstance(h.get("peak_rss_mb"), (int, float))]
    if not peaks:
        return {"peak_rss_mb": 0.0, "rss_slope_mb_per_h": 0.0}
    peak = max(p for p, _ in peaks)
    valid = [(p, t) for p, t in peaks if t is not None]
    slope = 0.0
    if len(valid) >= 2 and valid[-1][1] > valid[0][1]:
        hours = (valid[-1][1] - valid[0][1]) / 3600.0
        if hours > 0:
            slope = round((valid[-1][0] - valid[0][0]) / hours, 2)
    return {"peak_rss_mb": round(peak, 1), "rss_slope_mb_per_h": slope}


# ══════════════════════════════════════════════════════════════
#  Langfuse
# ══════════════════════════════════════════════════════════════

def _to_utc_z(iso: str) -> str:
    """把任意 ISO8601 时间转成 Langfuse 需要的 UTC `...Z`。"""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return iso


def _shift_minutes(iso_utc: str, minutes: int) -> str:
    """把 UTC `...Z` 时间平移若干分钟，解析失败时原样返回。"""
    try:
        dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        dt = dt + timedelta(minutes=minutes)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return iso_utc


def _lf_auth(cfg: dict) -> str:
    return "Basic " + base64.b64encode(
        f"{cfg['publicKey']}:{cfg['secretKey']}".encode()).decode()


def _lf_metrics_query(cfg: dict, query: dict) -> list[dict]:
    """调用 Langfuse v2 Metrics API，返回 data 行。"""
    sys.path.insert(0, str(RALPH_DIR))
    import eval_langfuse  # noqa: E402
    url = cfg["baseUrl"].rstrip("/") + "/api/public/v2/metrics?" + urllib.parse.urlencode(
        {"query": json.dumps(query)})
    data = eval_langfuse._http_get_json(url, _lf_auth(cfg))
    if not isinstance(data, dict) or "data" not in data:
        raise RuntimeError(f"unexpected metrics response: {str(data)[:200]}")
    return data.get("data", [])


def _fetch_via_metrics(cfg: dict, frm: str, to: str, environment: str | None,
                       release: str | None) -> dict:
    window = {"fromTimestamp": frm, "toTimestamp": to}
    rowlimit = {"config": {"row_limit": 1000}}

    def collect(use_release: bool) -> dict:
        filters: list[dict] = []
        if environment:
            filters.append({"column": "environment", "operator": "=",
                            "value": environment, "type": "string"})
        if use_release and release:
            filters.append({"column": "release", "operator": "=",
                            "value": release, "type": "string"})

        def query(metrics, dimensions, extra_filters=None, order_by=None):
            q = {"view": "observations", "metrics": metrics, "dimensions": dimensions,
                 "filters": filters + (extra_filters or []), **window, **rowlimit}
            if order_by:
                q["orderBy"] = order_by
            return _lf_metrics_query(cfg, q)

        by_type_rows = query(
            [{"measure": "count", "aggregation": "count"},
             {"measure": "latency", "aggregation": "p50"},
             {"measure": "latency", "aggregation": "p95"},
             {"measure": "latency", "aggregation": "p99"},
             {"measure": "timeToFirstToken", "aggregation": "p50"},
             {"measure": "timeToFirstToken", "aggregation": "p95"},
             {"measure": "totalTokens", "aggregation": "sum"},
             {"measure": "inputTokens", "aggregation": "sum"},
             {"measure": "outputTokens", "aggregation": "sum"},
             {"measure": "totalCost", "aggregation": "sum"},
             {"measure": "inputCost", "aggregation": "sum"},
             {"measure": "outputCost", "aggregation": "sum"}],
            [{"field": "type"}])
        by_type: dict[str, int] = {}
        lat: dict[str, dict] = {}
        total_cost = total_in_cost = total_out_cost = 0.0
        total_tokens = total_in_tok = total_out_tok = 0.0
        observations = 0
        for r in by_type_rows:
            t = str(r.get("type", "UNKNOWN"))
            n = int(r.get("count_count", 0) or 0)
            by_type[t] = n
            observations += n
            total_cost += float(r.get("sum_totalCost", 0) or 0)
            total_in_cost += float(r.get("sum_inputCost", 0) or 0)
            total_out_cost += float(r.get("sum_outputCost", 0) or 0)
            total_tokens += float(r.get("sum_totalTokens", 0) or 0)
            total_in_tok += float(r.get("sum_inputTokens", 0) or 0)
            total_out_tok += float(r.get("sum_outputTokens", 0) or 0)
            lat[t] = {
                "p50": round(float(r.get("p50_latency", 0) or 0) / 1000, 3),
                "p95": round(float(r.get("p95_latency", 0) or 0) / 1000, 3),
                "p99": round(float(r.get("p99_latency", 0) or 0) / 1000, 3),
                "ttft_p50": round(float(r.get("p50_timeToFirstToken", 0) or 0) / 1000, 3),
                "ttft_p95": round(float(r.get("p95_timeToFirstToken", 0) or 0) / 1000, 3),
            }

        by_role: dict[str, dict] = {}
        for r in query(
                [{"measure": "count", "aggregation": "count"},
                 {"measure": "latency", "aggregation": "p95"},
                 {"measure": "latency", "aggregation": "sum"},
                 {"measure": "totalCost", "aggregation": "sum"},
                 {"measure": "totalTokens", "aggregation": "sum"},
                 {"measure": "inputTokens", "aggregation": "sum"},
                 {"measure": "outputTokens", "aggregation": "sum"}],
                [{"field": "userId"}],
                extra_filters=[{"column": "type", "operator": "=",
                                "value": "GENERATION", "type": "string"}],
                order_by=[{"field": "count_count", "direction": "desc"}]):
            role = str(r.get("userId", "") or "(none)")
            by_role[role] = {
                "generations": int(r.get("count_count", 0) or 0),
                "gen_latency_p95": round(float(r.get("p95_latency", 0) or 0) / 1000, 3),
                "gen_latency_sum_s": round(float(r.get("sum_latency", 0) or 0) / 1000, 1),
                "total_cost": round(float(r.get("sum_totalCost", 0) or 0), 6),
                "total_tokens": int(r.get("sum_totalTokens", 0) or 0),
                "input_tokens": int(r.get("sum_inputTokens", 0) or 0),
                "output_tokens": int(r.get("sum_outputTokens", 0) or 0),
            }

        tool_names: dict[str, int] = {}
        for r in query([{"measure": "count", "aggregation": "count"}],
                       [{"field": "name"}],
                       extra_filters=[{"column": "type", "operator": "=",
                                       "value": "TOOL", "type": "string"}]):
            name = str(r.get("name", "")).replace("Tool:", "").strip()
            tool_names[name] = tool_names.get(name, 0) + int(r.get("count_count", 0) or 0)

        # 工具专属错误（type=TOOL 且 level=ERROR），区别于整体错误率
        tool_errors = 0
        for r in query([{"measure": "count", "aggregation": "count"}],
                       [{"field": "name"}],
                       extra_filters=[{"column": "type", "operator": "=",
                                       "value": "TOOL", "type": "string"},
                                      {"column": "level", "operator": "=",
                                       "value": "ERROR", "type": "string"}]):
            tool_errors += int(r.get("count_count", 0) or 0)

        level_counts: dict[str, int] = {}
        for r in query([{"measure": "count", "aggregation": "count"}],
                       [{"field": "level"}]):
            level_counts[str(r.get("level", "?"))] = int(r.get("count_count", 0) or 0)

        gen = lat.get("GENERATION", {})

        def pct(num, den):
            return round(num / den * 100, 1) if den else 0.0

        return {
            "available": True,
            "source": "metrics-v2",
            "window": {"from": frm, "to": to},
            "observations": observations,
            "by_type": by_type,
            "generations": by_type.get("GENERATION", 0),
            "turns": by_type.get("GENERATION", 0),
            "tools": by_type.get("TOOL", 0),
            "spans": by_type.get("SPAN", 0),
            "error_count": level_counts.get("ERROR", 0),
            "error_rate": pct(level_counts.get("ERROR", 0), observations),
            "gen_latency_p50": gen.get("p50", 0.0),
            "gen_latency_p95": gen.get("p95", 0.0),
            "gen_latency_p99": gen.get("p99", 0.0),
            "ttft_p50": gen.get("ttft_p50", 0.0),
            "ttft_p95": gen.get("ttft_p95", 0.0),
            "tool_latency_p95": lat.get("TOOL", {}).get("p95", 0.0),
            "tool_names": dict(sorted(tool_names.items(), key=lambda kv: -kv[1])),
            "tool_errors": tool_errors,
            "tool_error_rate": pct(tool_errors, by_type.get("TOOL", 0)),
            "total_price": round(total_cost, 6),
            "input_cost": round(total_in_cost, 6),
            "output_cost": round(total_out_cost, 6),
            "total_tokens": int(total_tokens),
            "input_tokens": int(total_in_tok),
            "output_tokens": int(total_out_tok),
            "by_role": by_role,
            "level_counts": level_counts,
        }

    result = collect(use_release=True)
    if result["observations"] == 0 and release:
        # release 标签可能缺失；回退到「环境 + 时间窗」，并标注以免与其它运行混淆
        fallback = collect(use_release=False)
        if fallback["observations"] > 0:
            fallback["release_fallback"] = True
            return fallback
    return result


def _fetch_via_observations(cfg: dict, frm: str, to: str, environment: str | None,
                            release: str | None, days: int,
                            limit: int, max_pages: int) -> dict:
    """fallback：分页拉 observation 明细后聚合（较慢，有上限）。"""
    sys.path.insert(0, str(RALPH_DIR))
    import eval_langfuse  # noqa: E402
    rows = eval_langfuse.fetch_observations(
        cfg, frm, to, environment, release, None, limit, max_pages)
    m = eval_langfuse.compute_metrics(rows)
    by_role: dict[str, dict] = {}
    for role in sorted({str(r.get("userId", "")) for r in rows}):
        sub = [r for r in rows if str(r.get("userId", "")) == role]
        rm = eval_langfuse.compute_metrics(sub)
        by_role[role or "(none)"] = {
            "generations": rm.get("generations", 0),
            "gen_latency_p95": rm.get("gen_latency_p95", 0.0),
            "gen_latency_sum_s": 0.0,
            "total_cost": rm.get("total_price", 0.0),
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
    m["by_role"] = by_role
    m["available"] = True
    m["source"] = "observations"
    m["window"] = {"from": frm, "to": to}
    m.setdefault("total_tokens", 0)
    return m


def fetch_langfuse(release: str | None, environment: str | None,
                   frm: str | None = None, to: str | None = None,
                   days: int = 7, enabled: bool = True,
                   limit: int = 100, max_pages: int = 10) -> dict:
    """拉取并按运行聚合 Langfuse 指标；优先 v2 Metrics API，失败回退 observation。"""
    if not enabled:
        return {"available": False, "reason": "disabled"}
    try:
        sys.path.insert(0, str(RALPH_DIR))
        import eval_langfuse  # noqa: E402
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"import failed: {e}"}
    cfg = eval_langfuse.load_config()
    if cfg is None:
        return {"available": False, "reason": "no credentials"}
    if not frm:
        frm = eval_langfuse._iso(days)
    if not to:
        to = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        return _fetch_via_metrics(cfg, frm, to, environment, release)
    except Exception as e:  # noqa: BLE001
        metrics_err = str(e)
    try:
        out = _fetch_via_observations(cfg, frm, to, environment, release, days, limit, max_pages)
        out["metrics_api_error"] = metrics_err
        return out
    except Exception as e:  # noqa: BLE001
        return {"available": False,
                "reason": f"metrics: {metrics_err}; observations: {e}"}


# ══════════════════════════════════════════════════════════════
#  阈值判定（纯函数）
# ══════════════════════════════════════════════════════════════

def _cmp(op: str, value: float, threshold: float) -> bool:
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == ">":
        return value > threshold
    if op == "<":
        return value < threshold
    return False


def evaluate_thresholds(values: dict, thresholds: list[tuple[str, str, float, str]]) -> list[dict]:
    """逐条判定。值为 None 的指标跳过（不计入 overall）。"""
    out = []
    for key, op, th, label in thresholds:
        v = values.get(key)
        if v is None:
            out.append({"key": key, "label": label, "value": None,
                        "op": op, "threshold": th, "ok": None})
            continue
        ok = _cmp(op, float(v), th)
        out.append({"key": key, "label": label, "value": v,
                    "op": op, "threshold": th, "ok": ok})
    return out


def overall_verdict(checks: list[dict]) -> str:
    evaluated = [c for c in checks if c["ok"] is not None]
    if not evaluated:
        return "UNKNOWN"
    if all(c["ok"] for c in evaluated):
        return "PASS"
    # 数据/质量类未过 → FAIL；仅资源/成本类未过 → CONDITIONAL
    hard = {"uptime_pct", "max_gap_s", "overclaim_rate", "guard_corruption"}
    if any((not c["ok"]) and c["key"] in hard for c in evaluated):
        return "FAIL"
    return "CONDITIONAL PASS"


# ══════════════════════════════════════════════════════════════
#  指标汇总
# ══════════════════════════════════════════════════════════════

def compute_all(runtime: Path, ralph_dir: Path, project_root: Path,
                langfuse: dict, thresholds: list[tuple[str, str, float, str]]) -> dict:
    summary = read_json(runtime / "summary.json", {})
    heartbeats = load_heartbeats(runtime)
    heartbeats = filter_heartbeats(heartbeats, summary.get("run_id"))
    log_path = runtime / "master.log"
    lines = (log_path.read_text(encoding="utf-8", errors="replace").splitlines()
             if log_path.exists() else [])
    master = parse_master_lines(lines)
    tasks = scan_tasks(ralph_dir / "tasks", ralph_dir / "tasks" / "done")
    sizes = file_sizes(ralph_dir, project_root)
    avail = heartbeat_gaps(heartbeats)
    res = resource_trend(heartbeats)
    tool = parse_tool_stats(lines)
    evidence = parse_evidence_stats(lines)
    phase = parse_phase_timings(lines)
    trend = trend_series(heartbeats)
    adj_path = ralph_dir / "adjustments.json"
    adj = parse_adjustments(adj_path)
    guard_corruption = parse_guard_corruption(lines, adj_path)

    elapsed_s = float(summary.get("elapsed_s") or avail.get("elapsed_s") or 0.0)
    hours = elapsed_s / 3600.0 if elapsed_s else 0.0
    iterations = int(summary.get("iterations") or master.get("iterations_from_log") or 0)
    cost_total = float(summary.get("cost_total") or master.get("cost_from_log") or 0.0)
    restarts = int(summary.get("restarts") or 0)
    cost_turns = int(master.get("cost_turns") or 0)

    def per_hour(x: float) -> float:
        return round(x / hours, 2) if hours > 0 else 0.0

    def per_iter(x: int) -> float:
        return round(x / iterations, 2) if iterations else 0.0

    timeouts_total = master.get("timeouts_total", 0)
    timeouts_all = (master.get("timeouts_first_token", 0) + master.get("timeouts_idle", 0)
                    + timeouts_total)
    agent_failures = master.get("agent_idle_failures", 0) + master.get("agent_api_failures", 0)

    metrics = {
        # 运行概览
        "stop_reason": summary.get("stop_reason"),
        "elapsed_s": round(elapsed_s, 1),
        "elapsed_hours": round(hours, 2),
        "iterations": iterations,
        # 可用性
        **avail,
        "restarts": restarts,
        "restarts_recovered": restarts,
        "exit_codes": summary.get("exit_codes", []),
        "mtbf_s": round(elapsed_s / restarts, 1) if restarts else round(elapsed_s, 1),
        "restart_rate_per_hour": per_hour(restarts),
        # 吞吐
        "iterations_per_hour": per_hour(iterations),
        "done_ok": tasks["done_ok"],
        "done_per_hour": per_hour(tasks["done_ok"]),
        "done_per_iteration": round(tasks["done_ok"] / iterations, 3) if iterations else 0.0,
        "avg_iter_seconds": round(elapsed_s / iterations, 1) if iterations else 0.0,
        # 任务质量
        **{k: tasks[k] for k in ("total", "pending", "done", "blocked",
                                 "completion_rate", "first_pass_rate",
                                 "blocked_rate", "retry_distribution")},
        # 失败分类与率
        "double_failures": summary.get("double_failures", master.get("double_failures", 0)),
        "guard_rollbacks": summary.get("guard_rollbacks", master.get("guard_rollbacks", 0)),
        "guard_corruption": guard_corruption,
        "guard_rollback_rate_per_100_iter": round(
            master.get("guard_rollbacks", 0) / iterations * 100, 2) if iterations else 0.0,
        "max_iter_events": master.get("max_iter_events", 0),
        "exhausted_events": master.get("exhausted_events", 0),
        "timeouts_total": timeouts_total,
        "timeouts_first_token": master.get("timeouts_first_token", 0),
        "timeouts_idle": master.get("timeouts_idle", 0),
        "timeouts_all": timeouts_all,
        "timeout_rate": round(timeouts_all / iterations * 100, 1) if iterations else 0.0,
        "agent_idle_failures": master.get("agent_idle_failures", 0),
        "agent_api_failures": master.get("agent_api_failures", 0),
        "process_errors": master.get("process_errors", 0),
        "agent_failure_rate": round(agent_failures / iterations * 100, 1) if iterations else 0.0,
        # 工具级（本地）
        **tool,
        # 证据/groundedness 代理（本地）
        **evidence,
        # 阶段耗时与迭代分布（本地）
        **phase,
        # 治理/协作（本地）
        **adj,
        # 成本（本地）
        "cost_total": round(cost_total, 4),
        "cost_per_hour": per_hour(cost_total),
        "cost_per_done": round(cost_total / tasks["done_ok"], 4) if tasks["done_ok"] else 0.0,
        "cost_per_iteration": round(cost_total / iterations, 4) if iterations else 0.0,
        "cost_turns": cost_turns,
        "cost_per_turn": round(cost_total / cost_turns, 6) if cost_turns else 0.0,
        # 资源与体积
        **res,
        **sizes,
        # 长跑趋势（随时间）
        **trend,
        # 诚实性（无 expectations 时不可算）
        "overclaim_rate": None,
    }

    checks = evaluate_thresholds(metrics, thresholds)
    metrics["verdict"] = overall_verdict(checks)
    metrics["checks"] = checks
    metrics["langfuse"] = langfuse
    return metrics


# ══════════════════════════════════════════════════════════════
#  报告渲染
# ══════════════════════════════════════════════════════════════

def _fmt_check(c: dict) -> str:
    if c["ok"] is None:
        mark = "➖"
        val = "n/a"
    else:
        mark = "✅" if c["ok"] else "❌"
        val = c["value"]
    return f"| {c['label']} | {val} | `{c['op']} {c['threshold']}` | {mark} |"


def build_report(m: dict, run_id: str) -> str:
    L: list[str] = []
    A = L.append
    A("# Ralph 长跑（soak）评测报告")
    A("")
    A(f"- run-id（Langfuse release）：`{run_id}`")
    A(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    A(f"- 结束原因：`{m.get('stop_reason')}`，总时长：{m.get('elapsed_hours')} 小时")
    A(f"- **总体判定：`{m.get('verdict')}`**")
    A("")
    A("> 口径见 `doc/acceptance.md` §5。本报告由 `scripts/ralph/soak/analyze.py` 生成。")
    A("")

    # 1 判定表
    A("## 1. 验收判定")
    A("")
    A("| 指标 | 值 | 通过线 | 结果 |")
    A("|---|---|---|---|")
    for c in m.get("checks", []):
        A(_fmt_check(c))
    A("")

    # 2 可用性与稳定性
    A("## 2. 可用性与稳定性")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| 守护存活率 | {m.get('uptime_pct')}% |")
    A(f"| 心跳数 / 最大断档 | {m.get('heartbeats')} / {m.get('max_gap_s')}s |")
    A(f"| 累计停机 | {m.get('downtime_s')}s |")
    A(f"| 重启次数 | {m.get('restarts')} |")
    A(f"| 平均无故障间隔 MTBF | {m.get('mtbf_s')}s |")
    A(f"| 各次退出码 | {m.get('exit_codes')} |")
    A("")

    # 3 吞吐与任务终态
    A("## 3. 吞吐与任务终态")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| 完成迭代 | {m.get('iterations')}（{m.get('iterations_per_hour')}/h） |")
    A(f"| 平均单迭代耗时 | {m.get('avg_iter_seconds')}s |")
    A(f"| 任务总数 | {m.get('total')} |")
    A(f"| 已完成且通过 | {m.get('done_ok')}（{m.get('done_per_hour')}/h） |")
    A(f"| 阻塞 | {m.get('blocked')} |")
    A(f"| 完成率 / 首次通过率 / 阻塞率 | {m.get('completion_rate')}% / "
      f"{m.get('first_pass_rate')}% / {m.get('blocked_rate')}% |")
    A(f"| 完成/迭代 | {m.get('done_per_iteration')} |")
    A(f"| retryCount 分布 | {m.get('retry_distribution')} |")
    A("")

    # 4 工具与证据
    A("## 4. 工具与证据（P0）")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| 工具调用总数 | {m.get('total_tool_calls')} |")
    A(f"| 工具分布 | {m.get('tool_calls')} |")
    A(f"| 工具错误 / 错误率 | {m.get('tool_errors')} / {m.get('tool_error_rate')}% |")
    A(f"| 确实写文件（write/edit） | {m.get('used_write_or_edit')} |")
    A(f"| 验证命令次数（测试/lint/typecheck） | {m.get('verification_commands')}（"
      f"有验证：{m.get('has_verification')}） |")
    A("")
    A("> 证据充分度 groundedness 的确定性代理：运行中是否真的跑过测试/检查。")
    A("")

    # 5 失败分类与率
    A("## 5. 失败分类与率（长跑韧性）")
    A("")
    A("| 类别 | 次数 | 率 |")
    A("|---|---|---|")
    A(f"| Agent 双失败（引擎 exit 2） | {m.get('double_failures')} | — |")
    A(f"| 首字超时 / 持续静默超时 / 总时长超时 | {m.get('timeouts_first_token')} / "
      f"{m.get('timeouts_idle')} / {m.get('timeouts_total')} | — |")
    A(f"| 超时合计 | {m.get('timeouts_all')} | {m.get('timeout_rate')}%/迭代 |")
    A(f"| 模型无响应 / 返回错误 / 进程异常 | {m.get('agent_idle_failures')} / "
      f"{m.get('agent_api_failures')} / {m.get('process_errors')} | "
      f"{m.get('agent_failure_rate')}%/迭代 |")
    A(f"| PM 越权回滚 | {m.get('guard_rollbacks')} | "
      f"{m.get('guard_rollback_rate_per_100_iter')}/100 迭代 |")
    A(f"| 守护重启 | {m.get('restarts')} | {m.get('restart_rate_per_hour')}/h |")
    A(f"| 达最大迭代 / 队列耗尽 | {m.get('max_iter_events')} / {m.get('exhausted_events')} | — |")
    A("")

    # 6 阶段耗时与迭代分布
    A("## 6. 阶段耗时与迭代分布（P1）")
    A("")
    lf_roles = (m.get("langfuse") or {}).get("by_role") or {}
    if lf_roles:
        A("按角色总生成耗时（Langfuse，仅 GENERATION，权威）：")
        A("")
        A("| 角色 | 总生成延迟 (s) | 调用数 | 延迟 p95 (s) |")
        A("|---|---|---|---|")
        for role, rm in lf_roles.items():
            A(f"| {role} | {rm.get('gen_latency_sum_s')} | {rm.get('generations')} | "
              f"{rm.get('gen_latency_p95')} |")
        A("")
    A("本地近似（仅 child 输出行时间戳；输出被缓冲时可能失真）：")
    A("")
    A("| 阶段 | p50 (s) | p95 (s) |")
    A("|---|---|---|")
    A(f"| Developer | {m.get('phase_developer_s_p50')} | {m.get('phase_developer_s_p95')} |")
    A(f"| Validator | {m.get('phase_validator_s_p50')} | {m.get('phase_validator_s_p95')} |")
    A(f"| PM | {m.get('phase_pm_s_p50')} | {m.get('phase_pm_s_p95')} |")
    A("")
    A(f"- 单迭代耗时：p50 {m.get('iter_seconds_p50')}s / p95 {m.get('iter_seconds_p95')}s"
      f"（样本 {m.get('iter_seconds_n')}{'，样本不足以估分布' if (m.get('iter_seconds_n') or 0) < 3 else ''}）")
    A("")

    # 7 成本与效率
    A("## 7. 成本与效率")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| 累计成本 | ${m.get('cost_total')} |")
    A(f"| 成本/小时 · 成本/完成任务 · 成本/迭代 | ${m.get('cost_per_hour')} · "
      f"${m.get('cost_per_done')} · ${m.get('cost_per_iteration')} |")
    A(f"| 成本/turn（轮次） | ${m.get('cost_per_turn')}（turns={m.get('cost_turns')}） |")
    A(f"| 成本随时间斜率 | ${m.get('cost_slope_usd_per_h')}/h（成本/迭代趋势 "
      f"${m.get('cost_per_iteration_trend')}） |")
    A("")

    # 8 资源与上下文增长
    A("## 8. 资源与上下文增长（长跑风险）")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| 峰值 RSS | {m.get('peak_rss_mb')} MB（增速 {m.get('rss_slope_mb_per_h')} MB/h） |")
    A(f"| progress.txt | {m.get('progress_bytes')} B（增长 {m.get('progress_slope_bytes_per_h')} B/h） |")
    A(f"| state.json | {m.get('state_json_bytes')} B（增长 {m.get('state_slope_bytes_per_h')} B/h） |")
    A(f"| AGENTS.md | {m.get('agents_md_bytes')} B |")
    A(f"| patterns 文档 | {m.get('patterns_files')} 个文件 / {m.get('patterns_lines')} 行 |")
    A(f"| 趋势采样点 | {m.get('points')} |")
    A("")
    A("> `state.json` 每轮多次全量重写且内嵌 progress，体积增长会带来写放大；"
      "`AGENTS.md`/patterns 每次 prompt 注入，增长会抬高单迭代成本。看增速是否随时间加速。")
    A("")

    # 9 治理与协作
    A("## 9. 治理与协作（PM）（P1）")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| PM 调整总数 | {m.get('pm_adjustments_total')} |")
    A(f"| 动作分布 | {m.get('pm_adjustments')} |")
    A(f"| reset / split / 新增 | {m.get('pm_resets')} / {m.get('pm_splits')} / {m.get('pm_new_tasks')} |")
    A(f"| 越权回滚次数 | {m.get('guard_rollbacks')} |")
    A(f"| 防腐硬失败（审计损坏/无备份回滚） | {m.get('guard_corruption')} |")
    A("")

    # 10 Langfuse
    A("## 10. 在线可观测（Langfuse）")
    A("")
    lf = m.get("langfuse", {})
    if lf.get("available"):
        A(f"- 数据源：`{lf.get('source')}`")
        if lf.get("release_fallback"):
            A("- ⚠️ 未匹配到 release 标签，已回退为「环境 + 时间窗」聚合，可能与同窗口其它运行混合")
        A("")
        A("| 指标 | 值 |")
        A("|---|---|")
        A(f"| 窗口 | {lf.get('window')} |")
        A(f"| observation 总数 | {lf.get('observations')} |")
        A(f"| 类型分布 | {lf.get('by_type')} |")
        A(f"| 整体错误数 / 错误率 | {lf.get('error_count')} / {lf.get('error_rate')}% |")
        A(f"| 模型调用(turns) / 工具调用 / span | {lf.get('turns')} / "
          f"{lf.get('tools')} / {lf.get('spans')} |")
        A(f"| 生成延迟 p50/p95/p99 (s) | {lf.get('gen_latency_p50')} / "
          f"{lf.get('gen_latency_p95')} / {lf.get('gen_latency_p99')} |")
        A(f"| TTFT p50/p95 (s) | {lf.get('ttft_p50')} / {lf.get('ttft_p95')} |")
        A(f"| 工具延迟 p95 (s) | {lf.get('tool_latency_p95')} |")
        A(f"| **工具错误 / 错误率** | {lf.get('tool_errors')} / {lf.get('tool_error_rate')}% |")
        A(f"| 工具分布 | {lf.get('tool_names')} |")
        A(f"| token 合计（输入/输出） | {lf.get('total_tokens')}"
          f"（{lf.get('input_tokens')} / {lf.get('output_tokens')}） |")
        A(f"| 成本合计（输入/输出） | ${lf.get('total_price')}"
          f"（${lf.get('input_cost')} / ${lf.get('output_cost')}） |")
        A("")
        by_role = lf.get("by_role") or {}
        if by_role:
            A("### 按角色（Developer / Validator / PM）")
            A("")
            A("| 角色 | 生成调用 | 总生成延迟 (s) | 延迟 p95 (s) | 成本 ($) | token (in/out) |")
            A("|---|---|---|---|---|---|")
            for role, rm in by_role.items():
                A(f"| {role} | {rm.get('generations')} | {rm.get('gen_latency_sum_s')} | "
                  f"{rm.get('gen_latency_p95')} | {rm.get('total_cost')} | "
                  f"{rm.get('total_tokens')}（{rm.get('input_tokens')}/{rm.get('output_tokens')}） |")
            A("")
    else:
        A(f"- 未采集：{lf.get('reason', 'unknown')}")
        A("")
        A("> 确认 pi 已加载 Langfuse 插件且存在凭据（`~/.pi/agent/langfuse.json`）；"
          "守护器默认开启上报并注入 `LANGFUSE_RELEASE=<run-id>`。")
        A("")

    # 11 结果质量（LLM-judge，可选）/ 诚实性
    judge = m.get("judge")
    if judge:
        A("## 11. 结果质量（LLM-as-judge，P2）")
        A("")
        A(f"- 事实正确率 factual_correctness：{judge.get('factual_correctness')}")
        A(f"- 证据充分度 groundedness：{judge.get('groundedness')}")
        A(f"- 理由：{judge.get('reason', '')}")
        A("")
    elif m.get("overclaim_rate") is not None:
        A("## 11. 诚实性")
        A("")
        A(f"- 伪完成率 overclaim：{m['overclaim_rate']}%")
        A("")

    A("---")
    A("")
    A("## 待修项提示")
    A("")
    hints = _hints(m)
    if hints:
        for h in hints:
            A(f"- {h}")
    else:
        A("- 未发现明显异常。")
    A("")
    return "\n".join(L)


def _hints(m: dict) -> list[str]:
    h: list[str] = []
    for c in m.get("checks", []):
        if c["ok"] is False:
            h.append(f"未达标：{c['label']} = {c['value']}（需 {c['op']} {c['threshold']}）")
    if m.get("uptime_pct", 100) < 100:
        h.append("存在心跳断档，检查是否被系统休眠/网络阻塞的中断。")
    if m.get("elapsed_hours", 0) >= 1 and m.get("progress_slope_bytes_per_h", 0) > 200_000:
        h.append("progress.txt 增长偏快，考虑轮转/摘要以控制上下文膨胀。")
    if m.get("elapsed_hours", 0) >= 1 and m.get("rss_slope_mb_per_h", 0) > 20:
        h.append("RSS 持续增长，检查是否存在内存泄漏（daemon 线程/连接未释放）。")
    if m.get("guard_corruption", 0) > 0:
        h.append("检测到防腐硬失败（审计损坏或无备份回滚），任务树可能已损坏，需人工核查。")
    if m.get("guard_rollbacks", 0) > 0:
        h.append("出现 PM 越权回滚，复核 PM 是否频繁尝试非法改动。")
    if not m.get("has_verification"):
        h.append("未观察到验证命令（测试/lint），groundedness 存疑。")
    return h


# ══════════════════════════════════════════════════════════════
#  对外入口
# ══════════════════════════════════════════════════════════════

def generate_report(runtime: Path, ralph_dir: Path = RALPH_DIR,
                    project_root: Path = PROJECT_ROOT,
                    out: Path | None = None, json_out: Path | None = None,
                    langfuse_enabled: bool = True, environment: str = "ralph",
                    release: str | None = None, days: int = 7,
                    judge: bool = False, judge_model: str | None = None,
                    judge_sandbox: Path | None = None) -> dict:
    """计算指标、写报告与 JSON，返回指标 dict。"""
    runtime = Path(runtime)
    summary = read_json(runtime / "summary.json", {})
    run_id = release or summary.get("run_id") or "unknown"
    frm = _to_utc_z(summary.get("started_at", "")) if summary.get("started_at") else None
    to = _to_utc_z(summary.get("ended_at", "")) if summary.get("ended_at") else None
    # 前后各留 5 分钟余量，缓解 Langfuse 数据延迟/时钟偏差
    if frm:
        frm = _shift_minutes(frm, -5)
    if to:
        to = _shift_minutes(to, 5)
    lf = fetch_langfuse(release or summary.get("run_id"), environment,
                        frm=frm, to=to, days=days, enabled=langfuse_enabled)
    m = compute_all(runtime, Path(ralph_dir), Path(project_root), lf, DEFAULT_THRESHOLDS)
    m["run_id"] = run_id

    if judge:
        m["judge"] = run_judge(
            Path(ralph_dir) / "tasks",
            judge_sandbox or Path(project_root),
            model=judge_model)

    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(build_report(m, run_id), encoding="utf-8")
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(
            json.dumps(m, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return m


def run_judge(tasks_dir: Path, sandbox_dir: Path, model: str | None = None) -> dict:
    """调用 LLM-as-judge（事实正确率 / 证据充分度）；失败返回带 error 的 dict。"""
    try:
        sys.path.insert(0, str(RALPH_DIR))
        import judge as judge_mod  # noqa: E402
        return judge_mod.judge(tasks_dir, sandbox_dir, model=model)
    except Exception as e:  # noqa: BLE001
        return {"factual_correctness": None, "groundedness": None,
                "reason": f"judge 调用失败：{e}"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ralph 长跑评测报告器")
    p.add_argument("--runtime", default=str(DEFAULT_RUNTIME), help="守护器输出目录")
    p.add_argument("--ralph-dir", default=str(RALPH_DIR), help="scripts/ralph 目录")
    p.add_argument("--project-root", default=str(PROJECT_ROOT), help="仓库根")
    p.add_argument("--release", default=None, help="按 Langfuse release/run-id 过滤")
    p.add_argument("--environment", default="ralph", help="Langfuse environment 过滤")
    p.add_argument("--days", type=int, default=7, help="无 summary 时 Langfuse 回看天数")
    p.add_argument("--no-langfuse", action="store_true", help="跳过 Langfuse")
    p.add_argument("--judge", action="store_true",
                   help="启用 LLM-as-judge（事实正确率/证据充分度，会额外调用模型）")
    p.add_argument("--judge-model", default=None, help="裁判模型")
    p.add_argument("--judge-sandbox", default=None, help="裁判读取的产出目录，默认仓库根")
    p.add_argument("--out", default=None, help="Markdown 报告输出路径")
    p.add_argument("--json", dest="json_out", default=None, help="指标 JSON 输出路径")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    m = generate_report(
        runtime=Path(args.runtime), ralph_dir=Path(args.ralph_dir),
        project_root=Path(args.project_root),
        out=Path(args.out) if args.out else None,
        json_out=Path(args.json_out) if args.json_out else None,
        langfuse_enabled=not args.no_langfuse,
        environment=args.environment, release=args.release, days=args.days,
        judge=args.judge, judge_model=args.judge_model,
        judge_sandbox=Path(args.judge_sandbox) if args.judge_sandbox else None,
    )
    print(build_report(m, m.get("run_id", "unknown")))
    if args.out:
        print(f"\n报告已写入：{args.out}", file=sys.stderr)
    if args.json_out:
        print(f"指标 JSON 已写入：{args.json_out}", file=sys.stderr)
    return 0 if m.get("verdict") != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
