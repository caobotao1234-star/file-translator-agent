---
name: Word 翻译
trigger: doc_type == "Word"
description: Word 文档翻译的专业经验
---

# Word 翻译经验

## 段内格式（最重要）
get_page_content 返回的数据中，有些段落有 `has_mixed_format: true` 和 `runs` 数组。
这表示段落内有不同格式的文字片段（如某个词加粗、某个词斜体、某个词红色）。

你翻译时必须注意这些格式，在译文中用 `<r0>...</r0><r1>...</r1>` 标记对应保留。

示例：
原文 runs: [{"text": "海尔", "bold": true}, {"text": "的空气系统能够", "bold": null}, {"text": "智能调节", "bold": true}]
你的译文应该是: `<r0>Haier</r0><r1>'s air system can </r1><r2>intelligently adjust</r2>`

规则：
- 标记编号必须与 runs 数组的索引对应
- 加粗的词翻译后仍然要在对应的标记里
- 如果段落没有 has_mixed_format，直接翻译纯文本即可

## Word 文档结构
- Word 没有"页"的概念，所有段落是线性的
- get_page_content(page_index=0) 返回前 30 段
- 表格的 key 格式：t_{table}_{row}_{col}
- 段落的 key 格式：p_{index}

## 翻译策略
- 一次获取所有内容
- 有 has_mixed_format 的段落，翻译时带上 `<rN>` 标记
- 没有 has_mixed_format 的段落，直接翻译纯文本
- 翻译完成后用 inspect_output 检查格式是否正确
