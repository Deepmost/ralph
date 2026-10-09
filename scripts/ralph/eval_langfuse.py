#!/usr/bin/env python3
"""
Ralph × Langfuse 评测采集脚本

从 Langfuse 拉取 pi observability 插件上报的 observation，结合本地 Ralph
产物（tasks/ 与 tasks/done/ 的 frontmatter），计算 Agent 评测指标并输出报告。

指标口径见 docs/agent-eval.md。

数据源：
  1) Langfuse v2 observations API（实时可观测数据）
  2) scripts/ralph/tasks | tasks/done 的 frontmatter（任务终态）

用法：
  python3 scripts/ralph/eval_langfuse.py                    # 最近 7 天
  python3 scripts/ralph/eval_langfuse.py --days 30
  python3 scripts/ralph/eval_langfuse.py --environment ralph
  python3 scripts/ralph/eval_langfuse.py --out docs/eval-report.md --json docs/eval-data.json

凭据读取顺序：环境变量 LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL → ~/.pi/agent/langfuse.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent
TASKS_DIR = SCRIPT_DIR / "tasks"
DONE_DIR = TASKS_DIR / "done"
AGENT_DIR = Path(os.path.expanduser("~/.pi/agent"))
LANGFUSE_CONFIG = AGENT_DIR / "langfuse.json"


# ══════════════════════════════════════════════════════════════
#  凭据 & HTTP
# ══════════════════════════════════════════════════════════════

def load_config() -> dict | None:
    """读取 Langfuse 凭据（环境变量优先）。"""
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    base_url = os.environ.get("LANGFUSE_BASE_URL", "").strip()
    if not (public_key and secret_key):
        try:
            data = json.loads(LANGFUSE_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return None
        public_key = public_key or str(data.get("publicKey", "")).strip()
        secret_key = secret_key or str(data.get("secretKey", "")).strip()
        base_url = base_url or str(data.get("baseUrl", "")).strip()
    if not (public_key and secret_key):
        return None
    return {
        "publicKey": public_key,
        "secretKey": secret_key,
        "baseUrl": (base_url or "https://cloud.langfuse.com").rstrip("/"),
    }


def _http_get_json(url: str, auth: str, timeout: int = 30) -> dict:
    """
    取 JSON。优先 urllib；遇到 SSL/环境问题回退 curl（本机 Python 可能缺 CA 证书）。
    """
    try:
        import urllib.request
        req = urllib.request.Request(url)
        req.add_header("Authorization", auth)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    # 回退 curl
    try:
        out = subprocess.run(
            ["curl", "-sS", "--max-time", str(timeout), "-H", f"Authorization: {auth}", url],
            capture_output=True, text=True,
        ).stdout
        return json.loads(out)
    except Exception as e:
        raise RuntimeError(f"请求失败: {e}")


def fetch_observations(cfg: dict, frm: str, to: str,
                       environment: str | None, release: str | None,
                       user_id: str | None, limit: int, max_pages: int) -> list[dict]:
    """分页拉取 v2 observations。"""
    auth = "Basic " + base64.b64encode(
        f"{cfg['publicKey']}:{cfg['secretKey']}".encode()
    ).decode()
    base = cfg["baseUrl"] + "/api/public/v2/observations"
    rows: list[dict] = []
    cursor: str | None = None
    for _ in range(max_pages):
        params = {
            "fromStartTime": frm,
            "toStartTime": to,
            "limit": limit,
        }
        if environment:
            params["environment"] = environment
        if release:
            params["release"] = release
        if user_id:
            params["userId"] = user_id
        if cursor:
            params["cursor"] = cursor
        url = base + "?" + urllib.parse.urlencode(params)
        data = _http_get_json(url, auth)
        if "data" not in data:
            # 兼容错误响应
            print(f"  Langfuse 返回异常: {str(data)[:200]}", file=sys.stderr)
            break
        page = data.get("data", [])
        rows.extend(page)
        cursor = (data.get("meta") or {}).get("cursor")
        if not cursor or not page:
            break
    return rows


# ══════════════════════════════════════════════════════════════
#  本地任务扫描（任务终态）
# ══════════════════════════════════════════════════════════════

def parse_frontmatter(filepath: Path) -> dict:
    """极简 YAML frontmatter 解析（与 ralph.py 保持一致，避免 pyyaml 依赖）。"""
    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    meta: dict = {}
    for line in content[4:end].strip().splitlines():
        m = re.match(r"^(\w+)\s*:\s*(.+)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
        if val.isdigit():
            val = int(val)
        elif val.lower() in ("true", "false"):
            val = val.lower() == "true"
        meta[key] = val
    return meta


def scan_tasks() -> dict:
    """扫描 tasks/ 与 done/，汇总任务终态指标。"""
    pending, done = [], []
    for f in sorted(TASKS_DIR.glob("*.md")):
        meta = parse_frontmatter(f)
        meta["_id"] = str(meta.get("id", f.stem))
        pending.append(meta)
    for f in sorted(DONE_DIR.glob("*.md")):
        meta = parse_frontmatter(f)
        meta["_id"] = str(meta.get("id", f.stem))
        done.append(meta)

    blocked = [t for t in (pending + done) if t.get("blocked")]
    done_ok = [t for t in done if not t.get("blocked")]
    first_pass = [t for t in done_ok if int(t.get("retryCount", 0) or 0) == 0]
    total = len(pending) + len(done)

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
        "retry_distribution": _retry_distribution(done + pending),
    }


def _retry_distribution(tasks: list[dict]) -> dict:
    dist: dict[str, int] = {}
    for t in tasks:
        rc = int(t.get("retryCount", 0) or 0)
        key = str(rc) if rc < 5 else "5+"
        dist[key] = dist.get(key, 0) + 1
    return dict(sorted(dist.items(), key=lambda kv: (kv[0] == "5+", kv[0])))


# ══════════════════════════════════════════════════════════════
#  指标计算
# ══════════════════════════════════════════════════════════════

def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


def compute_metrics(rows: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    errors = 0
    gen_latency: list[float] = []
    gen_ttft: list[float] = []
    tool_errors = 0
    tool_names: dict[str, int] = {}
    total_price = 0.0
    priced = 0
    traces: set[str] = set()
    sessions: set[str] = set()
    users: dict[str, int] = {}
    envs: dict[str, int] = {}
    releases: dict[str, int] = {}

    for o in rows:
        t = str(o.get("type", "UNKNOWN"))
        by_type[t] = by_type.get(t, 0) + 1
        if str(o.get("level", "")).upper() == "ERROR":
            errors += 1
        if o.get("traceId"):
            traces.add(o["traceId"])
        if o.get("sessionId"):
            sessions.add(o["sessionId"])
        users[str(o.get("userId", ""))] = users.get(str(o.get("userId", "")), 0) + 1
        envs[str(o.get("environment", ""))] = envs.get(str(o.get("environment", "")), 0) + 1
        rel = str(o.get("version") or "")
        if rel:
            releases[rel] = releases.get(rel, 0) + 1

        lat = o.get("latency")
        if t == "GENERATION":
            if isinstance(lat, (int, float)):
                gen_latency.append(float(lat))
            ttft = o.get("timeToFirstToken")
            if isinstance(ttft, (int, float)):
                gen_ttft.append(float(ttft))
            price = o.get("totalPrice")
            if isinstance(price, (int, float)):
                total_price += float(price)
                priced += 1
        elif t == "TOOL":
            name = str(o.get("name", "")).replace("Tool:", "").strip()
            tool_names[name] = tool_names.get(name, 0) + 1
            if str(o.get("level", "")).upper() == "ERROR":
                tool_errors += 1

    n = len(rows)
    n_gen = by_type.get("GENERATION", 0)
    n_tool = by_type.get("TOOL", 0)

    def pct(num, den):
        return round(num / den * 100, 1) if den else 0.0

    return {
        "observations": n,
        "traces": len(traces),
        "sessions": len(sessions),
        "by_type": by_type,
        "error_count": errors,
        "error_rate": pct(errors, n),
        "generations": n_gen,
        "gen_latency_p50": _percentile(gen_latency, 0.50),
        "gen_latency_p95": _percentile(gen_latency, 0.95),
        "gen_latency_p99": _percentile(gen_latency, 0.99),
        "gen_latency_max": round(max(gen_latency), 3) if gen_latency else 0.0,
        "ttft_p50": _percentile(gen_ttft, 0.50),
        "tools": n_tool,
        "tool_errors": tool_errors,
        "tool_error_rate": pct(tool_errors, n_tool),
        "tool_names": dict(sorted(tool_names.items(), key=lambda kv: -kv[1])),
        # 注：列表接口通常不返回 costDetails；totalPrice 有值时才有意义
        "total_price": round(total_price, 6),
        "priced_generations": priced,
        "users": dict(sorted(users.items(), key=lambda kv: -kv[1])),
        "environments": dict(sorted(envs.items(), key=lambda kv: -kv[1])),
        "releases": dict(sorted(releases.items(), key=lambda kv: -kv[1])),
    }


# ══════════════════════════════════════════════════════════════
#  报告
# ══════════════════════════════════════════════════════════════

def build_report(frm: str, to: str, filters: dict, tasks: dict, m: dict) -> str:
    L: list[str] = []
    A = L.append
    A("# Ralph Agent 评测报告（Langfuse 采集）")
    A("")
    A(f"- 时间窗：`{frm}` ~ `{to}`")
    active = {k: v for k, v in filters.items() if v}
    A(f"- 过滤条件：`{active or '无（全部）'}`")
    A(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    A("")
    A("> 口径定义见 `docs/agent-eval.md`。本报告为**离线采集快照**，非在线评分。")
    A("")

    # 任务终态
    A("## 1. 任务终态指标（本地 tasks/）")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| 任务总数 | {tasks['total']} |")
    A(f"| 待执行 (pending) | {tasks['pending']} |")
    A(f"| 已完成且通过 (done) | {tasks['done_ok']} |")
    A(f"| 阻塞 (blocked) | {tasks['blocked']} |")
    A(f"| 任务完成率 | {tasks['completion_rate']}% |")
    A(f"| 首次验证通过率 FPR | {tasks['first_pass_rate']}% |")
    A(f"| 阻塞率 | {tasks['blocked_rate']}% |")
    A(f"| retryCount 分布 | {tasks['retry_distribution'] or '无数据'} |")
    A("")

    # 可观测
    A("## 2. 可观测指标（Langfuse observations）")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| observation 总数 | {m['observations']} |")
    A(f"| trace 数 | {m['traces']} |")
    A(f"| session 数 | {m['sessions']} |")
    A(f"| 类型分布 | {m['by_type']} |")
    A(f"| 错误数 / 错误率 | {m['error_count']} / {m['error_rate']}% |")
    A("")

    A("### 模型调用（GENERATION）")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| 调用次数 | {m['generations']} |")
    A(f"| 延迟 p50 / p95 / p99 (s) | {m['gen_latency_p50']} / {m['gen_latency_p95']} / {m['gen_latency_p99']} |")
    A(f"| 延迟 max (s) | {m['gen_latency_max']} |")
    A(f"| TTFT p50 (原值) | {m['ttft_p50']} |")
    A(f"| 成本合计（若有）($) | {m['total_price']}（{m['priced_generations']} 条有价） |")
    A("")

    A("### 工具调用（TOOL）")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| 调用次数 | {m['tools']} |")
    A(f"| 失败数 / 失败率 | {m['tool_errors']} / {m['tool_error_rate']}% |")
    A(f"| 工具分布 | {m['tool_names'] or '无数据'} |")
    A("")

    A("### 分组标签")
    A("")
    A(f"- environment：{m['environments']}")
    A(f"- userId（角色）：{m['users']}")
    A(f"- release：{m['releases']}")
    A("")

    A("## 3. 待补齐（当前接口/规范未覆盖）")
    A("")
    A("- **token / cost 明细**：v2 observations 列表接口通常不含 usageDetails；需在 Langfuse UI/Metrics 或用 Scores 补齐。")
    A("- **事实正确率 / 证据充分度**：需 LLM-as-judge（可基于 root span output）。")
    A("- **工具正确率**：需定义每个任务的期望工具集后做断言。")
    A("- **pass^k / 一致性**：需同一任务跑 N 次后聚合。")
    A("- **让 trace 可按运行/角色分组**：在 `ralph.py` 注入 `LANGFUSE_TRACING_ENVIRONMENT / RELEASE / USER_ID`。")
    A("")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════

def _iso(days_ago: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ralph × Langfuse 评测采集")
    p.add_argument("--days", type=int, default=7, help="回看天数（默认 7）")
    p.add_argument("--from", dest="frm", default=None, help="起始时间 ISO8601，覆盖 --days")
    p.add_argument("--to", dest="to", default=None, help="结束时间 ISO8601，默认现在")
    p.add_argument("--environment", default=None, help="按 environment 过滤（如 ralph）")
    p.add_argument("--release", default=None, help="按 release/run-id 过滤")
    p.add_argument("--user", default=None, help="按 userId 过滤（如 developer）")
    p.add_argument("--limit", type=int, default=100, help="每页条数（默认 100）")
    p.add_argument("--max-pages", type=int, default=50, help="最大分页数（默认 50）")
    p.add_argument("--out", default=None, help="Markdown 报告输出路径")
    p.add_argument("--json", dest="json_out", default=None, help="原始指标 JSON 输出路径")
    p.add_argument("--no-langfuse", action="store_true", help="跳过 Langfuse，仅本地任务指标")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    frm = args.frm or _iso(args.days)
    to = args.to or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    tasks = scan_tasks()

    metrics: dict = {}
    rows: list[dict] = []
    if not args.no_langfuse:
        cfg = load_config()
        if cfg is None:
            print("跳过 Langfuse：未找到凭据（环境变量或 ~/.pi/agent/langfuse.json）", file=sys.stderr)
        else:
            print(f"从 Langfuse 拉取 observation：{frm} ~ {to}", file=sys.stderr)
            try:
                rows = fetch_observations(
                    cfg, frm, to,
                    args.environment, args.release, args.user,
                    args.limit, args.max_pages,
                )
            except Exception as e:
                print(f"拉取失败：{e}", file=sys.stderr)
                rows = []
            metrics = compute_metrics(rows)

    report = build_report(
        frm, to,
        {"environment": args.environment, "release": args.release, "userId": args.user},
        tasks,
        metrics or compute_metrics([]),
    )
    print(report)

    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"\n报告已写入：{args.out}", file=sys.stderr)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"window": {"from": frm, "to": to}, "tasks": tasks, "metrics": metrics},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"指标 JSON 已写入：{args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
