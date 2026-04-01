---
name: 格式审查
trigger: after_write
description: 翻译输出后的格式审查和修复经验
---

# 格式审查经验

## 审查时机
翻译完成并 write_document 后，必须检查输出效果。

## 审查方法
1. 用 inspect_output 查看关键页面的文本布局数据
2. 用 compare_layout 对比原文和译文的布局差异
3. 如果是 PPT 且有 Windows + Office，用 render_slide 看真实效果

## 常见问题和修复
- 字号过小 → smart_resize 或 adjust_format
- 文字溢出 → enable_autofit（PPT 最可靠）
- 格式标记丢失 → 检查翻译结果是否保留了 <r0> 标记
- 布局偏移 → compare_layout 定位问题页面

## 审查原则
- 目标不是"跟原文一模一样"，而是"译文本身美观专业"
- 你自己判断什么字号、什么布局最合适
- 抽查 2-3 个关键页面即可，不需要逐页检查
