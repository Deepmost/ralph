# AGENTS.md

## 项目简介

Ralph —— 自主 AI Agent 循环执行器。基于任务文件（Markdown + YAML frontmatter）驱动，
通过 Developer / Validator / PM 三个 Agent 构成「开发 → 验证 → 治理」自治闭环。

## 技术栈与约定

- 语言：Python 3.10+，仅标准库（不引入第三方依赖）
- 平台：macOS / Linux
- 脚本位于 `scripts/ralph/`，测试位于 `scripts/ralph/tests/`
- 任务格式见 `readme.md`「任务文件格式」一节
- 质量要求：提交前需通过相关测试（如 `python3 -m unittest`）

## 目录

- `scripts/ralph/ralph.py`：核心引擎（循环调度、超时、PM 防腐回滚）
- `scripts/ralph/tests/`：单元测试（确定性，可进 CI）
- `scripts/ralph/benchmark/sandbox/`：对照实验的沙盒代码与测试
- `docs/agent-eval.md`：评测口径

## 编码规范

- 保持改动专注、最小化
- 遵循现有代码风格
- 不引入非必要的第三方依赖
