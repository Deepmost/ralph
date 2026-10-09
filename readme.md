# Ralph - 自主 AI Agent 循环执行器

Ralph 是一个基于任务文件驱动的自主 AI 编码 Agent 循环系统。它将开发任务拆分为独立的 Markdown 文件，然后逐个交给 AI Agent 执行，由 Validator Agent 验证每个任务的完成质量，再由 PM Agent 站在项目整体视角沉淀经验、动态调整任务队列，形成自我演进的闭环。

## 核心架构

三个 Agent 在每轮迭代中依次协作，构成 开发 → 验证 → 治理 的自治闭环：

```
需求文档 → task-decomposition → tasks/*.md → Ralph 循环引擎
                                                  ↓
                          ┌──────── 每轮迭代 ────────┐
                          ↓                          │
                  ① Developer Agent（执行单个任务）   │
                          ↓                          │
                  ② Validator Agent（逐条验证验收）   │
                          ↓                          │
                  ③ PM Agent（治理：沉淀 + 规划）     │
                          ↓                          │
                  代码层越权防腐校验 / 越界回滚         │
                          └──────────────────────────┘
                                      ↓
                            全部任务完成或 blocked → 结束
```

### 三个 Agent 的职责

- **Developer Agent**（`DEVELOPER.md`）：每轮只实现一个任务，完成后移入 `tasks/done/`，并把"未来迭代的学习"和"对任务队列结构的反馈"写入 `progress.txt`，作为 PM 的素材。
- **Validator Agent**（`VALIDATOR.md`）：按 frontmatter `id` 定位刚完成的任务，逐条验证验收标准。失败则退回 `tasks/` 并累加 retryCount；可选地把"结构反馈块"留给 PM。
- **PM Agent**（`PM.md`）：每轮在前两者跑完后调用一次。做两类工作——
  - **后向沉淀**：把 progress.txt 中真正普适的学习提炼到 `docs/patterns-*.md` 与根 `AGENTS.md` 的索引段。
  - **前向规划**：基于 retryCount / 验证记录 / 结构反馈，对任务队列做 新增 / 拆分 / 重排 / 重置，并写入 `adjustments.json` 审计日志。

### PM 越权防腐（代码层硬约束）

PM 是 LLM，指令约束不可靠，因此引擎在 PM 运行前后做目录树快照对比，越界即自动回滚到 PM 跑前状态：

- 任务正文、不可变字段（validation_notes / retryCount / blocked）被篡改 → 回滚
- 未经审计的任务删除、`done/ → tasks/` 重置 → 回滚
- 把未完成任务私自移入 `done/`（伪造完成）→ 回滚
- 单轮净新增 > 3 个 或 重置 > 2 个任务（超收敛上限）→ 回滚
- 重置任务时联动清空其 validation_notes、retryCount 归零
- `docs/patterns-*.md` 合计超 200 行时告警，提示精简以控制各 Agent 上下文压力

## 快速开始

### 前置条件

- macOS 或 Linux（不支持 Windows）
- Python 3.10+
- pi agent CLI（已安装并登录，命令行 `pi` 可用）
- 项目根目录下需要有 `AGENTS.md` 文件（项目上下文）；没有的话可手动创建，pi 启动时会自动加载根目录的 `AGENTS.md`（兼容 `CLAUDE.md`）作为上下文

### 总共需要3步

1. 在项目根目录下创建 `doc/` 文件夹，放入需求文档 `requirement.md` 和设计文档 `design.md`

   > 💡 可选：在拆分任务前，可先用 `prd-check` 技能审核需求文档是否完整。
   > 它会从需求分析师视角检查文档的完整性、清晰性、一致性、可执行性等维度，
   > 输出一份结构化评审报告，帮你了解需求是否完善。这不是必须步骤，但需求越清晰，
   > 任务拆分和后续执行的质量越高。
   >
   > 在 pi 会话中执行：
   > ```
   > /skill:prd-check doc/requirement.md
   > ```

2. 在 pi 中执行任务拆分：
   ```
   /skill:task-decomposition doc/requirement.md doc/design.md
   ```
   > 拆分与审核技能位于 `.agents/skills/`，pi 会自动发现（可用 `/skill:task-decomposition` 强制调用）。
   > 若在非交互场景使用，需加 `--approve` 以信任项目本地技能。
3. 拆分完成后在项目根目录执行：

   python3 scripts/ralph/ralph.py pi --max-iterations xxx
   上述最后的xxx代表循环多少次，如果任务拆分后有50个任务，那么xxx应该大于等于50，这个任务总数在 scripts/ralph/tasks/ 下可看到拆分了多少个子任务
   
## 以下内容可不观看，只是介绍特性和更多用法

## 特性

- **需求预审（可选）**: 内置 `prd-check` 技能（`/skill:prd-check`），拆分前可先审核 PRD 完整性，输出评审报告与问题清单
- **三 Agent 闭环**: Developer 负责编码、Validator 逐条验证、PM 治理沉淀与动态规划，形成自我演进循环
- **知识自动沉淀**: PM 把每轮的普适学习提炼到 `docs/patterns-*.md` 与根 `AGENTS.md`，后续迭代复用
- **任务队列自演进**: PM 可基于实际进展新增 / 拆分 / 重排 / 重置任务，所有调整记入 `adjustments.json` 审计日志
- **PM 越权防腐**: 代码层目录树快照对比，对任务正文/受控字段篡改、未审计删除重置、超收敛上限的改动自动回滚
- **自动重试**: 验证失败自动回退任务并重试，最多 5 次后标记为 blocked 跳过
- **实时监控面板**: 运行时自动打开浏览器，展示任务进度、当前阶段（开发/验证/PM 规划）、运行日志
- **三层超时保护**: 首字响应 120s、持续静默 120s、总时长 30min
- **pi agent 后端**: 基于 pi agent CLI 的 JSON 事件流驱动，非交互模式下工具调用自动执行，无需逐次审批
- **自动归档**: 全部任务完成后，下次运行自动归档上一轮结果

