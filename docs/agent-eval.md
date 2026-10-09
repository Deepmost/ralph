# Ralph Agent 评测口径

> 本文是整个 Ralph 项目的**评测总面（单一事实来源）**：既定义口径，也索引全部评测载体。
> 一键产出全项目评测报告：`python3 scripts/ralph/evaluate.py --out docs/eval-report.md`

## 全项目评测面

| 面 | 载体 | 产出 | 命令 |
|---|---|---|---|
| ① 确定性质量/安全 | `scripts/ralph/tests/test_pm_guard.py` | 越权防腐对抗性用例（可进 CI） | `python3 scripts/ralph/tests/test_pm_guard.py` |
| ② 在线可观测 | `scripts/ralph/eval_langfuse.py` | 调用/工具/延迟/错误指标 | `python3 scripts/ralph/eval_langfuse.py --environment ralph` |
| ③ 对照实验 | `scripts/ralph/ab_experiment.py` + `scripts/ralph/benchmark/` | A/B 对照报告 | `python3 scripts/ralph/ab_experiment.py --report` |
| ④ 口径定义（本文） | `docs/agent-eval.md` | 维度/公式/统计口径 | — |
| **聚合报告** | `docs/eval-report.md` | 全项目评测（自动生成） | `python3 scripts/ralph/evaluate.py --out docs/eval-report.md` |

> 各面相互独立、可单独运行；`evaluate.py` 只是把它们聚合成一份报告，不复制口径。

### 实现状态

| 能力 | 状态 | 载体 |
|---|---|---|
| 越权防腐对抗性测试 | ✅ | `tests/test_pm_guard.py` |
| 引擎核心逻辑测试 | ✅ | `tests/test_engine.py` |
| 指标函数测试（pass^k/证据） | ✅ | `tests/test_metrics.py` |
| trace 按运行/角色分组 | ✅ | `ralph.py` 注入 `LANGFUSE_*` |
| pass@k / pass^k | ✅ | `metrics.py` + `ab_experiment.py --repeats` |
| 验证证据（groundedness 代理） | ✅ | `metrics.evidence_metrics` |
| 事实正确率 / 证据充分度 | ✅ | `judge.py`（LLM-as-judge） |
| 工具错误率 / 工具分布 | ✅ | `metrics.tool_metrics` |
| 成本聚合（美元） | ✅ | `metrics.cost_metrics`（从 run.log 提取） |
| A/B 对照实验 | ✅ | `ab_experiment.py` |
| 工具选择正确率（期望工具集断言） | ⏳ 待补齐 | 需为任务定义期望工具集 |
| token 明细聚合 | ⏳ 待补齐 | Langfuse UI/Metrics/Scores |

## 0. 评测单元：一次 task run

Agent 评测的最小单元不是 prompt，而是**一次完整的 task run**：

```
task run = 任务/成功标准 + 初始状态 + Agent 配置 + 执行环境
         + 资源预算 + 轨迹(trace) + 终态 + 验证器 + 统计方法
```

判定成败用**终态**（测试是否通过、git diff、环境状态），而非最终文本。
评测器分三类，优先级从高到低：确定性 grader（单测/规则断言）→ LLM-as-judge（rubric）→ 人工复核（校准）。

---

## 1. 分层指标体系

| 层 | 维度 | 关键指标 | 计算口径 | 数据来源 |
|---|---|---|---|---|
| 任务级 | 任务成功率 | SR、pass@k、pass^k、进度率 | `成功任务/总任务`；`pass@k`=k 次至少一次通过；`pass^k`=k 次全通过 | `tasks/done/` frontmatter + Validator 结论 |
| 工具级 | 工具正确率 | 工具选择/参数正确率、轨迹匹配、工具失败率 | 期望工具集断言 + Trace 对比 | Langfuse `Tool: *` spans |
| 结果级 | 事实正确率 | Answer Accuracy、幻觉率 | LLM-judge（有 rubric/参考答案） | root span output |
| 结果级 | 证据充分度 | Groundedness、引用覆盖率 | 结论是否可追溯到 tool output / 文件 | root span output + Tool output |
| 轨迹级 | 过程可靠性 | 检查点通过、错误恢复、失败归因 | 偏序约束校验 + 分类 | 完整 trace |
| 系统级 | 性能稳定性 | 延迟 p50/p95/p99、超时率、异常率、一致性 | 聚合 latency/level；`pass^k`；轨迹相似度 | Langfuse observations |
| 安全级 | 安全合规性 | 约束违规率、越权拦截率、注入成功率 | 对抗场景断言 + 硬门槛 | PM 越权防腐 + 硬约束测试 |
| 成本级 | 效率成本 | token、$、turn 数、重试数、`cost/成功任务` | 聚合 cost/usage/turn | Langfuse generations + task frontmatter |
| 记忆级 | 记忆质量 | 该记的记住、不该写的不写、记忆污染 | LLM-judge / 人工抽查 | `docs/patterns-*.md`、`AGENTS.md`、`progress.txt` |
| 协作级 | 多 Agent 协作 | 委派正确性、分解质量、信息利用率、合并质量 | 见 §4 | `adjustments.json` + trace |

