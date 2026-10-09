#!/usr/bin/env python3
"""
LLM-as-judge 纯函数单元测试（不调用模型）。

运行：
  python3 scripts/ralph/tests/test_judge.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import judge  # noqa: E402


class TestBuildJudgePrompt(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ralph-judge-"))
        self.tasks = self.root / "tasks"
        self.sandbox = self.root / "sandbox"
        self.tasks.mkdir()
        self.sandbox.mkdir()
        (self.tasks / "t1.md").write_text(
            "---\nid: \"t1\"\n---\n\n## 验收标准\n- [ ] 返回 hello\n", encoding="utf-8")
        (self.sandbox / "code.py").write_text("def f():\n    return 'hello'\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_prompt_contains_criteria_and_files(self):
        p = judge.build_judge_prompt(self.tasks, self.sandbox)
        self.assertIn("验收标准", p)
        self.assertIn("t1.md", p)
        self.assertIn("code.py", p)
        self.assertIn("factual_correctness", p)
        self.assertIn("groundedness", p)

    def test_empty_sandbox(self):
        empty = self.root / "empty"
        empty.mkdir()
        p = judge.build_judge_prompt(self.tasks, empty)
        self.assertIn("无产出文件", p)


class TestParseJudgeOutput(unittest.TestCase):
    def test_plain_json(self):
        r = judge.parse_judge_output('{"factual_correctness": 0.8, "groundedness": 0.5, "reason": "ok"}')
        self.assertEqual(r["factual_correctness"], 0.8)
        self.assertEqual(r["groundedness"], 0.5)
        self.assertEqual(r["reason"], "ok")

    def test_fenced_json(self):
        r = judge.parse_judge_output('说明...\n```json\n{"factual_correctness": 1, "groundedness": 1}\n```')
        self.assertEqual(r["factual_correctness"], 1.0)

    def test_extra_text_around(self):
        r = judge.parse_judge_output('结论如下 {"factual_correctness": 0.3, "groundedness": 0.9} 结束')
        self.assertEqual(r["factual_correctness"], 0.3)

    def test_clamps_out_of_range(self):
        r = judge.parse_judge_output('{"factual_correctness": 5, "groundedness": -2}')
        self.assertEqual(r["factual_correctness"], 1.0)
        self.assertEqual(r["groundedness"], 0.0)

    def test_invalid(self):
        self.assertIsNone(judge.parse_judge_output("no json here"))
        self.assertIsNone(judge.parse_judge_output(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
