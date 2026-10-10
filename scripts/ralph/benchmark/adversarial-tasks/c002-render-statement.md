---
id: "c002"
title: "基于 statement() 新增 render_statement()（多任务依赖链 第 2 步）"
priority: 3
depends_on: ["c001"]
---

## 背景

多任务长程依赖链的第 2 步，**依赖 c001**：没有 c001 的 `statement()` 就无法完成。

## 需求描述

为 `Ledger` 新增：

```python
def render_statement(self) -> str:
    """把 statement() 渲染为多行文本，每行形如 'alice: $12.34'，行间用 \n 连接。"""
```

金额使用 `self.format_amount(balance_cents)` 渲染。

## 实现约束

- 必须复用 `statement()`，不得另写排序逻辑
- 不修改 `test_ledger.py`

## 验收标准

- [ ] `render_statement()` 输出行形如 `alice: $12.34`
- [ ] 行序与 `statement()` 一致（账户名升序）
- [ ] `python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"` 通过
