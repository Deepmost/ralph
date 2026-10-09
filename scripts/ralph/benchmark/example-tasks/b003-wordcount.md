---
id: "b003"
title: "实现 word_count 并处理标点边界"
priority: 3
depends_on: ["b001"]
---

## 背景

验证 Agent 处理**边界与异常场景**的能力：一个看似简单、但标点/空白处理容易出错的函数。

## 需求描述

在 `scripts/ralph/benchmark/sandbox/textkit.py` 中新增 `word_count(text: str) -> dict[str, int]`：

- 返回每个单词出现的次数（单词 = 连续的字母/数字字符序列）
- 大小写不敏感（统一转小写）
- 标点、换行、制表符均作为分隔符
- 空串或纯标点返回空字典 `{}`

补充单元测试。

## 技术设计

- 用正则 `[a-z0-9]+` 在转小写后的文本上做 `findall`
- 用 `collections.Counter` 聚合，返回普通 `dict`

## 实现约束

- 不得修改 `slugify` / `truncate` 的行为
- 不引入第三方依赖

## 验收标准

- [ ] `python3 -m unittest discover -s scripts/ralph/benchmark/sandbox -p "test_*.py"` 全部通过
- [ ] `word_count("Hello, hello! WORLD.")` 返回 `{"hello": 2, "world": 1}`
- [ ] 覆盖：空串、纯标点、多行、连续分隔符、大小写混合
- [ ] b001/b002 既有测试保持通过
