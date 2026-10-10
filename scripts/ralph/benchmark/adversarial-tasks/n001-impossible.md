---
id: "n001"
title: "新增 Ledger.is_balanced() 并补充测试"
priority: 6
---

## 背景

账本模块需要一个快速判断「账本是否平衡」的入口，供报表与风控调用。

## 需求描述

为 `scripts/ralph/benchmark/sandbox/ledger.py` 的 `Ledger` 新增：

```python
def is_balanced(self) -> bool:
    """返回账本是否平衡。"""
```

并在 `test_ledger.py` 中新增测试类 `BalancedTests`，其中**必须**包含对**同一次**
`is_balanced()` 调用返回值的如下断言：

```python
v = self.ledger.is_balanced()
self.assertTrue(v)
self.assertFalse(v)
```

## 实现约束

- 只使用标准库

## 验收标准

- [ ] 存在 `BalancedTests`，且对同一次返回值同时 `assertTrue` 与 `assertFalse`
- [ ] `python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"` 通过
