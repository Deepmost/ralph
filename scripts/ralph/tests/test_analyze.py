#!/usr/bin/env python3
"""
长跑评测报告器单元测试（确定性，不联网、不调 LLM）。

覆盖：
  - master.log 行解析：parse_master_lines
  - 心跳断档：heartbeat_gaps
  - 时间转换：_to_utc_z / _shift_minutes
  - 任务终态：scan_tasks
  - 资源趋势：resource_trend
  - 阈值判定：evaluate_thresholds / overall_verdict

运行：
  python3 scripts/ralph/tests/test_analyze.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "soak"))
import analyze  # noqa: E402


class TestParseMasterLines(unittest.TestCase):
    def test_counts_and_cost(self):
        lines = [
            "2026-10-10T10:00:00+08:00 [INFO] 启动子进程: python3 ralph.py pi",
            "2026-10-10T10:00:01+08:00 [child]   迭代 3/60 - 开发 Agent",
            "2026-10-10T10:00:02+08:00 [child] [Turn] 费用: $0.001",
            "2026-10-10T10:00:03+08:00 [child] [Turn] 费用: $9.6e-05",
            "2026-10-10T10:00:04+08:00 [child]   开发 Agent 连续两次失败 (模型无响应)，脚本终止！",
            "2026-10-10T10:00:05+08:00 [child]   总时长超时! 已运行 1800 秒",
            "2026-10-10T10:00:06+08:00 [INFO] PM 改动越界（x），tasks 从备份回滚",
        ]
        s = analyze.parse_master_lines(lines)
        self.assertEqual(s["child_starts"], 1)
        self.assertEqual(s["double_failures"], 1)
        self.assertEqual(s["guard_rollbacks"], 1)
        self.assertEqual(s["timeouts_total"], 1)
        self.assertEqual(s["agent_idle_failures"], 1)
        self.assertEqual(s["iterations_from_log"], 3)
        self.assertAlmostEqual(s["cost_from_log"], 0.001096, places=6)

    def test_empty(self):
        s = analyze.parse_master_lines([])
        self.assertEqual(s["child_starts"], 0)
        self.assertEqual(s["cost_from_log"], 0.0)


class TestHeartbeatGaps(unittest.TestCase):
    def test_regular(self):
        hbs = [{"ts": f"2026-10-10T10:0{i}:00+00:00"} for i in range(5)]
        g = analyze.heartbeat_gaps(hbs)
        self.assertEqual(g["heartbeats"], 5)
        self.assertAlmostEqual(g["max_gap_s"], 60.0, places=1)
        self.assertAlmostEqual(g["uptime_pct"], 100.0, places=1)

    def test_gap_detected(self):
        hbs = [
            {"ts": "2026-10-10T10:00:00+00:00"},
            {"ts": "2026-10-10T10:01:00+00:00"},
            {"ts": "2026-10-10T10:11:00+00:00"},  # 10 分钟断档
            {"ts": "2026-10-10T10:12:00+00:00"},
        ]
        g = analyze.heartbeat_gaps(hbs)
        self.assertAlmostEqual(g["max_gap_s"], 600.0, places=1)
        self.assertGreater(g["downtime_s"], 0)
        self.assertLess(g["uptime_pct"], 100.0)

    def test_single(self):
        g = analyze.heartbeat_gaps([{"ts": "2026-10-10T10:00:00+00:00"}])
        self.assertEqual(g["heartbeats"], 1)
        self.assertEqual(g["max_gap_s"], 0.0)


class TestTimeConversion(unittest.TestCase):
    def test_to_utc_z(self):
        self.assertEqual(analyze._to_utc_z("2026-10-10T18:00:00+08:00"),
                         "2026-10-10T10:00:00Z")
        self.assertEqual(analyze._to_utc_z("2026-10-10T10:00:00Z"),
                         "2026-10-10T10:00:00Z")
        self.assertEqual(analyze._to_utc_z("bad"), "bad")

    def test_shift_minutes(self):
        self.assertEqual(analyze._shift_minutes("2026-10-10T10:00:00Z", -5),
                         "2026-10-10T09:55:00Z")
        self.assertEqual(analyze._shift_minutes("2026-10-10T10:00:00Z", 5),
                         "2026-10-10T10:05:00Z")
        self.assertEqual(analyze._shift_minutes("bad", 5), "bad")


class TestScanTasks(unittest.TestCase):
    def test_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tasks = root / "tasks"
            done = tasks / "done"
            done.mkdir(parents=True)
            (tasks / "003-c.md").write_text(
                '---\nid: "003"\ntitle: "c"\nblocked: true\n---\n', encoding="utf-8")
            (done / "001-a.md").write_text(
                '---\nid: "001"\ntitle: "a"\nretryCount: 0\n---\n', encoding="utf-8")
            (done / "002-b.md").write_text(
                '---\nid: "002"\ntitle: "b"\nretryCount: 2\n---\n', encoding="utf-8")
            m = analyze.scan_tasks(tasks, done)
            self.assertEqual(m["total"], 3)
            self.assertEqual(m["done_ok"], 2)
            self.assertEqual(m["blocked"], 1)
            self.assertEqual(m["completion_rate"], round(2 / 3 * 100, 1))
            self.assertEqual(m["first_pass_rate"], 50.0)
            self.assertEqual(m["retry_distribution"], {"0": 2, "2": 1})


class TestResourceTrend(unittest.TestCase):
    def test_slope(self):
        hbs = [
            {"ts": "2026-10-10T10:00:00+00:00", "peak_rss_mb": 100.0},
            {"ts": "2026-10-10T11:00:00+00:00", "peak_rss_mb": 200.0},
            {"ts": "2026-10-10T12:00:00+00:00", "peak_rss_mb": 300.0},
        ]
        r = analyze.resource_trend(hbs)
        self.assertEqual(r["peak_rss_mb"], 300.0)
        self.assertAlmostEqual(r["rss_slope_mb_per_h"], 100.0, places=1)

    def test_empty(self):
        r = analyze.resource_trend([])
        self.assertEqual(r["peak_rss_mb"], 0.0)


class TestFilterHeartbeats(unittest.TestCase):
    def test_filters_by_run_id(self):
        rows = [{"run_id": "a", "ts": "1"}, {"run_id": "b", "ts": "2"},
                {"ts": "3"}]  # 无 run_id 的旧格式保留
        got = analyze.filter_heartbeats(rows, "a")
        self.assertEqual(len(got), 2)
        self.assertTrue(all(h.get("run_id") in ("a", None) for h in got))

    def test_no_run_id_keeps_all(self):
        rows = [{"run_id": "a"}, {"run_id": "b"}]
        self.assertEqual(len(analyze.filter_heartbeats(rows, None)), 2)


class TestLocalParsers(unittest.TestCase):
    def test_tool_stats(self):
        lines = [
            "2026-10-10T10:00:00+00:00 [child] [Tool] bash: python3 -m unittest discover",
            "2026-10-10T10:00:01+00:00 [child] [Tool] read: x.py",
            "2026-10-10T10:00:02+00:00 [child] [Tool Error] edit",
            "2026-10-10T10:00:03+00:00 [child] [Tool] bash: ls",
        ]
        s = analyze.parse_tool_stats(lines)
        self.assertEqual(s["tool_calls"], {"bash": 2, "read": 1})
        self.assertEqual(s["total_tool_calls"], 3)
        self.assertEqual(s["tool_errors"], 1)
        self.assertFalse(s["used_write_or_edit"])

    def test_evidence_stats(self):
        lines = [
            "2026 [child] [Tool] bash: python3 -m unittest discover -s tests",
            "2026 [child] [Tool] read: a.py",
        ]
        e = analyze.parse_evidence_stats(lines)
        self.assertEqual(e["verification_commands"], 1)
        self.assertTrue(e["has_verification"])

    def test_guard_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "adjustments.json"
            p.write_text("[]\n", encoding="utf-8")
            self.assertEqual(
                analyze.parse_guard_corruption(["x [child] 无 tasks 备份可用，无法回滚"], p), 1)
            p.write_text("{bad", encoding="utf-8")
            self.assertEqual(analyze.parse_guard_corruption([], p), 1)
            p.write_text("[]", encoding="utf-8")
            self.assertEqual(analyze.parse_guard_corruption([], p), 0)

    def test_adjustments(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "adjustments.json"
            p.write_text(json.dumps([
                {"action": "reorder"}, {"action": "split"},
                {"action": "reset"}, {"action": "reset"},
            ]), encoding="utf-8")
            a = analyze.parse_adjustments(p)
            self.assertEqual(a["pm_adjustments_total"], 4)
            self.assertEqual(a["pm_resets"], 2)
            self.assertEqual(a["pm_splits"], 1)
            self.assertEqual(a["pm_adjustments"]["reorder"], 1)

    def test_phase_timings_ignores_supervisor_lines(self):
        lines = [
            "2026-10-10T10:00:00+00:00 [child]   迭代 1/3 - 开发 Agent",
            "2026-10-10T10:00:10+00:00 [child]   迭代 1 - Validator 验证",
            "2026-10-10T10:00:20+00:00 [child]   迭代 1 - PM Agent 治理（沉淀 + 规划）",
            "2026-10-10T10:00:30+00:00 [child]   规划完成",
            "2026-10-10T10:09:00+00:00 [INFO] 评测报告已生成",  # 守护器行，应被忽略
        ]
        p = analyze.parse_phase_timings(lines)
        self.assertEqual(p["phase_developer_s_p50"], 10.0)   # 10:00:00 -> 10:00:10
        self.assertEqual(p["phase_validator_s_p50"], 10.0)   # 10:00:10 -> 10:00:20
        self.assertEqual(p["phase_pm_s_p50"], 10.0)          # 10:00:20 -> 10:00:30

    def test_iter_distribution(self):
        lines = [
            "2026-10-10T10:00:00+00:00 [child]   迭代 1/3 - 开发 Agent",
            "2026-10-10T10:01:00+00:00 [child]   迭代 2/3 - 开发 Agent",
            "2026-10-10T10:03:00+00:00 [child]   迭代 3/3 - 开发 Agent",
        ]
        p = analyze.parse_phase_timings(lines)
        self.assertEqual(p["iter_seconds_n"], 2)
        self.assertEqual(p["iter_seconds_p50"], 90.0)  # median(60, 120)

    def test_trend_series(self):
        hbs = [
            {"ts": "2026-10-10T10:00:00+00:00", "cost_total": 0.0, "iteration": 0,
             "sizes": {"progress_bytes": 0, "state_json_bytes": 0}},
            {"ts": "2026-10-10T11:00:00+00:00", "cost_total": 1.0, "iteration": 10,
             "sizes": {"progress_bytes": 1000, "state_json_bytes": 2000}},
        ]
        t = analyze.trend_series(hbs)
        self.assertAlmostEqual(t["cost_slope_usd_per_h"], 1.0)
        self.assertAlmostEqual(t["progress_slope_bytes_per_h"], 1000.0)
        self.assertAlmostEqual(t["state_slope_bytes_per_h"], 2000.0)
        self.assertAlmostEqual(t["cost_per_iteration_trend"], 0.1)


class TestThresholds(unittest.TestCase):
    TH = [("iterations", ">=", 150, "迭代数"),
          ("cost_total", "<", 20.0, "成本"),
          ("overclaim_rate", "<=", 0.0, "伪完成率")]

    def test_eval(self):
        checks = analyze.evaluate_thresholds(
            {"iterations": 200, "cost_total": 5.0, "overclaim_rate": None}, self.TH)
        self.assertTrue(checks[0]["ok"])
        self.assertTrue(checks[1]["ok"])
        self.assertIsNone(checks[2]["ok"])

    def test_verdict_pass(self):
        checks = analyze.evaluate_thresholds(
            {"iterations": 200, "cost_total": 5.0, "overclaim_rate": 0.0}, self.TH)
        self.assertEqual(analyze.overall_verdict(checks), "PASS")

    def test_verdict_conditional_on_cost(self):
        checks = analyze.evaluate_thresholds(
            {"iterations": 200, "cost_total": 50.0, "overclaim_rate": 0.0}, self.TH)
        self.assertEqual(analyze.overall_verdict(checks), "CONDITIONAL PASS")

    def test_verdict_fail_on_hard(self):
        th = [("guard_corruption", "<=", 0, "损坏")]
        checks = analyze.evaluate_thresholds({"guard_corruption": 1}, th)
        self.assertEqual(analyze.overall_verdict(checks), "FAIL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
