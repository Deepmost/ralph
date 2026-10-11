#!/usr/bin/env python3
"""
泡测守护器单元测试（确定性，不调 LLM、不联网、不启动子进程）。

覆盖：
  - parse_iteration / parse_cost（含科学计数法）
  - backoff_delay（指数退避与封顶）
  - 引擎日志判定：is_exhausted / is_max_iterations / is_double_failure / is_guard_rollback
  - 任务可运行性：has_runnable_task / count_runnable
  - 命令构建：Supervisor.build_cmd

运行：
  python3 scripts/ralph/tests/test_soak.py
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "soak"))
import soak  # noqa: E402


class TestParsers(unittest.TestCase):
    def test_parse_iteration(self):
        self.assertEqual(soak.parse_iteration("  迭代 12/60 - 开发 Agent"), 12)
        self.assertEqual(soak.parse_iteration("迭代 1/400"), 1)
        self.assertIsNone(soak.parse_iteration("开发完成"))

    def test_parse_cost(self):
        self.assertAlmostEqual(soak.parse_cost("[Turn] 费用: $0.000758886"), 0.000758886)
        self.assertAlmostEqual(soak.parse_cost("[Turn] 费用: $9.6e-05"), 9.6e-05)
        self.assertIsNone(soak.parse_cost("[Tool] read: x.py"))

    def test_backoff_delay(self):
        self.assertEqual(soak.backoff_delay(1, base=5, cap=60), 5)
        self.assertEqual(soak.backoff_delay(2, base=5, cap=60), 10)
        self.assertEqual(soak.backoff_delay(3, base=5, cap=60), 20)
        self.assertEqual(soak.backoff_delay(10, base=5, cap=60), 60)
        self.assertEqual(soak.backoff_delay(0, base=5, cap=60), 5)


class TestLogPredicates(unittest.TestCase):
    def test_predicates(self):
        self.assertTrue(soak.is_exhausted("\n所有任务已完成！总运行时间: 1小时"))
        self.assertTrue(soak.is_max_iterations("已达最大迭代次数 (400)。"))
        self.assertTrue(soak.is_double_failure("  开发 Agent 连续两次失败 (模型无响应)，脚本终止！"))
        self.assertTrue(soak.is_guard_rollback("   PM 改动越界（tasks 正文被改），tasks 从备份回滚"))
        self.assertFalse(soak.is_exhausted("迭代 3/60"))
        self.assertFalse(soak.is_guard_rollback("普通日志"))


class TestRunnableTasks(unittest.TestCase):
    def test_has_runnable_task(self):
        ok = '---\nid: "001"\ntitle: "x"\n---\n\n## 任务\n'
        blocked = '---\nid: "002"\nblocked: true\n---\n'
        self.assertTrue(soak.has_runnable_task(ok))
        self.assertFalse(soak.has_runnable_task(blocked))
        self.assertFalse(soak.has_runnable_task("# 只是文档，没有 id"))

    def test_count_runnable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "001-a.md").write_text('---\nid: "001"\n---\n', encoding="utf-8")
            (root / "002-b.md").write_text('---\nid: "002"\nblocked: true\n---\n', encoding="utf-8")
            (root / "README.md").write_text("no id here", encoding="utf-8")
            (root / "done").mkdir()
            (root / "done" / "000-c.md").write_text('---\nid: "000"\n---\n', encoding="utf-8")
            self.assertEqual(soak.count_runnable(root), 1)


class TestBuildCmd(unittest.TestCase):
    def _sup(self, args):
        sup = soak.Supervisor.__new__(soak.Supervisor)
        sup.args = args
        sup.run_id = "soak-test"
        return sup

    def _args(self, **over):
        base = dict(
            python="python3", ralph_py="/tmp/ralph.py", max_iterations=400,
            with_dashboard=False, port=17331, no_validator=False, no_pm=False,
            no_guard=False, extra=[],
        )
        base.update(over)
        return argparse.Namespace(**base)

    def test_default_cmd(self):
        cmd = self._sup(self._args()).build_cmd()
        self.assertEqual(cmd[0], "python3")
        self.assertEqual(cmd[1], str(Path("/tmp/ralph.py").resolve()))
        self.assertEqual(cmd[2], "pi")
        self.assertIn("--max-iterations", cmd)
        self.assertIn("400", cmd)
        self.assertIn("--no-dashboard", cmd)
        self.assertIn("--run-id", cmd)
        self.assertIn("soak-test", cmd)

    def test_flags_and_extra(self):
        args = self._args(no_validator=True, no_pm=True, no_guard=True,
                          extra=["--foo", "bar"])
        cmd = self._sup(args).build_cmd()
        for flag in ("--no-validator", "--no-pm", "--no-guard"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[-2:], ["--foo", "bar"])

    def test_dashboard_uses_port(self):
        cmd = self._sup(self._args(with_dashboard=True, port=8080)).build_cmd()
        self.assertNotIn("--no-dashboard", cmd)
        self.assertIn("--port", cmd)
        self.assertIn("8080", cmd)


class TestRunArtifactIsolation(unittest.TestCase):
    """复用同一 --out 目录重跑时，上一轮产物必须被归档，避免跨运行污染。"""

    def _sup(self, out: Path, run_id: str) -> soak.Supervisor:
        sup = soak.Supervisor.__new__(soak.Supervisor)
        sup.out = out
        sup.run_id = run_id
        return sup

    def test_rotate_foreign_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "metrics.jsonl").write_text(
                json.dumps({"run_id": "old", "ts": "2026-10-10T10:00:00+00:00"}) + "\n",
                encoding="utf-8")
            (out / "master.log").write_text("old\n", encoding="utf-8")
            (out / "resource.csv").write_text("hdr\n", encoding="utf-8")
            (out / "restart-0.log").write_text("x\n", encoding="utf-8")
            sup = self._sup(out, "new")
            sup._rotate_foreign_run_artifacts()
            for name in ("metrics.jsonl", "master.log", "resource.csv", "restart-0.log"):
                self.assertFalse((out / name).exists(), name)
                self.assertTrue((out / "previous-old" / name).exists(), name)

    def test_keep_same_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "metrics.jsonl").write_text(
                json.dumps({"run_id": "same"}) + "\n", encoding="utf-8")
            sup = self._sup(out, "same")
            sup._rotate_foreign_run_artifacts()
            self.assertTrue((out / "metrics.jsonl").exists())
            self.assertFalse((out / "previous-same").exists())

    def test_no_prior(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            sup = self._sup(out, "fresh")
            sup._rotate_foreign_run_artifacts()
            self.assertFalse((out / "previous-fresh").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)