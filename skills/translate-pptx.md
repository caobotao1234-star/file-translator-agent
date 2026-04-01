---
name: PPT 翻译
trigger: doc_type == "PPT"
description: PPT 演示文稿翻译的专业经验
---

# PPT 翻译经验

## 格式标记保留
PPT 段落可能包含 `<r0>...</r0><r1>...</r1>` 格式标记，表示不同格式的文本片段（加粗、颜色、字号不同）。翻译时必须保留所有标记，只翻译标记内的文字。

示例：`<r0>关键：</r0><r1>说明文字</r1>` → `<r0>Key: </r0><r1>Description text</r1>`

## 字号和溢出
- 中文翻译成英文后，英文字符更窄但单词更长，文本框容易溢出
- 翻译完成后用 enable_autofit 给所有幻灯片启用 PowerPoint 原生的"缩小文字以适应"
- 如果某些关键页面效果不好，用 smart_resize 精确调整
- 用 compare_layout 对比原文和译文的布局差异，找出问题页面

## 批量翻译策略
- PPT 的 key 格式：s{slide}_sh{shape}_p{para}
- 用 get_page_content(page_range=[0,1,2,...]) 一次获取多页
- 一次性翻译所有文本，减少 API 调用

## 输出后检查
- write_document 后，用 inspect_output 抽查 2-3 页
- 重点关注：标题字号是否合适、正文是否溢出、表格是否对齐
