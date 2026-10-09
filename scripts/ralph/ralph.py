#!/usr/bin/env python3
"""
Ralph - 自主 AI Agent 循环执行器（pi agent 后端，含 Validator）
基于任务文件（tasks/*.md）模式运行。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── 目录配置 ──

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent
TASKS_DIR = SCRIPT_DIR / "tasks"
DONE_DIR = TASKS_DIR / "done"
ARCHIVE_DIR = SCRIPT_DIR / "archive"
TASKS_BAK_DIR = SCRIPT_DIR / "tasks.bak"
PROGRESS_FILE = SCRIPT_DIR / "progress.txt"
STATE_FILE = SCRIPT_DIR / "state.json"
ADJUSTMENTS_FILE = SCRIPT_DIR / "adjustments.json"
DEVELOPER_INSTRUCTION = SCRIPT_DIR / "DEVELOPER.md"
VALIDATOR_INSTRUCTION = SCRIPT_DIR / "VALIDATOR.md"
PM_INSTRUCTION = SCRIPT_DIR / "PM.md"
# 项目上下文文件：优先 AGENTS.md（pi 原生约定），兼容旧的 CLAUDE.md
PROJECT_CONTEXT_CANDIDATES = (PROJECT_ROOT / "AGENTS.md", PROJECT_ROOT / "CLAUDE.md")
PATTERNS_DIR = PROJECT_ROOT / "docs"

# ── 超时配置 ──

FIRST_TOKEN_TIMEOUT = 120   # 首字响应超时（秒）
IDLE_TIMEOUT = 120           # 持续无输出超时（秒）
TIMEOUT_SECONDS = 30 * 60   # 总时长超时（30分钟）

# ── PM Agent 越权防护与收敛阈值 ──

PM_MAX_NEW_TASKS = 3         # PM 单轮允许净新增的任务上限，超过则回滚
PM_MAX_RESETS = 2            # PM 单轮允许 reset（done→tasks）的任务上限，超过则回滚
PM_PATTERNS_WARN_LINES = 200 # docs/patterns-*.md 总行数告警阈值

# ── 全局变量（由 main 设置）──

MAX_ITERATIONS = 50
AGENT = "pi"
NO_VALIDATOR = False   # --no-validator：跳过验证（对照实验用）
NO_PM = False          # --no-pm：跳过 PM 治理（对照实验用）
NO_GUARD = False       # --no-guard：关闭 PM 越权回滚（对照实验用）

# ── Langfuse 观测标签 ──
# 本次运行的唯一标识，作为 trace 的 release 标签，供 eval_langfuse.py 按运行聚合
LANGFUSE_RUN_ID = os.environ.get("RALPH_RUN_ID") or time.strftime("%Y%m%d-%H%M%S")


def _inject_langfuse_env(env: dict, role: str = "") -> None:
    """
    给 pi 子进程注入 Langfuse 观测标签，便于按「运行 / 角色」聚合 trace。
    依赖 pi 的 @langfuse/pi-observability-plugin；环境变量优先级高于其配置文件。
    设置 RALPH_NO_LANGFUSE=1 可整体关闭上报。
    """
    if os.environ.get("RALPH_NO_LANGFUSE") == "1":
        env["LANGFUSE_TRACING_ENABLED"] = "false"
        return
    env["LANGFUSE_TRACING_ENVIRONMENT"] = os.environ.get("RALPH_LANGFUSE_ENV", "ralph")
    env["LANGFUSE_RELEASE"] = LANGFUSE_RUN_ID
    if role:
        env["LANGFUSE_USER_ID"] = role


# ══════════════════════════════════════════════════════════════
#  可执行文件解析
# ══════════════════════════════════════════════════════════════

def _resolve_exe(name: str) -> str:
    """解析可执行文件的完整路径（macOS / Linux）"""
    return shutil.which(name) or name


# ══════════════════════════════════════════════════════════════
#  命令构建 & pi JSON 事件流解析
# ══════════════════════════════════════════════════════════════

def build_cmd() -> list[str]:
    """
    构建 pi agent 命令（不含 prompt，prompt 通过 stdin 传递）。
      --mode json  : 输出 JSONL 事件流，便于实时解析
      --print      : 非交互模式，处理完 prompt 后退出
      --no-session : 每次迭代独立会话，不持久化
      --approve    : 信任项目本地资源，使 .agents/skills/ 等项目技能在非交互模式下也能加载
    pi 在非交互模式下不会为工具调用请求审批，可自主执行。
    """
    exe = _resolve_exe("pi")
    return [exe, "--mode", "json", "--print", "--no-session", "--approve"]


def _format_tool(name: str, args: dict) -> str:
    """把一次工具调用格式化为一行可读文本"""
    if name in ("read", "write", "edit"):
        fname = Path(str(args.get("path") or args.get("file_path") or "")).name
        return f"[Tool] {name}: {fname}"
    if name == "bash":
        return f"[Tool] bash: {str(args.get('command', ''))[:80]}"
    if name == "grep":
        return f"[Tool] grep: {args.get('pattern', '')}"
    if name in ("find", "ls"):
        return f"[Tool] {name}: {args.get('path') or args.get('pattern') or ''}"
    return f"[Tool] {name}"


# pi JSON 事件流中已知的事件类型（识别到但无需展示时静默处理，避免刷屏）
_PI_KNOWN_EVENTS = {
    "agent_start", "turn_start", "message_start", "message_end",
    "turn_end", "agent_end", "auto_retry_end", "compaction_end",
    "tool_execution_update", "tool_execution_end", "queue_update",
    "entry_appended", "session_info_changed", "thinking_level_changed",
    "summarization_retry_scheduled", "summarization_retry_attempt_start",
    "summarization_retry_finished", "error",
}


def parse_stream_line(line: str) -> str | None:
    """
    解析 pi JSON 事件流的一行。
    返回要显示的文本；返回 "" 表示已识别但无需展示；返回 None 表示无法识别的原始行。
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    msg_type = obj.get("type")

    if msg_type == "session":
        return f"[System] pi 会话: {obj.get('id', '?')}"

    if msg_type == "message_update":
        ev = obj.get("assistantMessageEvent") or {}
        et = ev.get("type")
        if et == "text_end":
            text = (ev.get("content") or "").strip()
            if text:
                return f"[Assistant] {text[:300]}"
        elif et == "thinking_end":
            text = (ev.get("content") or "").strip()
            if text:
                return f"[Thinking] {text[:160]}"
        elif et == "error":
            return f"[Error] {ev.get('reason') or ''} {ev.get('error') or ''}".strip()
        return ""

    if msg_type == "tool_execution_start":
        return _format_tool(obj.get("toolName", "?"), obj.get("args") or {})

    if msg_type == "tool_execution_end" and obj.get("isError"):
        return f"[Tool Error] {obj.get('toolName', '?')}"

    if msg_type == "turn_end":
        usage = (obj.get("message") or {}).get("usage") or {}
        cost = (usage.get("cost") or {}).get("total")
        if cost:
            return f"[Turn] 费用: ${cost}"
        return ""

    if msg_type == "agent_end":
        return "[Agent] 将自动重试..." if obj.get("willRetry") else ""

    if msg_type == "auto_retry_start":
        return (f"[Retry] 第 {obj.get('attempt')}/{obj.get('maxAttempts')} 次"
                f"，原因: {obj.get('errorMessage', '')}")

    if msg_type == "compaction_start":
        return f"[Compaction] {obj.get('reason', '')}"

    if msg_type == "agent_settled":
        return "\n".join([
            "",
            "=========== 迭代结果 ===========",
            "状态: settled",
            "================================",
        ])

    if msg_type in _PI_KNOWN_EVENTS:
        return ""
    return None


