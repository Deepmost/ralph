---
id: "b002"
title: "为 textkit 增加 truncate 函数"
priority: 2
depends_on: ["b001"]
---

## 背景

延续 b001 的沙盒模块，验证 Agent 能否在**已有代码基础上增量扩展**并保持既有测试通过（回归）。

## 需求描述

在 `scripts/ralph/benchmark/sandbox/textkit.py` 中新增 `truncate(text: str, limit: int, suffix: str = "...") -> str`：

- 当 `len(text) <= limit` 时原样返回
- 否则截断到 `limit` 长度并追加 `suffix`，**最终长度不超过 limit**
- `limit <= len(suffix)` 时返回 `text[:limit]`（不追加 suffix，避免超长）

补充单元测试。

## 技术设计

- 复用 b001 的文件，不要新建模块
- 边界处理需显式处理 `limit` 为 0 或负数的情况（返回空串）

## 实现约束

- 不得修改 `slugify` 的行为（b001 的验收须仍通过）
- 不引入第三方依赖

## 验收标准

- [ ] `python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"` 全部通过（含 b001 用例）
- [ ] `truncate("abcdef", 4)` 返回 `"a..."`，长度 4
- [ ] `truncate("ab", 4)` 返回 `"ab"`
- [ ] 覆盖 `limit=0`、负 `limit`、`limit <= len(suffix)` 场景
