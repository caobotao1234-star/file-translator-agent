---
name: PDF 翻译
trigger: doc_type == "PDF"
description: 普通 PDF 文档翻译的专业经验
---

# PDF 翻译经验

## PDF 特点
- PDF 的文本是按块（block）提取的，每个块有精确的坐标（bbox）
- key 格式：pg{page}_b{block} 或 pg{page}_b{block}s{sub}
- 有字号、字体、颜色等格式信息
- PDF 可能包含图片（type=pdf_image），图片在翻译时自动保留在原位

## 图片处理
- parse_document 会报告 image_count（文档中的图片数量）
- get_page_content 返回的 pdf_image 类型项有位置和尺寸信息
- 图片在 write_document 时自动保留，不需要额外处理
- 如果图片里有需要翻译的文字（如图表标题），可以用 ask_user 确认是否需要处理

## 翻译策略
- 用 get_page_content(page_range=[...]) 批量获取
- 用 translate_page(keys=[...]) 翻译文本块
- pdf_image 类型的项不需要翻译，跳过即可
- write_document 会在原 PDF 上擦除原文、写入译文，图片自动保留

## 注意事项
- PDF 翻译后排版可能与原文有差异（字号自动缩放适配）
- 如果 parse_document 返回 warnings（文本很少），可能是扫描件，先问用户
