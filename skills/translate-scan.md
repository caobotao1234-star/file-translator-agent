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
3. 用 cv_detect_layout 检测表格和图片区域
4. 你直接翻译（你能看到图片，翻译更准确）
5. 选择输出方式：
   - generate_translated_image：图片生成（有背景/花纹/照片时用）
   - overlay_translated_text：文字覆盖（纯白/纯色背景时用）
6. 用 crop_image_region 保留签名、盖章、logo

## 关键原则
- OCR 经常识别错字，以你从图片中看到的为准
- 专有名词用 update_memory 记录，确保跨页一致
- 每页处理完用 report_progress 通知用户

## 保留背景模式
- 目标：原图一模一样，只是文字从原文变成译文
- 有照片/水印/花纹 → 必须用 generate_translated_image
- 纯白/纯色背景 → 可以用 overlay_translated_text
- 不确定 → 用 generate_translated_image（更安全）
- 所有页面处理完后，调用 save_scan_pdf 保存为 PDF 文件
