#!/usr/bin/env python3
"""
引擎核心逻辑单元测试（确定性，不调 LLM、不联网）。

覆盖：
  - parse_frontmatter（各种类型与异常格式）
  - 任务调度：scan_tasks / get_next_task / all_tasks_resolved
  - pi JSON 事件流解析：parse_stream_line / _format_tool
  - 工具函数：format_duration
  - Langfuse 环境注入：_inject_langfuse_env

运行：
  python3 scripts/ralph/tests/test_engine.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ralph  # noqa: E402


def task_text(tid: str, *, priority: int = 1, blocked: bool | None = None,
              retry: int | None = None) -> str:
    fm = [f'id: "{tid}"', f'title: "T{tid}"', f"priority: {priority}"]
    if blocked is not None:
        fm.append(f"blocked: {'true' if blocked else 'false'}")
    if retry is not None:
        fm.append(f"retryCount: {retry}")
    return "---\n" + "\n".join(fm) + "\n---\n\nbody\n"


class DirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ralph-engine-"))
        self.tasks = self.root / "tasks"
        self.done = self.tasks / "done"
        self.done.mkdir(parents=True)
        self._orig = (ralph.TASKS_DIR, ralph.DONE_DIR)
        ralph.TASKS_DIR, ralph.DONE_DIR = self.tasks, self.done

    def tearDown(self) -> None:
        ralph.TASKS_DIR, ralph.DONE_DIR = self._orig
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, tid: str, location: str = "pending", **kw) -> Path:
        d = self.tasks if location == "pending" else self.done
        f = d / f"{tid}.md"
        f.write_text(task_text(tid, **kw), encoding="utf-8")
        return f


# ══════════════════════════════════════════════════════════════
#  parse_frontmatter
# ══════════════════════════════════════════════════════════════

class TestParseFrontmatter(unittest.TestCase):
    def _parse(self, text: str) -> dict:
        f = Path(tempfile.mkdtemp()) / "t.md"
        f.write_text(text, encoding="utf-8")
        return ralph.parse_frontmatter(f)

    def test_basic_types(self):
        m = self._parse('---\nid: "001"\ntitle: "x"\npriority: 3\nretryCount: 2\nblocked: true\n---\n\nbody\n')
        self.assertEqual(m["id"], "001")          # id 保持字符串
        self.assertEqual(m["priority"], 3)         # 数字转 int
        self.assertEqual(m["retryCount"], 2)
        self.assertIs(m["blocked"], True)          # bool

    def test_no_frontmatter(self):
        self.assertEqual(self._parse("# just markdown\n"), {})

    def test_unterminated_frontmatter(self):
        self.assertEqual(self._parse('---\nid: "001"\nno close\n'), {})

    def test_single_and_double_quotes_stripped(self):
        m = self._parse("---\nid: '007'\ntitle: \"hello world\"\n---\n\nb\n")
        self.assertEqual(m["id"], "007")
        self.assertEqual(m["title"], "hello world")

    def test_leading_zero_id_stays_string(self):
        self.assertEqual(self._parse('---\nid: "0001"\n---\n\nb\n')["id"], "0001")


# ══════════════════════════════════════════════════════════════
#  任务调度
# ══════════════════════════════════════════════════════════════

class TestTaskScheduling(DirTestCase):
    def test_get_next_task_by_filename_order(self):
        self.write("003")
        self.write("001")
        self.write("002")
        nxt = ralph.get_next_task()
        self.assertEqual(nxt.name, "001.md")

    def test_get_next_task_skips_blocked(self):
        self.write("001", blocked=True)
        self.write("002")
        self.assertEqual(ralph.get_next_task().name, "002.md")

    def test_all_tasks_resolved(self):
        # 无任务时视为已全部解决
        self.assertTrue(ralph.all_tasks_resolved())

    def test_all_tasks_resolved_with_blocked_only(self):
        self.write("001", blocked=True)
        self.assertTrue(ralph.all_tasks_resolved())

    def test_all_tasks_resolved_with_pending(self):
        self.write("001")
        self.assertFalse(ralph.all_tasks_resolved())

    def test_scan_tasks_status_and_sort(self):
        self.write("001", priority=2)
        self.write("002", priority=1)
        self.write("003", location="done")
        self.write("004", blocked=True, priority=3)
        tasks = ralph.scan_tasks()
        by_id = {t["id"]: t for t in tasks}
        self.assertEqual(by_id["001"]["status"], "pending")
        self.assertEqual(by_id["003"]["status"], "done")
        self.assertEqual(by_id["004"]["status"], "blocked")
        self.assertEqual(tasks[0]["priority"], 1)  # 按 priority 排序


# ══════════════════════════════════════════════════════════════
#  pi JSON 事件流解析
# ══════════════════════════════════════════════════════════════

class TestParseStreamLine(unittest.TestCase):
    def p(self, obj) -> str | None:
        return ralph.parse_stream_line(json.dumps(obj) if not isinstance(obj, str) else obj)

    def test_invalid_json_returns_none(self):
        self.assertIsNone(ralph.parse_stream_line("not json"))
        self.assertIsNone(self.p({"type": "totally_unknown_type"}))

    def test_known_silent_event_returns_empty(self):
        self.assertEqual(self.p({"type": "turn_start"}), "")

    def test_session(self):
        self.assertIn("pi 会话", self.p({"type": "session", "id": "abc"}))

    def test_assistant_text(self):
        out = self.p({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "hi"}})
        self.assertEqual(out, "[Assistant] hi")

    def test_thinking(self):
        out = self.p({"type": "message_update", "assistantMessageEvent": {"type": "thinking_end", "content": "hmm"}})
        self.assertEqual(out, "[Thinking] hmm")

    def test_tool_start(self):
        out = self.p({"type": "tool_execution_start", "toolName": "read", "args": {"path": "/a/b/c.py"}})
        self.assertEqual(out, "[Tool] read: c.py")

    def test_tool_error(self):
        out = self.p({"type": "tool_execution_end", "isError": True, "toolName": "bash"})
        self.assertEqual(out, "[Tool Error] bash")

    def test_turn_end_with_cost(self):
        out = self.p({"type": "turn_end", "message": {"usage": {"cost": {"total": 0.01}}}})
        self.assertEqual(out, "[Turn] 费用: $0.01")

    def test_turn_end_without_cost_empty(self):
        self.assertEqual(self.p({"type": "turn_end", "message": {}}), "")

    def test_auto_retry_start(self):
        out = self.p({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "errorMessage": "x"})
        self.assertIn("第 1/3 次", out)

    def test_agent_settled(self):
        self.assertIn("迭代结果", self.p({"type": "agent_settled"}))


class TestFormatTool(unittest.TestCase):
    def test_read(self):
        self.assertEqual(ralph._format_tool("read", {"path": "/x/y/z.txt"}), "[Tool] read: z.txt")

    def test_bash_truncates(self):
        out = ralph._format_tool("bash", {"command": "a" * 200})
        self.assertTrue(out.startswith("[Tool] bash: "))
        self.assertLessEqual(len(out), len("[Tool] bash: ") + 80)

    def test_grep(self):
        self.assertEqual(ralph._format_tool("grep", {"pattern": "foo"}), "[Tool] grep: foo")

    def test_unknown(self):
        self.assertEqual(ralph._format_tool("weird", {}), "[Tool] weird")


# ══════════════════════════════════════════════════════════════
#  工具函数 & 环境注入
# ══════════════════════════════════════════════════════════════

class TestMisc(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(ralph.format_duration(30), "30秒")
        self.assertEqual(ralph.format_duration(90), "1分钟 30秒")
        self.assertEqual(ralph.format_duration(3661), "1小时 1分钟 1秒")

    def test_inject_langfuse_env(self):
        os.environ.pop("RALPH_NO_LANGFUSE", None)
        env: dict = {}
        ralph._inject_langfuse_env(env, "developer")
        self.assertEqual(env["LANGFUSE_TRACING_ENVIRONMENT"], "ralph")
        self.assertEqual(env["LANGFUSE_USER_ID"], "developer")
        self.assertIn("LANGFUSE_RELEASE", env)

    def test_inject_langfuse_disabled(self):
        os.environ["RALPH_NO_LANGFUSE"] = "1"
        try:
            env: dict = {}
            ralph._inject_langfuse_env(env, "pm")
            self.assertEqual(env.get("LANGFUSE_TRACING_ENABLED"), "false")
            self.assertNotIn("LANGFUSE_RELEASE", env)
        finally:
            os.environ.pop("RALPH_NO_LANGFUSE", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
