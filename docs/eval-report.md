# Ralph 项目评测报告

- 生成时间：2026-10-09 11:35:11
- 可观测窗口：`2026-10-06T03:32:05Z` ~ `2026-10-09T03:32:05Z`
- 过滤条件：`{'environment': 'ralph'}`
- 口径定义：见 [`docs/agent-eval.md`](agent-eval.md)

本报告由 `scripts/ralph/evaluate.py` 聚合四个评测面生成。

## ① 确定性质量 / 安全（单元测试）

- 用例总数：18，通过 18，失败 0 — **✅ 通过**

## ② 在线可观测（Langfuse）

| 指标 | 值 |
|---|---|
| observation / trace / session | 501 / 27 / 27 |
| 类型分布 | {'GENERATION': 212, 'TOOL': 262, 'SPAN': 27} |
| 错误率 | 13 / 2.6% |
| LLM 调用 | 212 |
| 延迟 p50/p95/p99 (s) | 3.294 / 9.402 / 16.152 |
| 工具调用 / 失败率 | 262 / 2.7% |
| 工具分布 | {'bash': 166, 'read': 56, 'edit': 30, 'write': 10} |
| 角色分布 | {'developer': 293, 'validator': 107, 'pm': 101} |

## ③ 对照实验（A/B）

# Ralph A/B 对照实验结果

| 配置 | run-id | 任务总数 | 完成 | 完成率 | 首次通过率 | 阻塞率 | 越权拦截 | PM 调整 |
|---|---|---|---|---|---|---|---|---|
| A | ralph-A-20261009-110142 | 3 | 3 | 100.0% | 100.0% | 0.0% | 0 | {} |
| B | ralph-B-20261009-110634 | 3 | 3 | 100.0% | 100.0% | 0.0% | 0 | {} |
| C | ralph-C-20261009-110834 | 3 | 3 | 100.0% | 100.0% | 0.0% | 0 | {} |
| D | ralph-D-20261009-111146 | 3 | 3 | 100.0% | 100.0% | 0.0% | 0 | {} |

## 相对基线 A 的差值

| 配置 | Δ完成率 | Δ首次通过率 | Δ阻塞率 |
|---|---|---|---|
| B | 0.0 | 0.0 | 0.0 |
| C | 0.0 | 0.0 | 0.0 |
| D | 0.0 | 0.0 | 0.0 |

> 口径见 `docs/agent-eval.md`；成本/延迟可结合 `eval_langfuse.py --release <run-id>` 获取。

---

## 评测面索引

| 面 | 载体 | 命令 |
|---|---|---|
| ① 确定性质量/安全 | `tests/test_pm_guard.py` | `python3 scripts/ralph/tests/test_pm_guard.py` |
| ② 在线可观测 | `eval_langfuse.py` | `python3 scripts/ralph/eval_langfuse.py --environment ralph` |
| ③ 对照实验 | `ab_experiment.py` | `python3 scripts/ralph/ab_experiment.py --report` |
| ④ 口径定义 | `docs/agent-eval.md` | （文档） |
| **总入口** | `evaluate.py` | `python3 scripts/ralph/evaluate.py --out docs/eval-report.md` |