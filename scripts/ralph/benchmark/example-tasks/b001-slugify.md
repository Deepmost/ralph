---
id: "b001"
title: "实现 slugify 工具函数"
priority: 1
---

## 背景

Benchmark 沙盒需要一个字符串工具模块，用于验证 Ralph 的编码 Agent 能否完成一个边界明确、可确定性验收的任务。

## 需求描述

在 `scripts/ralph/benchmark/sandbox/textkit.py` 中实现 `slugify(text: str) -> str`：

- 转为小写
- 连续空白替换为单个连字符 `-`
- 移除非「字母 / 数字 / 连字符」的字符
- 去除首尾连字符

同时编写单元测试 `scripts/ralph/benchmark/sandbox/test_textkit.py`。

## 技术设计

- 使用标准库 `re`，不引入第三方依赖
- 函数需有 docstring

## 实现约束

- 只新增上述两个文件，不修改沙盒以外的任何文件
- 不得使用 `str.isalnum()` 之外的非 ASCII 语义扩展（保持 ASCII 行为）

## 验收标准

- [ ] `python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"` 通过
- [ ] 测试覆盖：空字符串、纯空白、含特殊字符、首尾空白、连续空白
- [ ] `slugify("Hello, World!")` 返回 `"hello-world"`
