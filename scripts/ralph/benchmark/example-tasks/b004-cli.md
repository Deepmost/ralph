---
id: "b004"
title: "为 textkit 增加命令行工具（多文件集成）"
priority: 4
depends_on: ["b001", "b002", "b003"]
---

## 背景

沙盒需要一个可执行的 CLI，串联已有的文本处理函数。本任务考察 Agent 的**多文件集成**能力。

## 需求描述

- 新增 `scripts/ralph/benchmark/sandbox/textkit_cli.py`
- 支持子命令：`slugify / truncate / wordcount`
- 新增集成测试 `test_textkit_cli.py`，用 `subprocess` 实际执行 CLI 并断言输出

## 技术设计

- 使用标准库 `argparse`，不得引入第三方依赖
- CLI 可被 `python3 scripts/ralph/benchmark/sandbox/textkit_cli.py <sub> <arg>` 直接执行

## 实现约束

- **不得修改** `textkit.py` 中既有函数的签名与行为
- 只新增文件，不改动沙盒以外内容

## 验收标准

- [ ] `python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"` 全部通过
- [ ] `python3 scripts/ralph/benchmark/sandbox/textkit_cli.py slugify "Hello World"` 输出 `hello-world`
- [ ] 集成测试覆盖三个子命令各至少 1 条断言
