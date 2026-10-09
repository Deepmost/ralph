---
id: "b005"
title: "slugify 的 Unicode 重构（严格边界 + 回归）"
priority: 5
depends_on: ["b001"]
---

## 背景

考察 Agent 在**严格边界 + 既有测试回归**双重约束下能否安全重构。naive 实现容易破坏既有 ASCII 行为。

## 需求描述

重构 `scripts/ralph/benchmark/sandbox/textkit.py` 的 `slugify`：

- 保留既有 ASCII 行为不变（大小写、空白、特殊字符处理）
- 新增对 Unicode 字母/数字的支持：使用 `unicodedata.normalize("NFKD", text)` 去除组合音标后进行 ASCII 归一化
- 例：`slugify("Café Déjà Vu")` 返回 `"cafe-deja-vu"`
- 例：`slugify("Ünïcödé")` 返回 `"unicode"`

## 技术设计

- 先归一化再套用既有规则；组合字符（如 é → e + ́）需正确处理
- 边界：全组合字符、空串、纯符号

## 实现约束

- **b001 的既有 ASCII 测试必须继续通过**（这是本任务的回归门槛）
- 不得引入第三方依赖

## 验收标准

- [ ] `python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"` 全部通过（含 b001 原有用例）
- [ ] `slugify("Café Déjà Vu")` == `"cafe-deja-vu"`
- [ ] `slugify("Ünïcödé")` == `"unicode"`
- [ ] `slugify("Hello, World!")` 仍 == `"hello-world"`（回归）
