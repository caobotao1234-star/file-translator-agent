---
name: Word 翻译
trigger: doc_type == "Word"
description: Word 文档翻译的专业经验
---

# Word 翻译经验

## 格式标记保留（最重要）
Word 段落中不同格式的文字用 `<r0>...</r0><r1>...</r1>` 标记区分。
每个标记对应一个 Run（格式片段），可能有不同的加粗、斜体、字号、颜色。

翻译时必须：
1. 保留所有 `<rN>` 标记，数量和编号与原文一致
2. 只翻译标记内的文字
3. 不要合并或拆分标记

示例：
- 原文：`<r0>海尔</r0><r1>的空气系统能够</r1><r2>智能调节</r2><r1>室内温度</r1>`
- 译文：`<r0>Haier</r0><r1>'s air system can </r1><r2>intelligently adjust</r2><r1> indoor temperature</r1>`

如果标记被破坏，writer 会降级为整段替换，所有格式（加粗、颜色等）都会丢失。

## Word 文档结构
- Word 没有"页"的概念，所有段落是线性的
- get_page_content(page_index=0) 返回前 30 段，page_index=1 返回 30-60 段
- 表格的 key 格式：t_{table}_{row}_{col}
- 段落的 key 格式：p_{index}

## 翻译策略
- 一次获取所有内容：get_page_content(page_index=0)
- 如果段落数 > 30，分批获取
- 一次性翻译所有文本

## 输出后检查
- 用 inspect_output 检查段落格式（字号、字体、样式）
- 重点关注：格式标记是否正确保留、标题样式是否丢失
