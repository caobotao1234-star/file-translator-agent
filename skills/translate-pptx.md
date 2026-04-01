---
name: PPT 翻译
trigger: doc_type == "PPT"
description: PPT 演示文稿翻译的专业经验
---

# PPT 翻译经验

## 段内格式
PPT 段落也可能有 `has_mixed_format: true` 和 `runs` 数组。
处理方式与 Word 相同：翻译时用 `<r0>...</r0><r1>...</r1>` 标记保留格式。

## 字号和溢出
- 翻译完成后用 enable_autofit 给幻灯片启用"缩小文字以适应"
- 用 compare_layout 对比原文和译文的布局差异
- 用 smart_resize 精确调整字号

## 批量翻译策略
- PPT 的 key 格式：s{slide}_sh{shape}_p{para}
- 用 get_page_content(page_range=[0,1,2,...]) 一次获取多页
- 一次性翻译所有文本

## 输出后检查
- write_document 后，用 inspect_output 抽查 2-3 页
