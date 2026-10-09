#!/usr/bin/env python3
"""
PM 越权防腐（guardrail）对抗性单元测试。

被测对象：scripts/ralph/ralph.py 中的
  - parse_frontmatter
  - snapshot_tasks_tree
  - validate_pm_task_changes
  - _apply_reset_cleanup / backup_tasks_tree / restore_tasks_tree

测试思路：构造 PM「越权」与「合法」两类目录树改动，断言校验器给出正确判定。
全部为确定性测试，不调用 LLM、不联网，可进 CI。

运行：
  python3 scripts/ralph/tests/test_pm_guard.py        # 内置 unittest
  python3 -m pytest scripts/ralph/tests/ -q           # 若装了 pytest
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# 让测试能 import 到 scripts/ralph/ralph.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ralph  # noqa: E402


def make_task_text(tid: str, body: str = "## 任务描述\n原始正文\n",
                   retry: int | None = None, blocked: bool | None = None,
                   notes: str | None = None) -> str:
    """构造一个合法的任务 MD（带 frontmatter）。"""
    fm = [f'id: "{tid}"', f'title: "任务 {tid}"', "priority: 1"]
    if retry is not None:
        fm.append(f"retryCount: {retry}")
    if blocked is not None:
        fm.append(f"blocked: {'true' if blocked else 'false'}")
    if notes is not None:
        fm.append(f'validation_notes: "{notes}"')
    return "---\n" + "\n".join(fm) + "\n---\n" + body


class GuardTestCase(unittest.TestCase):
    """把 ralph 的目录常量重定向到临时目录，构造可控的任务树。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ralph-guard-"))
        self.tasks_dir = self.root / "tasks"
        self.done_dir = self.tasks_dir / "done"
        self.bak_dir = self.root / "tasks.bak"
        self.adj_file = self.root / "adjustments.json"
        self.tasks_dir.mkdir(parents=True)
        self.done_dir.mkdir(parents=True)
        self.adj_file.write_text("[]", encoding="utf-8")

        # 重定向模块级常量
        self._orig = {
            "TASKS_DIR": ralph.TASKS_DIR,
            "DONE_DIR": ralph.DONE_DIR,
            "TASKS_BAK_DIR": ralph.TASKS_BAK_DIR,
            "ADJUSTMENTS_FILE": ralph.ADJUSTMENTS_FILE,
        }
        ralph.TASKS_DIR = self.tasks_dir
        ralph.DONE_DIR = self.done_dir
        ralph.TASKS_BAK_DIR = self.bak_dir
        ralph.ADJUSTMENTS_FILE = self.adj_file

    def tearDown(self) -> None:
        for k, v in self._orig.items():
            setattr(ralph, k, v)
        shutil.rmtree(self.root, ignore_errors=True)

    # ── helpers ────────────────────────────────────────────────

    def write_task(self, tid: str, location: str = "pending", **kw) -> Path:
        d = self.tasks_dir if location == "pending" else self.done_dir
        f = d / f"{tid}-task.md"
        f.write_text(make_task_text(tid, **kw), encoding="utf-8")
        return f

    def snapshot(self) -> dict:
        return ralph.snapshot_tasks_tree()

    def append_adjustment(self, action: str, targets=None, new=None) -> None:
        data = json.loads(self.adj_file.read_text(encoding="utf-8"))
        data.append({
            "iteration": 1, "timestamp": "2026-10-09 10:00",
            "action": action, "targets": targets or [],
            "newStories": new or [], "reason": "test",
        })
        self.adj_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def validate(self, before: dict, after: dict, before_adj_len: int = 0):
        return ralph.validate_pm_task_changes(before, after, before_adj_len)

    # ── 1. 合法：无改动 ────────────────────────────────────────

    def test_no_change_is_ok(self):
        self.write_task("t1")
        before = self.snapshot()
        ok, reason, resets = self.validate(before, self.snapshot())
        self.assertTrue(ok, reason)
        self.assertEqual(resets, [])

    # ── 2. 不可变字段篡改 ─────────────────────────────────────

    def test_tamper_retrycount_rolls_back(self):
        self.write_task("t1", retry=0)
        before = self.snapshot()
        self.write_task("t1", retry=3)  # PM 篡改 retryCount
        ok, reason, _ = self.validate(before, self.snapshot())
        self.assertFalse(ok)
        self.assertIn("不可变字段", reason)

    def test_tamper_validation_notes_rolls_back(self):
        self.write_task("t1", notes="原记录")
        before = self.snapshot()
        self.write_task("t1", notes="被 PM 改写")
        ok, reason, _ = self.validate(before, self.snapshot())
        self.assertFalse(ok)
        self.assertIn("不可变字段", reason)

    def test_tamper_blocked_rolls_back(self):
        self.write_task("t1", blocked=False)
        before = self.snapshot()
        self.write_task("t1", blocked=True)
        ok, reason, _ = self.validate(before, self.snapshot())
        self.assertFalse(ok)
        self.assertIn("不可变字段", reason)

    # ── 3. 正文篡改 ───────────────────────────────────────────

    def test_tamper_body_rolls_back(self):
        self.write_task("t1", body="## 任务描述\n原始正文\n")
        before = self.snapshot()
        self.write_task("t1", body="## 任务描述\nPM 偷偷改成了简单版本\n")
        ok, reason, _ = self.validate(before, self.snapshot())
        self.assertFalse(ok)
        self.assertIn("正文", reason)

    # ── 4. 删除任务 ───────────────────────────────────────────

    def test_undocumented_delete_rolls_back(self):
        self.write_task("t1")
        before = self.snapshot()
        (self.tasks_dir / "t1-task.md").unlink()
        ok, reason, _ = self.validate(before, self.snapshot())
        self.assertFalse(ok)
        self.assertIn("未经审计删除", reason)

    def test_documented_split_delete_is_ok(self):
        self.write_task("t1")
        before = self.snapshot()
        (self.tasks_dir / "t1-task.md").unlink()          # split：归档原任务
        self.write_task("t1a")                             # 拆成两个子任务
        self.write_task("t1b")
        self.append_adjustment("split", targets=["t1"], new=["t1a", "t1b"])
        ok, reason, _ = self.validate(before, self.snapshot(), before_adj_len=0)
        self.assertTrue(ok, reason)

    # ── 5. 伪造完成（pending → done）──────────────────────────

    def test_forge_completion_rolls_back(self):
        f = self.write_task("t1", location="pending")
        before = self.snapshot()
        # 未经开发/验证，PM 直接把未完成任务标记为完成
        (self.done_dir / f.name).write_text(make_task_text("t1"), encoding="utf-8")
        f.unlink()
        ok, reason, _ = self.validate(before, self.snapshot())
        self.assertFalse(ok)
        self.assertIn("done/", reason)

    # ── 6. reset（done → pending）────────────────────────────

    def test_undocumented_reset_rolls_back(self):
        f = self.write_task("t1", location="done")
        before = self.snapshot()
        (self.tasks_dir / f.name).write_text(make_task_text("t1"), encoding="utf-8")
        f.unlink()
        ok, reason, _ = self.validate(before, self.snapshot())
        self.assertFalse(ok)
        self.assertIn("未经审计 reset", reason)

    def test_documented_reset_is_ok(self):
        f = self.write_task("t1", location="done")
        before = self.snapshot()
        (self.tasks_dir / f.name).write_text(make_task_text("t1"), encoding="utf-8")
        f.unlink()
        self.append_adjustment("reset", targets=["t1"])
        ok, reason, resets = self.validate(before, self.snapshot(), before_adj_len=0)
        self.assertTrue(ok, reason)
        self.assertEqual(resets, ["t1"])

    # ── 7. 收敛上限 ───────────────────────────────────────────

    def test_reset_over_limit_rolls_back(self):
        files = [self.write_task(f"t{i}", location="done") for i in (1, 2, 3)]
        before = self.snapshot()
        for f in files:
            (self.tasks_dir / f.name).write_text(make_task_text(f.stem.split("-")[0]),
                                                 encoding="utf-8")
            f.unlink()
        self.append_adjustment("reset", targets=["t1", "t2", "t3"])
        ok, reason, _ = self.validate(before, self.snapshot(), before_adj_len=0)
        self.assertFalse(ok)
        self.assertIn("超上限", reason)

    def test_net_new_over_limit_rolls_back(self):
        self.write_task("t1")
        before = self.snapshot()
        for i in range(1, 5):  # 净新增 4 > 3
            self.write_task(f"n{i}")
        ok, reason, _ = self.validate(before, self.snapshot())
        self.assertFalse(ok)
        self.assertIn("净新增", reason)

    def test_net_new_within_limit_is_ok(self):
        self.write_task("t1")
        before = self.snapshot()
        for i in range(1, 4):  # 净新增 3 <= 3
            self.write_task(f"n{i}")
        ok, reason, _ = self.validate(before, self.snapshot())
        self.assertTrue(ok, reason)

    # ── 8. 审计日志被删 ───────────────────────────────────────

    def test_adjustments_deleted_rolls_back(self):
        self.write_task("t1")
        self.append_adjustment("add", new=["x"])
        before = self.snapshot()
        self.adj_file.write_text("[]", encoding="utf-8")  # PM 删掉了审计记录
        ok, reason, _ = self.validate(before, self.snapshot(), before_adj_len=1)
        self.assertFalse(ok)
        self.assertIn("审计记录被删除", reason)

    # ── 9. reset 后联动清理 ───────────────────────────────────

    def test_reset_cleanup_clears_fields(self):
        # reset 后任务应位于 tasks/（pending），此处直接构造该状态
        self.write_task("t1", location="pending", retry=4, notes="上次失败原因")
        after = self.snapshot()
        ralph._apply_reset_cleanup(after, ["t1"])
        meta = ralph.parse_frontmatter(self.tasks_dir / "t1-task.md")
        self.assertEqual(int(meta.get("retryCount", 0)), 0)
        self.assertEqual(meta.get("validation_notes", ""), "")

    # ── 10. 备份 / 回滚 ───────────────────────────────────────

    def test_backup_and_restore_roundtrip(self):
        self.write_task("t1")
        ralph.backup_tasks_tree()
        before = self.snapshot()
        self.write_task("injected")          # 模拟 PM 越权新增
        self.assertTrue(self.snapshot() != before)
        self.assertTrue(ralph.restore_tasks_tree())
        self.assertEqual(self.snapshot(), before)

    # ── 11. id 字符串回归 ─────────────────────────────────────

    def test_frontmatter_id_keeps_string(self):
        """回归：id "001" 不能被转成整数 1，否则快照键与审计 id 不匹配。"""
        self.write_task("001")
        meta = ralph.parse_frontmatter(self.tasks_dir / "001-task.md")
        self.assertEqual(meta.get("id"), "001")

    def test_documented_split_with_numeric_id_is_ok(self):
        """带前导零的数字 id（001）也应能正常审计 split。"""
        self.write_task("001")
        before = self.snapshot()
        (self.tasks_dir / "001-task.md").unlink()
        self.write_task("001a")
        self.write_task("001b")
        self.append_adjustment("split", targets=["001"], new=["001a", "001b"])
        ok, reason, _ = self.validate(before, self.snapshot(), before_adj_len=0)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
