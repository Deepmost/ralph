---
id: "c001"
title: "新增 Ledger.statement()（多任务依赖链 第 1 步）"
priority: 2
---

## 背景

多任务长程依赖链的第 1 步：后续任务会依赖本步产出，用于考察任务排序与依赖传递。

## 需求描述

为 `scripts/ralph/benchmark/sandbox/ledger.py` 的 `Ledger` 新增方法：

```python
def statement(self) -> list[tuple[str, int]]:
    """按账户名升序返回 [(name, balance_cents), ...]。"""
```

## 实现约束

- 只使用标准库；不修改 `test_ledger.py`
- 返回顺序必须按账户名升序

## 验收标准

- [ ] `Ledger.statement()` 存在且返回 `list[tuple[str, int]]`
- [ ] 账户按名称升序排列
- [ ] `python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"` 通过