# ══════════════════════════════════════════════════════════════
#  进程管理 & pi agent 执行
# ══════════════════════════════════════════════════════════════

def _kill_process(process: subprocess.Popen) -> None:
    """安全终止子进程"""
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except Exception:
        pass


def run_cc_stream(cmd: list[str], prompt: str, label: str, total_timeout: int,
                  role: str = "") -> str:
    """
    运行 pi agent 子进程并实时解析 JSON 事件流。
    prompt 通过 stdin 传递，避免命令行参数编码/长度问题。
    用独立线程非阻塞读取 stdout，主循环检测超时。
    role: Langfuse 角色标签（developer / validator / pm），用于 trace 分组。
    返回: "ok" | "idle" | "timeout" | "error" | "api_error"
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    _inject_langfuse_env(env, role)
    try:
        process = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env,
        )
        process.stdin.write(prompt.encode("utf-8"))
        process.stdin.close()
    except Exception as e:
        print(f"\n  启动进程失败: {e}")
        return "error"

    start_time = time.time()
    last_output_time = time.time()
    got_first_token = False
    got_result = False
    result_subtype = ""
    pi_error = False

    line_buffer: list[str] = []
    read_finished = threading.Event()

    def _reader():
        try:
            for ln in process.stdout:
                if isinstance(ln, bytes):
                    ln = ln.decode("utf-8", errors="replace")
                line_buffer.append(ln)
        except Exception:
            pass
        read_finished.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    try:
        while True:
            while line_buffer:
                line = line_buffer.pop(0)
                stripped = line.strip()
                last_output_time = time.time()
                got_first_token = True
                display = parse_stream_line(stripped)
                if display:
                    print(display)
                elif display is None and stripped:
                    print(f"[raw] {stripped[:300]}")
                try:
                    obj = json.loads(stripped)
                    etype = obj.get("type")
                    if etype == "message_end":
                        msg = obj.get("message") or {}
                        if msg.get("role") == "assistant" and \
                                msg.get("stopReason") in ("error", "aborted"):
                            pi_error = True
                    elif etype == "auto_retry_end":
                        pi_error = obj.get("success") is not True
                    elif etype == "error":
                        pi_error = True
                    elif etype == "agent_settled":
                        # pi 明确的"不会再有自动工作"标志，作为本次运行终点
                        got_result = True
                        result_subtype = "error" if pi_error else "success"
                except (json.JSONDecodeError, ValueError):
                    pass

            if got_result:
                _kill_process(process)
                if result_subtype == "success":
                    return "ok"
                print(f"\n  {label} 返回错误 (subtype: {result_subtype})")
                return "api_error"

            if read_finished.is_set() and not line_buffer:
                process.wait()
                if not got_first_token or process.returncode != 0:
                    rc = process.returncode
                    print(f"\n  {label} 进程异常退出 (exit code: {rc}, got_output: {got_first_token})")
                    return "error"
                if pi_error:
                    print(f"\n  {label} 事件流报告错误")
                    return "api_error"
                return "ok"

            now = time.time()

            if now - start_time > total_timeout:
                print(f"\n  {label} 总时长超时! 已运行 {int(now - start_time)} 秒")
                _kill_process(process)
                return "timeout"

            idle_sec = now - last_output_time
            if not got_first_token and idle_sec > FIRST_TOKEN_TIMEOUT:
                print(f"\n  {label} 首字响应超时! 等待 {int(idle_sec)} 秒无输出")
                _kill_process(process)
                return "idle"

            if got_first_token and idle_sec > IDLE_TIMEOUT:
                print(f"\n  {label} 持续无输出超时! 已 {int(idle_sec)} 秒无响应")
                _kill_process(process)
                return "idle"

            time.sleep(0.1)

    except Exception as e:
        print(f"\n  {label} 错误: {e}")
        _kill_process(process)
        return "error"


# ══════════════════════════════════════════════════════════════
#  任务文件解析 & 状态管理
# ══════════════════════════════════════════════════════════════

def parse_frontmatter(filepath: Path) -> dict:
    """用正则解析简单的 YAML frontmatter，避免 pyyaml 依赖"""
    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    meta = {}
    for line in content[4:end].strip().splitlines():
        m = re.match(r'^(\w+)\s*:\s*(.+)$', line)
        if m:
            key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
            # id 必须保持字符串：避免 "001" 被转成 1，导致快照键("1")与
            # adjustments.json 中的审计 id("001") 不匹配，使合法 split/reset 被误回滚
            if key == "id":
                pass
            elif val.isdigit():
                val = int(val)
            elif val.lower() in ("true", "false"):
                val = val.lower() == "true"
            meta[key] = val
    return meta


def scan_tasks() -> list[dict]:
    """扫描 tasks/ 和 done/ 目录，返回所有任务的状态列表"""
    tasks = []
    # pending 任务
    for f in sorted(TASKS_DIR.glob("*.md")):
        meta = parse_frontmatter(f)
        blocked = meta.get("blocked", False)
        tasks.append({
            "id": str(meta.get("id", f.stem)),
            "title": meta.get("title", f.stem),
            "file": f.name,
            "status": "blocked" if blocked else "pending",
            "retryCount": meta.get("retryCount", 0),
            "priority": meta.get("priority", 999),
            "notes": meta.get("validation_notes", ""),
        })
    # done 任务
    for f in sorted(DONE_DIR.glob("*.md")):
        meta = parse_frontmatter(f)
        blocked = meta.get("blocked", False)
        tasks.append({
            "id": str(meta.get("id", f.stem)),
            "title": meta.get("title", f.stem),
            "file": f.name,
            "status": "blocked" if blocked else "done",
            "retryCount": meta.get("retryCount", 0),
            "priority": meta.get("priority", 999),
            "notes": "",
        })
    tasks.sort(key=lambda t: t["priority"])
    return tasks


def get_next_task() -> Path | None:
    """返回 tasks/ 中按文件名排序的第一个非 blocked 的 .md 文件"""
    for f in sorted(TASKS_DIR.glob("*.md")):
        meta = parse_frontmatter(f)
        if not meta.get("blocked", False):
            return f
    return None


def all_tasks_resolved() -> bool:
    """检查 tasks/ 目录是否没有可执行的任务"""
    for f in TASKS_DIR.glob("*.md"):
        meta = parse_frontmatter(f)
        if not meta.get("blocked", False):
            return False
    return True


def check_validation_result(task_file: Path) -> bool:
    """检查验证结果：任务是否仍在 done/ 中（验证通过）"""
    return (DONE_DIR / task_file.name).exists()


# ══════════════════════════════════════════════════════════════
#  state.json 更新
# ══════════════════════════════════════════════════════════════

_state_started_at: float = 0.0


def update_state(
    iteration: int | None = None,
    phase: str | None = None,
    current_task: str | None = None,
) -> None:
    """更新 state.json，供 dashboard 读取"""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {
            "runtime": {
                "iteration": 0,
                "max_iterations": MAX_ITERATIONS,
                "phase": "idle",
                "current_task": None,
                "started_at": _state_started_at,
                "elapsed": 0,
            },
            "tasks": [],
            "logs": "",
        }

    rt = state["runtime"]
    if iteration is not None:
        rt["iteration"] = iteration
    if phase is not None:
        rt["phase"] = phase
    if current_task is not None:
        rt["current_task"] = current_task
    rt["elapsed"] = int(time.time() - _state_started_at) if _state_started_at else 0
    rt["max_iterations"] = MAX_ITERATIONS

    state["tasks"] = scan_tasks()

    try:
        state["logs"] = PROGRESS_FILE.read_text(encoding="utf-8") if PROGRESS_FILE.exists() else ""
    except Exception:
        state["logs"] = ""

    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ══════════════════════════════════════════════════════════════
#  归档
# ══════════════════════════════════════════════════════════════

def archive_if_needed() -> None:
    """如果 done/ 有内容但 tasks/ 无待执行任务，归档上一轮"""
    pending = list(TASKS_DIR.glob("*.md"))
    done = list(DONE_DIR.glob("*.md"))
    if not pending and done:
        date_str = time.strftime("%Y-%m-%d")
        archive_folder = ARCHIVE_DIR / f"{date_str}-tasks"
        archive_done = archive_folder / "tasks" / "done"
        archive_done.mkdir(parents=True, exist_ok=True)
        print(f"归档上一轮到: {archive_folder}")
        for f in done:
            shutil.copy2(f, archive_done)
        if PROGRESS_FILE.exists():
            shutil.copy2(PROGRESS_FILE, archive_folder)
        for f in done:
            f.unlink()
        PROGRESS_FILE.write_text(
            f"# Ralph Progress Log\nStarted: {time.strftime('%Y-%m-%d %H:%M:%S')}\n---\n",
            encoding="utf-8",
        )


# ══════════════════════════════════════════════════════════════
#  PM Agent 越权防护（目录树快照 / 校验 / 回滚）
# ══════════════════════════════════════════════════════════════

# 不可变 frontmatter 字段（除 reset 动作外，PM 不得修改）
_PM_IMMUTABLE_FIELDS = ("validation_notes", "retryCount", "blocked")


def _body_after_frontmatter(content: str) -> str:
    """返回 frontmatter 之后的正文，用于检测 PM 是否篡改任务正文"""
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    return content[end + 4:]


def snapshot_tasks_tree() -> dict:
    """
    以 frontmatter id 为键快照 tasks/ 与 done/ 全部任务。
    返回 {id: {filename, location, meta, body_hash}}。
    无 id 的文件以 'file::<文件名>' 兜底为键，保证可比对。
    """
    snap: dict = {}
    for loc, d in (("pending", TASKS_DIR), ("done", DONE_DIR)):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            meta = parse_frontmatter(f)
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                content = ""
            key = str(meta.get("id")) if meta.get("id") is not None else f"file::{f.name}"
            body = _body_after_frontmatter(content)
            snap[key] = {
                "filename": f.name,
                "location": loc,
                "meta": meta,
                "body_hash": hashlib.md5(body.encode("utf-8")).hexdigest(),
            }
    return snap


def backup_tasks_tree() -> None:
    """备份整个 tasks 树到 tasks.bak/，供越权时回滚"""
    if TASKS_BAK_DIR.exists():
        shutil.rmtree(TASKS_BAK_DIR, ignore_errors=True)
    if TASKS_DIR.exists():
        shutil.copytree(TASKS_DIR, TASKS_BAK_DIR)


def restore_tasks_tree() -> bool:
    """从 tasks.bak/ 恢复 tasks 树"""
    if not TASKS_BAK_DIR.exists():
        print("   无 tasks 备份可用，无法回滚")
        return False
    if TASKS_DIR.exists():
        shutil.rmtree(TASKS_DIR, ignore_errors=True)
    shutil.copytree(TASKS_BAK_DIR, TASKS_DIR)
    print("   tasks 已从备份回滚")
    return True


def _read_adjustments() -> list:
    """读取 adjustments.json，失败返回空列表"""
    try:
        data = json.loads(ADJUSTMENTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _documented_ids(before_adj_len: int, after_adj: list) -> set:
    """收集本轮新增审计记录中涉及的所有任务 id（targets + newStories）"""
    documented: set = set()
    for adj in after_adj[before_adj_len:]:
        for key in ("targets", "newStories"):
            val = adj.get(key)
            if isinstance(val, list):
                documented.update(str(x) for x in val)
    return documented


def validate_pm_task_changes(before: dict, after: dict, before_adj_len: int) -> tuple[bool, str, list]:
    """
    校验 PM 对任务树的改动是否越界。
    before/after 为 snapshot_tasks_tree() 结果；before_adj_len 为 PM 跑前 adjustments 条数。
    返回 (ok, reason, reset_ids)。reset_ids 为被 reset（done→pending）的任务 id。
    """
    after_adj = _read_adjustments()
    # adjustments 只能追加
    if len(after_adj) < before_adj_len:
        return False, "adjustments 审计记录被删除", []
    documented = _documented_ids(before_adj_len, after_adj)

    # 1) 删除任务必须有审计支撑（支持 split，禁止无故删除）
    undocumented_removed = (set(before) - set(after)) - documented
    if undocumented_removed:
        return False, f"未经审计删除了任务: {sorted(undocumented_removed)}", []

    # 2) 不可变字段保护 + 状态跃迁规则
    resets = []
    for tid, after_t in after.items():
        before_t = before.get(tid)
        if before_t is None:
            continue  # 新增任务，不做字段比对
        bmeta, ameta = before_t["meta"], after_t["meta"]
        for field in _PM_IMMUTABLE_FIELDS:
            if bmeta.get(field) != ameta.get(field):
                return False, f"{tid} 的不可变字段 {field} 被 PM 修改", []
        # 正文不得篡改
        if before_t["body_hash"] != after_t["body_hash"]:
            return False, f"{tid} 的任务正文被 PM 修改", []
        # pending → done（等效把未完成任务标记为完成，越权）
        if before_t["location"] == "pending" and after_t["location"] == "done":
            return False, f"{tid} 被 PM 从 tasks/ 移到 done/（越权）", []
        # done → pending（等效 reset，需审计支撑）
        if before_t["location"] == "done" and after_t["location"] == "pending":
            resets.append(tid)

    # 3) reset 必须有审计支撑
    undocumented_reset = set(resets) - documented
    if undocumented_reset:
        return False, f"未经审计 reset: {sorted(undocumented_reset)}", []

    # 4) 收敛保护
    if len(resets) > PM_MAX_RESETS:
        return False, f"单轮 reset 数 {len(resets)} 超上限 {PM_MAX_RESETS}", []
    net_new = len(after) - len(before)
    if net_new > PM_MAX_NEW_TASKS:
        return False, f"单轮净新增任务 {net_new} 超上限 {PM_MAX_NEW_TASKS}", []

    return True, "", resets


def _apply_reset_cleanup(after_snap: dict, reset_ids: list) -> None:
    """对被 reset 的任务清空 validation_notes / retryCount 归零（直接改文件 frontmatter）"""
    if not reset_ids:
        return
    for tid in reset_ids:
        info = after_snap.get(tid)
        if not info:
            continue
        f = TASKS_DIR / info["filename"]
        if not f.exists():
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        content = re.sub(r'(?m)^retryCount\s*:\s*.+$', 'retryCount: 0', content)
        content = re.sub(r'(?m)^validation_notes\s*:\s*.+$', 'validation_notes: ""', content)
        f.write_text(content, encoding="utf-8")


def _check_patterns_budget() -> None:
    """统计 docs/patterns-*.md 总行数，超阈值打印警告"""
    if not PATTERNS_DIR.exists():
        return
    total = 0
    for f in sorted(PATTERNS_DIR.glob("patterns-*.md")):
        try:
            total += len(f.read_text(encoding="utf-8").splitlines())
        except Exception:
            pass
    if total > PM_PATTERNS_WARN_LINES:
        print(f"   patterns 文档已达 {total} 行"
              f"（>{PM_PATTERNS_WARN_LINES}），建议精简以控制各 Agent 上下文压力")


def _guard_pm_changes(before_snap: dict, before_adj_len: int) -> None:
    """PM 跑完后的越权防护：校验任务树改动，越界则回滚"""
    after_snap = snapshot_tasks_tree()
    ok, reason, reset_ids = validate_pm_task_changes(before_snap, after_snap, before_adj_len)
    if not ok:
        print(f"   PM 改动越界（{reason}），tasks 从备份回滚")
        restore_tasks_tree()
    elif reset_ids:
        _apply_reset_cleanup(after_snap, reset_ids)
        print(f"   PM 重置了 {sorted(reset_ids)}，已同步清空其 validation_notes/retryCount")
    _check_patterns_budget()


# ══════════════════════════════════════════════════════════════
#  Prompt 构建
# ══════════════════════════════════════════════════════════════

def project_context_file() -> Path | None:
    """返回实际存在的项目上下文文件（优先 AGENTS.md，回退 CLAUDE.md）"""
    for p in PROJECT_CONTEXT_CANDIDATES:
        if p.exists():
            return p
    return None


def _read_project_context() -> tuple[str, str]:
    """返回 (上下文文件名, 内容)。不存在时返回 ("AGENTS.md", "")"""
    path = project_context_file()
    if path is None:
        return "AGENTS.md", ""
    return path.name, path.read_text(encoding="utf-8")


def build_developer_prompt(task_file: Path) -> str:
    """拼接: 项目上下文(AGENTS.md) + 任务 MD + Developer 指令"""
    ctx_name, project_ctx = _read_project_context()
    task_content = task_file.read_text(encoding="utf-8")
    ralph_instructions = DEVELOPER_INSTRUCTION.read_text(encoding="utf-8")
    task_name = task_file.name

    return (
        f"# 项目上下文（{ctx_name}）\n\n{project_ctx}\n\n---\n\n"
        f"# 当前任务\n\n**任务文件**: scripts/ralph/tasks/{task_name}\n\n"
        f"{task_content}\n\n---\n\n"
        f"# Agent 执行指令\n\n{ralph_instructions}\n\n---\n"
        f"立即开始执行以上指令。不要询问确认，直接开始工作。"
        f"严格遵守：只完成当前任务就停止响应，禁止继续处理下一个。"
    )


def build_validator_prompt(task_file: Path) -> str:
    """拼接: 项目上下文(AGENTS.md) + 验证指令 + 当前任务文件路径"""
    ctx_name, project_ctx = _read_project_context()
    validator_instructions = VALIDATOR_INSTRUCTION.read_text(encoding="utf-8")
    task_name = task_file.name

    return (
        f"# 项目上下文（{ctx_name}）\n\n{project_ctx}\n\n---\n\n"
        f"# 验证 Agent 指令\n\n{validator_instructions}\n\n---\n\n"
        f"**当前需要验证的任务文件**: scripts/ralph/tasks/done/{task_name}\n\n"
        f"立即开始验证。不要询问确认，直接开始工作。"
    )


def build_pm_prompt() -> str:
    """拼接: 项目上下文(AGENTS.md) + PM 指令 + 执行触发语"""
    ctx_name, project_ctx = _read_project_context()
    pm_instructions = PM_INSTRUCTION.read_text(encoding="utf-8")

    return (
        f"# 项目上下文（{ctx_name}）\n\n{project_ctx}\n\n---\n\n"
        f"# PM Agent 指令\n\n{pm_instructions}\n\n---\n\n"
        f"立即开始治理工作。不要询问确认，直接开始。"
        f"如评估下来无 patterns 可沉淀、无任务队列需调整，正常结束即可，不要强行写入。"
    )


# ══════════════════════════════════════════════════════════════
#  Agent 调用（含重试）
# ══════════════════════════════════════════════════════════════

def run_developer(iteration: int, task_file: Path) -> bool:
    """
    调用开发 Agent（含重试）。
    返回值：True=超时需跳过, False=正常完成
    """
    print(f"\n{'='*64}")
    print(f"  迭代 {iteration}/{MAX_ITERATIONS} - 开发 Agent")
    print(f"  任务: {task_file.name}")
    print(f"{'='*64}")

    prompt = build_developer_prompt(task_file)
    cmd = build_cmd()
    result = run_cc_stream(cmd, prompt, "开发 Agent", TIMEOUT_SECONDS, role="developer")

    if result == "ok":
        print("\n  开发迭代完成")
        return False
    if result == "timeout":
        print("  进程已终止，将在下一次迭代重试")
        return True
    if result in ("idle", "api_error", "error"):
        reasons = {"idle": "模型无响应", "api_error": "模型返回错误", "error": "进程异常退出"}
        reason = reasons[result]
        print(f"  {reason}，等待 5 秒后重试...")
        time.sleep(5)
        result2 = run_cc_stream(cmd, prompt, "开发 Agent (重试)", TIMEOUT_SECONDS, role="developer")
        if result2 == "ok":
            print("\n  开发迭代完成 (重试成功)")
            return False
        print(f"\n  开发 Agent 连续两次失败 ({reason})，脚本终止！")
        sys.exit(2)
    return True


def run_validator(iteration: int, task_file: Path) -> None:
    """调用 Validator Agent（含重试）"""
    print(f"\n{'='*64}")
    print(f"  迭代 {iteration} - Validator 验证")
    print(f"  验证任务: {task_file.name}")
    print(f"{'='*64}")

    if not VALIDATOR_INSTRUCTION.exists():
        print("  警告: VALIDATOR.md 不存在，跳过验证")
        return

    prompt = build_validator_prompt(task_file)
    cmd = build_cmd()
    result = run_cc_stream(cmd, prompt, "Validator", TIMEOUT_SECONDS * 2, role="validator")

    if result == "ok":
        print("\n  验证完成")
        return
    if result in ("idle", "api_error", "error"):
        reasons = {"idle": "模型无响应", "api_error": "模型返回错误", "error": "进程异常退出"}
        reason = reasons[result]
        print(f"  {reason}，等待 5 秒后重试...")
        time.sleep(5)
        result2 = run_cc_stream(cmd, prompt, "Validator (重试)", TIMEOUT_SECONDS * 2, role="validator")
        if result2 == "ok":
            print("\n  验证完成 (重试成功)")
            return
        print(f"\n  Validator 连续两次失败 ({reason})，脚本终止！")
        sys.exit(2)
    if result == "timeout":
        print("  Validator 超时，跳过本次验证")


def run_pm(iteration: int) -> None:
    """调用 PM Agent（含重试）。失败不终止主流程，仅跳过本次规划"""
    print(f"\n{'='*64}")
    print(f"  迭代 {iteration} - PM Agent 治理（沉淀 + 规划）")
    print(f"{'='*64}")

    if not PM_INSTRUCTION.exists():
        print("  警告: PM.md 不存在，跳过规划")
        return

    prompt = build_pm_prompt()
    cmd = build_cmd()
    result = run_cc_stream(cmd, prompt, "PM", TIMEOUT_SECONDS, role="pm")

    if result == "ok":
        print("\n  规划完成")
        return
    if result in ("idle", "api_error", "error"):
        reasons = {"idle": "模型无响应", "api_error": "模型返回错误", "error": "进程异常退出"}
        reason = reasons[result]
        print(f"  {reason}，等待 5 秒后重试...")
        time.sleep(5)
        result2 = run_cc_stream(cmd, prompt, "PM (重试)", TIMEOUT_SECONDS, role="pm")
        if result2 == "ok":
            print("\n  规划完成 (重试成功)")
            return
        print(f"  PM 连续两次失败 ({reason})，跳过本次规划（不影响主流程）")
        return
    if result == "timeout":
        print("  PM 超时，跳过本次规划")


# ══════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════

def format_duration(seconds: float) -> str:
    """将秒数格式化为易读的时间字符串"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}小时 {m}分钟 {s}秒"
    elif m > 0:
        return f"{m}分钟 {s}秒"
    else:
        return f"{s}秒"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ralph - 自主 AI Agent 循环执行器")
    parser.add_argument("agent", nargs="?", default="pi",
                        choices=["pi"],
                        help="Agent 后端 (默认: pi)")
    parser.add_argument("--max-iterations", type=int, default=50,
                        help="最大迭代次数 (默认: 50)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="不启动监控面板")
    parser.add_argument("--port", type=int, default=17331,
                        help="监控面板端口 (默认: 17331)")
    parser.add_argument("--run-id", default=None,
                        help="本次运行标识（Langfuse release 标签），默认时间戳")
    parser.add_argument("--no-validator", action="store_true",
                        help="跳过 Validator（对照实验用）")
    parser.add_argument("--no-pm", action="store_true",
                        help="跳过 PM 治理（对照实验用）")
    parser.add_argument("--no-guard", action="store_true",
                        help="关闭 PM 越权回滚（对照实验用）")
    return parser.parse_args()


# ══════════════════════════════════════════════════════════════
#  主函数
# ══════════════════════════════════════════════════════════════

def main():
    global MAX_ITERATIONS, AGENT, _state_started_at, LANGFUSE_RUN_ID
    global NO_VALIDATOR, NO_PM, NO_GUARD

    args = parse_args()
    MAX_ITERATIONS = args.max_iterations
    AGENT = args.agent
    if args.run_id:
        LANGFUSE_RUN_ID = args.run_id
    NO_VALIDATOR = args.no_validator
    NO_PM = args.no_pm
    NO_GUARD = args.no_guard

    print(f"启动 Ralph - Agent: {AGENT} - 最大迭代: {MAX_ITERATIONS} - run-id: {LANGFUSE_RUN_ID}")
    if NO_VALIDATOR or NO_PM or NO_GUARD:
        print(f"  对照模式: validator={'off' if NO_VALIDATOR else 'on'}, "
              f"pm={'off' if NO_PM else 'on'}, guard={'off' if NO_GUARD else 'on'}")

    # 前置检查
    if project_context_file() is None:
        print("错误: 项目上下文文件不存在，请在项目根目录创建 AGENTS.md"
              f"（或 CLAUDE.md）: {PROJECT_ROOT}")
        sys.exit(1)
    if not DEVELOPER_INSTRUCTION.exists():
        print(f"错误: 开发 Agent 指令不存在: {DEVELOPER_INSTRUCTION}")
        sys.exit(1)

    DONE_DIR.mkdir(parents=True, exist_ok=True)
    archive_if_needed()

    # 初始化 progress.txt
    if not PROGRESS_FILE.exists():
        PROGRESS_FILE.write_text(
            f"# Ralph Progress Log\nStarted: {time.strftime('%Y-%m-%d %H:%M:%S')}\n---\n",
            encoding="utf-8",
        )

    _state_started_at = time.time()

    # 启动 Dashboard
    if not args.no_dashboard:
        try:
            import dashboard
            dashboard.start(port=args.port, max_iterations=MAX_ITERATIONS)
        except Exception as e:
            print(f"  Dashboard 启动失败: {e}，继续运行...")

    # 初始化 state.json
    update_state(iteration=0, phase="idle", current_task="")

    os.chdir(PROJECT_ROOT)

    for i in range(1, MAX_ITERATIONS + 1):
        try:
            # 获取下一个任务
            task_file = get_next_task()
            if not task_file:
                update_state(iteration=i, phase="done")
                elapsed = time.time() - _state_started_at
                print(f"\n所有任务已完成！总运行时间: {format_duration(elapsed)}")
                sys.exit(0)

            task_meta = parse_frontmatter(task_file)
            task_id = str(task_meta.get("id", task_file.stem))

            # 第一步：开发 Agent
            update_state(iteration=i, phase="developing", current_task=task_id)
            timed_out = run_developer(i, task_file)

            if timed_out:
                update_state(phase="idle")
                print("  开发 Agent 超时，跳过验证，下一迭代继续...")
                time.sleep(2)
                continue

            # 第二步：验证 Agent（--no-validator 可跳过，用于对照实验）
            done_task = DONE_DIR / task_file.name
            if not done_task.exists():
                print("  警告: 开发 Agent 未将任务移到 done/，跳过验证")
            elif NO_VALIDATOR:
                print(f"  已跳过验证（--no-validator），任务 {task_id} 直接视为完成")
            else:
                update_state(phase="validating")
                run_validator(i, task_file)

                if not check_validation_result(task_file):
                    retry_meta = parse_frontmatter(TASKS_DIR / task_file.name)
                    retry_count = retry_meta.get("retryCount", 0)
                    print(f"  验证失败 (第 {retry_count} 次重试)")
                else:
                    print(f"  任务 {task_id} 验证通过")

            # 第三步：PM Agent 治理（--no-pm 可跳过；--no-guard 关闭越权回滚以做对照）
            if PM_INSTRUCTION.exists() and not NO_PM:
                update_state(phase="planning")
                if NO_GUARD:
                    run_pm(i)
                else:
                    backup_tasks_tree()
                    pm_before_snap = snapshot_tasks_tree()
                    pm_before_adj_len = len(_read_adjustments())
                    run_pm(i)
                    _guard_pm_changes(pm_before_snap, pm_before_adj_len)

            # 第四步：检查是否全部完成
            update_state(phase="idle")
            if all_tasks_resolved():
                update_state(phase="done")
                elapsed = time.time() - _state_started_at
                print(f"\n所有任务已完成！总运行时间: {format_duration(elapsed)}")
                sys.exit(0)

        except KeyboardInterrupt:
            elapsed = time.time() - _state_started_at
            print(f"\n\n用户中断。总运行时间: {format_duration(elapsed)}")
            update_state(phase="idle")
            sys.exit(130)

    elapsed = time.time() - _state_started_at
    print(f"\n已达最大迭代次数 ({MAX_ITERATIONS})。总运行时间: {format_duration(elapsed)}")
    update_state(phase="idle")
    sys.exit(1)


if __name__ == "__main__":
    main()