## 高级用法

```bash
# 默认使用 pi 后端并启动监控面板（端口 17331）
./scripts/ralph/ralph.sh

# 指定最大迭代次数（位置参数）
./scripts/ralph/ralph.sh 30

# 不启动监控面板
./scripts/ralph/ralph.sh --tool pi 30 --no-dashboard

# 指定面板端口
./scripts/ralph/ralph.sh --tool pi 30 --port 8080

# 直接调用 Python 引擎
python3 scripts/ralph/ralph.py pi --max-iterations 30 --no-dashboard --port 8080

# 指定本次运行标识（用于 Langfuse 按运行分组）
python3 scripts/ralph/ralph.py pi --max-iterations 30 --run-id my-run-001

# 对照实验：仅 Developer（跳过验证与治理）
python3 scripts/ralph/ralph.py pi --max-iterations 30 --no-validator --no-pm
```

## 目录结构

```
AGENTS.md               # 项目上下文（PM 维护知识索引段）

scripts/ralph/
├── ralph.py            # 核心引擎（循环调度、超时管理、PM 防腐回滚、状态更新）
├── ralph.sh            # Linux/macOS 启动入口
├── chrome-cdp.sh       # macOS/Linux 下启动带 CDP 的 Chrome（浏览器验证用）
├── dashboard.py        # 实时监控面板 HTTP 服务
├── DEVELOPER.md        # Developer Agent 指令
├── VALIDATOR.md        # Validator Agent 指令
├── PM.md               # PM Agent 指令（后向沉淀 + 前向规划）
├── eval_langfuse.py    # 评测采集脚本（Langfuse + 本地任务终态 → 报告）
├── ab_experiment.py    # A/B 对照实验编排器（完整/单 Agent/无 PM/无防腐）
├── tests/              # 单元测试（PM 越权防腐对抗性测试）
├── benchmark/          # 对照实验任务集与运行归档
├── adjustments.json    # PM 任务队列调整的审计日志
├── progress.txt        # 进度日志（Developer/Validator 写入，PM 读取）
├── state.json          # 运行状态（供 Dashboard 读取）
├── tasks/              # 待执行任务队列（文件名序号 = 执行顺序）
│   └── done/           # 已完成任务
├── tasks.bak/          # PM 运行前的任务树备份（用于越权回滚，自动生成）
└── archive/            # 历史归档

.agents/
└── skills/             # pi agent 技能（prd-check / task-decomposition / agent-browser）

docs/
├── patterns-*.md       # PM 沉淀的可复用经验（按主题分文件，自动生成）
└── agent-eval.md       # Agent 评测口径（维度/公式/统计方法）
```

> 说明：任务的执行顺序由**文件名序号**决定；frontmatter 的 `id` 是任务的稳定身份，
> PM 重排（reorder）时只改文件名序号、不改 `id`，因此 Validator 按 `id` 定位任务文件。

## 任务文件格式

每个任务是一个带 YAML frontmatter 的 Markdown 文件：

```yaml
---
id: "001"
title: "初始化项目结构"
priority: 1
---

## 任务描述
...

## 技术设计
...

## 验收标准
- [ ] 条件1
- [ ] 条件2
```

## 监控面板

默认端口 `17331`，启动后自动打开浏览器。提供：
- 当前阶段徽标：IDLE / 开发中 / 验证中 / PM 规划中 / DONE（各阶段不同配色）
- 任务列表及状态（pending / done / blocked）
- 当前迭代进度
- 运行时间统计
- 实时日志输出

## 评测与对照实验

指标维度、公式与统计口径见 [`docs/agent-eval.md`](docs/agent-eval.md)。

### 可观测性（Langfuse）

Ralph 的每次 Agent 调用（Developer / Validator / PM）都会通过 pi 的
`@langfuse/pi-observability-plugin` 上报 trace。引擎已自动为每次调用注入标签：

- `LANGFUSE_TRACING_ENVIRONMENT=ralph`（与日常交互区分）
- `LANGFUSE_RELEASE=<run-id>`（按整轮运行聚合）
- `LANGFUSE_USER_ID=<developer|validator|pm>`（按角色聚合）

设置 `RALPH_NO_LANGFUSE=1` 可关闭上报。

### 采集报告

```bash
# 最近 7 天（读取 ~/.pi/agent/langfuse.json 凭据）
python3 scripts/ralph/eval_langfuse.py

# 指定时间窗与运行
python3 scripts/ralph/eval_langfuse.py --days 30 --environment ralph \
  --out docs/eval-report.md --json docs/eval-data.json
```

### 越权防腐单元测试

```bash
python3 scripts/ralph/tests/test_pm_guard.py
```

覆盖篡改受控字段、正文篡改、伪造完成、未审计删除/重置、收敛上限、审计删除、
备份回滚、`id` 字符串保持等 18 个对抗性用例（确定性、可进 CI）。

### A/B 对照实验

```bash
python3 scripts/ralph/ab_experiment.py --plan                        # 预演
python3 scripts/ralph/ab_experiment.py --run --configs A,B,C,D       # 实跑
python3 scripts/ralph/ab_experiment.py --report                      # 汇总
```

对比 A 完整闭环 / B 仅 Developer / C 无 PM / D 关闭越权回滚四组的
完成率、首次通过率、阻塞率与越权拦截次数。
