<!-- 由 ab_experiment.py 生成，可提交归档 -->
# Ralph A/B 对照实验报告（归档）

- 日期：2026-10-09 11:27
- 命令：`python3 scripts/ralph/ab_experiment.py --run --configs A,B,C,D --max-iterations 20`
- 任务集：`scripts/ralph/benchmark/example-tasks/`（b001/b002/b003）
- 模型：opencode-go / deepseek-v4.1-flash
- 归档证据：`scripts/ralph/benchmark/runs/`（已 gitignore）

> ⚠️ 结论：任务集过于简单，四组均为 100%，**无区分度**，不足以证明 Validator/PM 的增益。
> 需升级 benchmark（困难/负例/工具失败/安全边界样本）后重跑。

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