---

## 2. 逐维度定义

### 2.1 任务成功率（任务级）
- `SR = 成功任务数 / 总任务数`
- 多次运行要区分：
  - `pass@k` = k 次里**至少一次**成功（能力）
  - `pass^k` = k 次里**全部**成功（可靠性/一致性）
- 长任务用**进度率**：`完成的子目标 / 总子目标`（成功率是进度率=100% 的特例）
- Ralph 特有拆分：
  - `首次验证通过率 FPR = retryCount==0 的完成任务 / 完成任务`
  - `阻塞率 BR = blocked:true / 总数`

### 2.2 工具正确率（工具级）
- 细粒度：工具选择对不对 + 参数对不对。坏例类型：函数名错、缺必需参数、参数类型错、取值越界、幻觉参数
- 轨迹级：应调的工具是否调用、顺序是否正确（Trajectory Exact-Match / Inclusion）
- 工具失败 6 口径（ToolFailBench 思路，按需裁剪）：
  - Tool-Skip（该调未调）、Result-Ignore（无视返回）、Output-Fabrication（伪造输出）、Unnecessary-Use（不该调却调）
  - Clean Tool-Use Rate（干净使用率）、Control Accuracy（无需工具时不调）

### 2.3 事实正确率（结果级）
- `Answer Accuracy`：与参考答案/rubric 一致，通常 LLM-judge
- 反面对手：**幻觉率** = 无依据断言占比

### 2.4 证据充分度 / Groundedness（结果级）
- 结论是否引用了证据来源（tool 返回、文件、官方文档）
- 约束："先取证、后结论"——最终结论必须能追溯到某个 artifact

### 2.5 过程可靠性（轨迹级）
- 轨迹合理性：是否先收集证据、是否验证、是否明显绕路
- 失败归因：模型 / 提示 / 工具 / 环境 / 调度 / 评分器 / 任务
- 错误恢复：`child_error_detected` / `child_error_propagated` / `recovery_success`
- 状态一致性：是否重复执行、是否丢 checkpoint

### 2.6 性能稳定性（系统级）
- 延迟用 **p50/p95/p99**（不是均值）、超时率、异常率
- 一致性：同任务多跑，轨迹是否稳定（Tool Sequence Similarity、Divergence Point、Output Agreement）
- 鲁棒性：换措辞 / 换数据格式 / 注错（API 错误、超时）后的性能保持度
- 可预测性：置信度校准——知不知道自己在犯错、会不会该弃权就弃权

> 核心观点：**可靠性 ≠ 能力**。成功率 90% 不代表稳定、可复现、可信任。

### 2.7 安全合规性（安全级）
- 约束违规率、越权拦截率、注入成功率、策略遵守率
- **独立套件 + 硬门槛，不可被总分抵消**（"99% 安全但 1% 灾难"不给高分）
- Ralph 特有：**PM 越权防腐**——篡改受控字段、伪造完成、未审计删除/重置、超收敛上限 → 自动回滚
- 覆盖负例：prompt injection、tool output poisoning、越权、状态恢复、记忆污染

### 2.8 效率成本（成本级）
- token（输入/输出/缓存）、$、turn 数、工具调用数、重试数
- 关键指标：`cost / 成功任务`、`turns / 成功任务`

### 2.9 记忆质量（记忆级）
- 该记的记住（跨迭代复用）、不该写的不写（防污染）、旧任务不影响新任务
- 对本项目：PM 沉淀的 `patterns-*.md` 是否被后续任务复用（复用率）

### 2.10 多 Agent 协作（协作级）
- 委派正确性：任务是否需要委派、角色是否选对
- 任务分解质量：子任务是否覆盖目标、边界是否重叠、依赖是否明确
- 信息利用率：父 Agent 是否真的读取并使用子结果
- 合并质量：冲突是否被识别、最终产物是否满足全局约束
- 对 Ralph：Developer→Validator→PM 的信息是否有效传递、PM 调整是否产生正收益

---

## 3. 统计口径（决定报告是否可信）

