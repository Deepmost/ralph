# Ralph Benchmark（对照实验任务集）

用于验证 Ralph 多 Agent 闭环（Developer / Validator / PM）与越权防腐的**增益**。

## 评测目标

通过 A/B 对照回答：**多 Agent 闭环相比单 Agent，是否在完成率 / 首次通过率 / 稳定性上更优？**（口径见 `docs/agent-eval.md`）

## 四组配置

| 组 | 含义 | Ralph 参数 |
|---|---|---|
| A | 完整闭环（基线） | 无 |
| B | 仅 Developer（无验证、无治理） | `--no-validator --no-pm` |
| C | Developer + Validator（无 PM） | `--no-pm` |
| D | 完整闭环但关闭越权回滚 | `--no-guard` |

每组用不同的 `--run-id`（即 Langfuse `release` 标签），便于在 Langfuse 与 `eval_langfuse.py` 中分组对比。

## 任务集设计原则

对齐 `docs/agent-eval.md` 的样本分类，一个合格的 benchmark 应覆盖：

- **正常成功样本**：中等复杂度、可确定性验收
- **困难样本**：多文件、多步骤、隐式约定
- **负例样本**：任务本身不该被完成（验证 Agent 会拒绝 / 打回）
- **工具失败样本**：故意依赖不存在的接口（考察错误恢复）
- **安全边界样本**：诱导改动受保护文件（考察越权防腐）

每个任务必须带**可确定性验收**（测试 / typecheck / 断言），优先用代码 grader，少用 LLM-judge。

## 示例任务集

`example-tasks/` 提供一组自包含、**可确定性验收**的任务（在 `benchmark/sandbox/` 下产出代码），
并覆盖评测文档要求的样本分类：

| 任务 | 分类 | 考察点 | 预期差异 |
|---|---|---|---|
| b001 slugify | 正常成功 | 基础编码 + 边界 | 各配置应都通过 |
| b002 truncate | 正常成功 | 增量扩展 + 回归 | 各配置应都通过 |
| b003 word_count | 正常成功 | 异常/边界处理 | 各配置应都通过 |
| b004 CLI | **困难/多文件** | 多文件集成 | 单 Agent 更易遗漏集成测试 |
| b005 Unicode 重构 | **回归约束** | 严格边界 + 不破坏既有测试 | 无验证组更易引入回归 |
| b006 缺失依赖 | **工具/环境失败** | 依赖缺失时如实报告 | 无验证组更易伪造实现 |
| b007 不可完成 | **负例** | 验证严格性 | **有验证组应 blocked；无验证组易伪造成完成** |

其中 b006/b007 是**判分关键**：它们决定了「完成率高」到底是真的完成，还是没被发现的问题。

任务格式即 Ralph 任务文件格式（YAML frontmatter + 背景/需求/技术设计/实现约束/验收标准）。

验收命令示例：

```bash
python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"
```

## 多次重复与 pass^k

单次运行的差异会被 LLM 随机性淹没。建议每组重复 N 次：

```bash
python3 scripts/ralph/ab_experiment.py --run --repeats 3 --configs A,B,C,D
```

报告会用 `pass@k`（至少一次成功）与 `pass^k`（k 次全部成功）区分「能力」与「可靠性」。

## 运行对照实验

```bash
# 1) 预演：只打印将要执行的命令，不改动任何文件
python3 scripts/ralph/ab_experiment.py --plan

# 2) 实跑（会临时接管 scripts/ralph/tasks/ 等运行时文件，结束后自动恢复）
python3 scripts/ralph/ab_experiment.py --run --configs A,B,C,D --max-iterations 30

# 3) 汇总报告（读取每次运行归档到 runs/ 的结果）
python3 scripts/ralph/ab_experiment.py --report
```

> ⚠️ 实跑会真实调用模型、消耗 token，并修改工作区。脚本会对运行时文件做备份/恢复，但仍建议在干净的 git 状态下运行。

## 产出

- `runs/<run-id>/`：每次运行的归档（`tasks/done`、`adjustments.json`、`progress.txt`、运行日志、指标 JSON）
- 终端输出的 Markdown 对照表（完成率 / 首次通过率 / 阻塞率 / 越权拦截 / 成本）
