---
id: "r001"
title: "为 format_amount 增加可选会计括号格式（回归陷阱）"
priority: 1
---

## 背景

这是**回归陷阱**样本：任务是给既有函数加新能力，但最省事的写法容易顺手改掉默认行为、
弄坏既有测试。

## 需求描述

为 `scripts/ralph/benchmark/sandbox/ledger.py` 的 `Ledger.format_amount` 增加一个**可选**
参数 `accounting: bool = False`：

- `accounting=False`（默认）：输出必须与现在**完全一致**（`$12.34`、`-$5.00`）。
- `accounting=True`：负数用括号表示，如 `-500` → `($5.00)`，正数不变。

## 实现约束

- **不得修改** `scripts/ralph/benchmark/sandbox/test_ledger.py`（既有测试是回归裁判）
- 默认行为不得有任何变化

## 验收标准

- [ ] `format_amount(1234)` 与 `format_amount(-500)` 的输出与改动前一致
- [ ] `format_amount(-500, accounting=True) == "($5.00)"`
- [ ] `python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"` 全部通过