1. **非确定性** → 每任务跑 N 次，报 `pass@k` 与 `pass^k`，不用单次。
2. **置信区间** → 二元结果用 **Wilson 区间**；跨任务用 **cluster bootstrap**（以任务为聚类单位，避免把同任务多次 trial 当独立样本）。
3. **非独立性** → 共享缓存/服务/数据库会让 trial 相关；**每 trial 重置状态**，否则只能叫"经验通过率"。
4. **阈值保护** → 报 p50/p95/p99；设告警阈值。
5. **不做单一总分** → 保留 **Pareto 前沿**（成功率 × 成本 × 延迟）。
6. **黄金路径陷阱** → 不写死唯一正确轨迹；用"检查点 + 偏序约束"（必须有权威来源、摘要必须在取证之后、必须引用…），多路径合理也应算过。

---

## 4. 数据来源与采集

| 数据 | 来源 | 采集方式 |
|---|---|---|
| Trace / LLM 调用 / 工具调用 | Langfuse（pi observability 插件） | `scripts/ralph/eval_langfuse.py` |
| 延迟 / TTFT / 错误 | Langfuse observations | 同上 |
| 任务终态 / retryCount / blocked | `tasks/` `tasks/done/` frontmatter | 同上（本地扫描） |
| 结果对错（编码） | 项目测试 / typecheck / lint | CI 或 Validator |
| PM 调整记录 | `adjustments.json` | 本地扫描 |
| 知识沉淀 | `docs/patterns-*.md`、`AGENTS.md`、`progress.txt` | 人工 / LLM-judge |
| 越权回滚 | 引擎运行日志 | 解析或回填为 Langfuse Score |

> Langfuse 列表接口 `/api/public/v2/observations` 返回 `latency / timeToFirstToken / level / sessionId / environment / userId / type / name`，**不含 token/cost 明细**；成本聚合以 Langfuse UI / Metrics / Scores 为准。

### 让 trace 可分组（需在 `ralph.py` 注入）
在每次 `pi` 子进程环境里设置：
- `LANGFUSE_TRACING_ENVIRONMENT=ralph`（与日常交互区分）
- `LANGFUSE_RELEASE=<run-id>`（按整轮运行聚合，A/B 实验换不同 run-id）
- `LANGFUSE_USER_ID=<developer|validator|pm>`（按角色聚合）

---

## 5. 分级落地

| 优先级 | 指标 | 是否可自动化 | 说明 |
|---|---|---|---|
| P0 | 首次验证通过率、工具错误率、延迟分位、越权拦截率 | 是（确定性） | 进 CI，可复现 |
| P1 | 任务成功率 pass^k、成本/成功任务、一致性 | 半自动 | 多次运行 + 聚合 |
| P2 | 事实正确率、证据充分度、记忆质量 | 需 judge | LLM-as-judge + 抽检 |

---

## 6. 运行方式

```bash
# 全项目评测（聚合四面的总入口，推荐）
python3 scripts/ralph/evaluate.py --out docs/eval-report.md

# 各面单独运行
python3 scripts/ralph/tests/test_pm_guard.py          # ① 确定性质量/安全

# 最近 7 天（默认）
python3 scripts/ralph/eval_langfuse.py

# 指定时间窗与环境标签
python3 scripts/ralph/eval_langfuse.py --days 30 --environment ralph

# 输出到文件
python3 scripts/ralph/eval_langfuse.py --out docs/eval-report.md --json docs/eval-data.json
```

凭据读取顺序：环境变量 `LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL` → `~/.pi/agent/langfuse.json`。

---

## 7. 参考来源

- Anthropic《Demystifying evals for AI agents》——三类 grader、非确定性、离线/在线
- *Towards a Science of AI Agent Reliability*（ICML 2026, arXiv:2602.16666）——consistency/robustness/predictability/safety
- NVIDIA NeMo / RAGAS Agentic Metrics——tool call accuracy、agent goal accuracy、trajectory evaluation
- Snowflake《Agent Evaluation》——Outcome / Trajectory / Reasoning / Safety
- IBM《什么是 AI 智能体评估》——函数调用指标、性能/伦理/交互/系统分类
- ToolFailBench（arXiv:2607.04686）——工具失败 6 口径
- TRAJECT-Bench（arXiv:2510.04550）——轨迹感知指标
- 掘金《万字长文详解 Agent 的评测机制》、腾讯云《Agent 质量评估》——八层指标、进度率、Wilson 区间/cluster bootstrap、多 Agent 五类指标
- 测试之家《Agent 评测到底测什么》——黄金路径陷阱、检查点约束
- LangSmith《How to evaluate agents》——Final / Single-step / Trajectory
