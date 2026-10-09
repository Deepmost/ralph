#!/usr/bin/env python3
"""
评测指标模块单元测试（pass@k / pass^k / 证据层）。

运行：
  python3 scripts/ralph/tests/test_metrics.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metrics  # noqa: E402


class TestPassMetrics(unittest.TestCase):
    def test_pass_at_k_all_need_one_success(self):
        matrix = {
            "t1": [True, True, True],
            "t2": [False, True, False],   # 至少一次
            "t3": [False, False, False],  # 从未成功
        }
        self.assertEqual(metrics.pass_at_k(matrix), 66.7)

    def test_pass_power_k_needs_all(self):
        matrix = {
            "t1": [True, True, True],
            "t2": [False, True, False],
            "t3": [False, False, False],
        }
        self.assertEqual(metrics.pass_power_k(matrix), 33.3)

    def test_two_runs(self):
        matrix = {"a": [True, True], "b": [True, False]}
        self.assertEqual(metrics.pass_at_k(matrix), 100.0)
        self.assertEqual(metrics.pass_power_k(matrix), 50.0)

    def test_empty(self):
        self.assertEqual(metrics.pass_at_k({}), 0.0)
        self.assertEqual(metrics.pass_power_k({}), 0.0)

    def test_empty_sequence_is_not_success(self):
        self.assertEqual(metrics.pass_power_k({"x": []}), 0.0)

    def test_aggregate_outcomes(self):
        o1 = {"t1": True, "t2": False}
        o2 = {"t1": True, "t3": True}
        m = metrics.aggregate_outcomes([o1, o2])
        self.assertEqual(m["t1"], [True, True])
        self.assertEqual(m["t2"], [False, False])
        self.assertEqual(m["t3"], [False, True])


class TestTaskOutcomes(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp(prefix="ralph-metrics-"))
        (self.run / "tasks" / "done").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.run, ignore_errors=True)

    def _write(self, rel, tid, blocked=False):
        p = self.run / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        fm = [f'id: "{tid}"', f"blocked: {'true' if blocked else 'false'}"]
        p.write_text("---\n" + "\n".join(fm) + "\n---\n\nbody\n", encoding="utf-8")

    def test_done_is_success(self):
        self._write("tasks/done/a.md", "a")
        self.assertTrue(metrics.task_outcomes(self.run)["a"])

    def test_blocked_done_is_failure(self):
        self._write("tasks/done/b.md", "b", blocked=True)
        self.assertFalse(metrics.task_outcomes(self.run)["b"])

    def test_pending_is_failure(self):
        self._write("tasks/c.md", "c")
        self.assertFalse(metrics.task_outcomes(self.run)["c"])

    def test_compute_pass_metrics_single_run(self):
        self._write("tasks/done/a.md", "a")
        pm = metrics.compute_pass_metrics([self.run])
        self.assertEqual(pm["k"], 1)
        self.assertEqual(pm["pass_at_k"], 100.0)
        self.assertEqual(pm["pass_power_k"], 100.0)


class TestEvidenceMetrics(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp(prefix="ralph-evidence-"))

    def tearDown(self):
        shutil.rmtree(self.run, ignore_errors=True)

    def test_detects_verification(self):
        (self.run / "run.log").write_text(
            "[Tool] bash: python3 -m unittest discover -s sandbox\n"
            "[Tool] read: textkit.py\n"
            "[Tool] bash: ls -la\n",
            encoding="utf-8",
        )
        ev = metrics.evidence_metrics(self.run)
        self.assertEqual(ev["verification_commands"], 1)
        self.assertTrue(ev["has_verification"])

    def test_no_verification(self):
        (self.run / "run.log").write_text("[Tool] bash: ls\n", encoding="utf-8")
        ev = metrics.evidence_metrics(self.run)
        self.assertFalse(ev["has_verification"])

    def test_missing_log(self):
        ev = metrics.evidence_metrics(self.run)
        self.assertEqual(ev["verification_commands"], 0)


class TestCostMetrics(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp(prefix="ralph-cost-"))

    def tearDown(self):
        shutil.rmtree(self.run, ignore_errors=True)

    def test_scientific_notation(self):
        # pi 对极小费用会用科学计数法，必须正确解析（回归）
        (self.run / "run.log").write_text(
            "[Turn] 费用: $9.6288e-05\n[Turn] 费用: $0.00081108\n",
            encoding="utf-8")
        c = metrics.cost_metrics(self.run)
        self.assertAlmostEqual(c["cost_total"], round(9.6288e-05 + 0.00081108, 6), places=6)
        self.assertEqual(c["cost_turns"], 2)

    def test_no_cost(self):
        (self.run / "run.log").write_text("[Tool] read: a.py\n", encoding="utf-8")
        self.assertEqual(metrics.cost_metrics(self.run)["cost_total"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
