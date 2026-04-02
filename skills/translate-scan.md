---
name: 扫描件翻译
trigger: doc_type == "scanned_PDF"
description: 扫描件 PDF 翻译的专业经验
---

# 扫描件翻译经验

## 扫描件特点
- 页面是图片，没有可提取的文本
- 需要 OCR 识别文字，CV 检测布局
- 你能看到页面图片，OCR 只提供坐标参考，文字内容以你看到的为准

## 工作流程
1. 用 get_page_image 查看页面图片
2. 用 ocr_extract_text 获取文字坐标
3. 你直接翻译（你能看到图片，翻译更准确）
4. 用 generate_translated_image 生成翻译后的页面图片
5. 用 save_scan_pdf 保存为 PDF

## 关键原则
- 扫描件优先用 generate_translated_image，因为它能保留原始背景
- overlay_translated_text 只适合纯白/纯色背景，大多数扫描件不适合
- 如果 generate_translated_image 失败，用 ask_user 问用户怎么处理，不要自己降级
- OCR 经常识别错字，以你从图片中看到的为准
- 专有名词用 update_memory 记录，确保跨页一致
- 每页处理完用 report_progress 通知用户

## 必须问用户的情况
- 人名翻译（拼音→汉字是一对多，你不可能猜对）
- 不确定的专业术语、机构名、地名
- 文档用途（用户可能有特殊要求）
- 任何你拿不准的翻译选择
- 用 ask_user 工具提问，用户回答后必须严格遵守
