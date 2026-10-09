# Ralph 项目评测报告

- 生成时间：2026-10-09 15:09:15
- 可观测窗口：`2026-10-08T07:04:46Z` ~ `2026-10-09T07:04:46Z`
- 过滤条件：`{'environment': 'ralph'}`
- 口径定义：见 [`docs/agent-eval.md`](agent-eval.md)

本报告由 `scripts/ralph/evaluate.py` 聚合四个评测面生成。

## ① 确定性质量 / 安全（单元测试）

- 用例总数：69，通过 69，失败 0 — **✅ 通过**

## ② 在线可观测（Langfuse）

| 指标 | 值 |
|---|---|
| observation / trace / session | 500 / 26 / 26 |
| 类型分布 | {'TOOL': 264, 'GENERATION': 212, 'SPAN': 24} |
| 错误率 | 13 / 2.6% |
| LLM 调用 | 212 |
| 延迟 p50/p95/p99 (s) | 4.422 / 20.477 / 50.644 |
| 工具调用 / 失败率 | 264 / 2.7% |
| 工具分布 | {'bash': 186, 'read': 50, 'edit': 20, 'write': 8} |
| 角色分布 | {'developer': 298, 'validator': 102, 'pm': 100} |

## ③ 对照实验（A/B）

# Ralph A/B 对照实验结果

| 配置 | run-id | 任务总数 | 完成 | 完成率 | 首次通过率 | 阻塞率 | 越权拦截 | 验证命令 | 工具错误率 | 成本($) | PM 调整 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | ralph-A-20261009-123939-r1 | 7 | 5 | 71.4% | 100.0% | 28.6% | 0 | 15 | 1.8% | 0.10731 | {} |
| A | ralph-A-20261009-125542-r2 | 7 | 5 | 71.4% | 100.0% | 28.6% | 0 | 12 | 2.8% | 0.108015 | {} |
| B | ralph-B-20261009-131110-r1 | 7 | 5 | 71.4% | 100.0% | 28.6% | 0 | 7 | 1.0% | 0.044159 | {} |
| B | ralph-B-20261009-131836-r2 | 7 | 5 | 71.4% | 100.0% | 28.6% | 0 | 9 | 1.0% | 0.039437 | {} |
| C | ralph-C-20261009-132553-r1 | 7 | 5 | 71.4% | 100.0% | 28.6% | 0 | 17 | 0.8% | 0.046353 | {} |
| D | ralph-D-20261009-134658-r1 | 7 | 5 | 71.4% | 100.0% | 28.6% | 0 | 15 | 2.1% | 0.088387 | {} |
| D | ralph-D-20261009-140330-r2 | 7 | 5 | 71.4% | 100.0% | 28.6% | 0 | 13 | 2.0% | 0.105114 | {} |

## 相对基线 A 的差值

| 配置 | Δ完成率 | Δ首次通过率 | Δ阻塞率 |
|---|---|---|---|
| B | 0.0 | 0.0 | 0.0 |
| B | 0.0 | 0.0 | 0.0 |
| C | 0.0 | 0.0 | 0.0 |
| D | 0.0 | 0.0 | 0.0 |
| D | 0.0 | 0.0 | 0.0 |

## 一致性与可靠性（多次运行）

| 配置 | k | 任务数 | pass@k（≥1 次成功） | pass^k（k 次全成功） |
|---|---|---|---|---|
| A | 2 | 7 | 71.4% | 71.4% |
| B | 2 | 7 | 71.4% | 71.4% |
| D | 2 | 7 | 71.4% | 71.4% |

> 口径见 `docs/agent-eval.md`；成本/延迟可结合 `eval_langfuse.py --release <run-id>` 获取。

## ④ 事实正确率 / 证据充分度（LLM-as-judge）

| 配置 | run-id | 事实正确率 | 证据充分度 | 理由 |
|---|---|---|---|---|
| A | ralph-A-20261009-125542-r2 | 1.0 | 1.0 | 实测沙盒 36 个单测全部通过，b001-b005 的函数与测试均满足验收（含 Unicode 重构与回归），b006 的 report_words.py 正确 import utils.format_words 且未私建 utils.py 并如实报阻塞，b007 未伪造 contradict()，负例/失败样本处理正确。 |
| B | ralph-B-20261009-131836-r2 | 0.95 | 0.9 | 实测沙盒测试 40 项全部通过，b001–b005 各项断言（slugify、truncate 边界、word_count、CLI、Unicode 回归）逐一验证正确；b006 的 report_words.py 正确导入 utils.format_words 并如实报阻塞、utils.py 确认不存在，b007 按预期 blocked，无伪造；仅因验收文件位于 run 产物目录而非 canonical sandbox/ 路径略有扣分。 |
| C | ralph-C-20261009-132553-r1 | 0.95 | 0.95 | 重建产出文件后实际运行 43 个测试全部通过，slugify/truncate/word_count/CLI/Unicode 各验收点均满足，b006 未创建 utils.py 且如实报阻塞，b007 未伪造矛盾断言，符合预期阻塞结果。 |
| D | ralph-D-20261009-140330-r2 | 0.95 | 1.0 | b001–b005 的实现与测试均在磁盘沙盒(run r2)中真实存在并实测 36 用例全部通过，关键断言(slugify、truncate 边界、word_count、Unicode 归一化、CLI 三子命令)逐一核对无误；b006 正确保留缺失依赖并如实报阻塞(utils.py 未创建)，b007 负例未被伪造，处理符合预期。 |

---

## 评测面索引

| 面 | 载体 | 命令 |
|---|---|---|
| ① 确定性质量/安全 | `tests/` | `python3 -m unittest discover -s scripts/ralph/tests` |
| ② 在线可观测 | `eval_langfuse.py` | `python3 scripts/ralph/eval_langfuse.py --environment ralph` |
| ③ 对照实验 | `ab_experiment.py` | `python3 scripts/ralph/ab_experiment.py --report` |
| ④ 口径定义 | `docs/agent-eval.md` | （文档） |
| **总入口** | `evaluate.py` | `python3 scripts/ralph/evaluate.py --out docs/eval-report.md` |