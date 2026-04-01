---
name: PDF 翻译
trigger: doc_type == "PDF"
description: 普通 PDF 文档翻译的专业经验
---

# PDF 翻译经验

## PDF 特点
- PDF 的文本是按块（block）提取的，每个块有精确的坐标（bbox）
- key 格式：pg{page}_e{element}
- 有字号、字体、颜色等格式信息

## 翻译策略
- 用 get_page_content(page_range=[...]) 批量获取
- 一次性翻译，减少 API 调用
- PDF 的格式标记规则与 Word 相同

## 注意事项
- PDF 翻译后输出为新 PDF，排版可能与原文有差异
- 如果 parse_document 返回 warnings（文本很少），可能是扫描件，先问用户
