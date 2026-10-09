<!-- 由 ab_experiment.py 生成，可提交归档 -->
# Ralph A/B 对照实验报告（归档）

- 日期：2026-10-09 12:31
- 命令：`python3 scripts/ralph/ab_experiment.py --run --configs A,B,C,D --max-iterations 20`
- 任务集：`scripts/ralph/benchmark/example-tasks/`（首次为 b001/b002/b003；现已扩充至 b001–b007）
- 模型：opencode-go / deepseek-v4.1-flash
- 归档证据：`scripts/ralph/benchmark/runs/`（已 gitignore）

> ⚠️ 本轮任务集过于简单，四组均为 100%，**无区分度**；b006/b007 等负例任务是在本轮之后补充的，
> 需用扩充后的任务集（`--repeats 3`）重跑才能得到有对照意义的结论。

# Ralph A/B 对照实验结果

| 配置 | run-id | 任务总数 | 完成 | 完成率 | 首次通过率 | 阻塞率 | 越权拦截 | 验证命令 | PM 调整 |
|---|---|---|---|---|---|---|---|---|---|
| A | ralph-A-20261009-110142 | 3 | 3 | 100.0% | 100.0% | 0.0% | 0 | 7 | {} |
| B | ralph-B-20261009-110634 | 3 | 3 | 100.0% | 100.0% | 0.0% | 0 | 5 | {} |
| C | ralph-C-20261009-110834 | 3 | 3 | 100.0% | 100.0% | 0.0% | 0 | 8 | {} |
| D | ralph-D-20261009-111146 | 3 | 3 | 100.0% | 100.0% | 0.0% | 0 | 7 | {} |

## 相对基线 A 的差值

| 配置 | Δ完成率 | Δ首次通过率 | Δ阻塞率 |
|---|---|---|---|
| B | 0.0 | 0.0 | 0.0 |
| C | 0.0 | 0.0 | 0.0 |
| D | 0.0 | 0.0 | 0.0 |

> 口径见 `docs/agent-eval.md`；成本/延迟可结合 `eval_langfuse.py --release <run-id>` 获取。
