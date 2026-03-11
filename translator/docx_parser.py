# translator/docx_parser.py
from docx import Document
from docx.shared import Pt, RGBColor
from typing import List, Dict, Any, Optional
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：Word 文档解析器（v2 — Run 级别格式保真）
# =============================================================
# v1 的问题：
#   以段落为单位翻译，多 Run 段落的格式信息（加粗、字体混排）会丢失。
#
# v2 的策略：
#   - 单 Run 段落：直接输出纯文本（最常见，不需要标记）
#   - 多 Run 段落：用 <r0>...</r0> 标记包裹每个 Run 的文本
#     例如原文："这是**加粗**和*斜体*混排"
#     输出："<r0>这是</r0><r1>加粗</r1><r2>和</r2><r3>斜体</r3><r4>混排</r4>"
#   - LLM 翻译时保持标记，翻译后按标记拆分回多个 Run
#   - 每个 Run 继承原始格式（加粗、字体、字号、颜色等）
#
# 为什么用 <r0> 标记而不是其他方案？
#   - XML 风格标记对 LLM 来说最容易理解和保持
#   - 编号简短，不会干扰翻译质量
#   - 解析简单，用正则就能拆
# =============================================================

logger = get_logger("docx_parser")


def _extract_run_format(run) -> Dict[str, Any]:
    """提取一个 Run 的格式信息"""
    fmt = {
        "bold": run.bold,
        "italic": run.italic,
        "underline": run.underline,
        "font_name": run.font.name,
        "font_size": run.font.size.pt if run.font.size else None,
        "font_color": str(run.font.color.rgb) if run.font.color and run.font.color.rgb else None,
    }
    return fmt


def _extract_paragraph_style(paragraph) -> Dict[str, Any]:
    """提取段落级别的样式信息"""
    style = {
        "style_name": paragraph.style.name if paragraph.style else None,
        "alignment": str(paragraph.alignment) if paragraph.alignment else None,
    }

    pf = paragraph.paragraph_format
    if pf:
        style["line_spacing"] = pf.line_spacing
        style["space_before"] = pf.space_before.pt if pf.space_before else None
        style["space_after"] = pf.space_after.pt if pf.space_after else None
        style["first_line_indent"] = pf.first_line_indent.pt if pf.first_line_indent else None

    return style


def _build_tagged_text(runs_data: List[Dict]) -> str:
    """
    把多个 Run 的文本用标记包裹起来。

    输入: [{"text": "这是", ...}, {"text": "加粗", ...}, {"text": "文字", ...}]
    输出: "<r0>这是</r0><r1>加粗</r1><r2>文字</r2>"
    """
    parts = []
    for i, run in enumerate(runs_data):
        parts.append(f"<r{i}>{run['text']}</r{i}>")
    return "".join(parts)


def parse_docx(filepath: str) -> Dict[str, Any]:
    """
    解析 Word 文档，返回结构化数据。

    对于多 Run 段落，full_text 会包含 <rN>...</rN> 标记，
    标记 tagged_text 字段为 True。
    单 Run 段落的 full_text 是纯文本，tagged_text 为 False。
    """
    logger.info(f"开始解析文档: {filepath}")
    doc = Document(filepath)

    paragraphs = []
    for i, para in enumerate(doc.paragraphs):
        raw_text = para.text.strip()

        # 空段落
        if not raw_text:
            paragraphs.append({
                "index": i,
                "full_text": "",
                "style": _extract_paragraph_style(para),
                "runs": [],
                "is_empty": True,
                "tagged_text": False,
            })
            continue

        # 收集非空 Run
        runs = []
        for run in para.runs:
            if not run.text:
                continue
            runs.append({
                "text": run.text,
                "format": _extract_run_format(run),
            })

        # 判断是否需要标记
        # 只有多个 Run 且格式不完全相同时才需要标记
        needs_tagging = False
        if len(runs) > 1:
            # 检查是否有格式差异
            first_fmt = runs[0]["format"]
            for r in runs[1:]:
                if r["format"] != first_fmt:
                    needs_tagging = True
                    break

        if needs_tagging:
            full_text = _build_tagged_text(runs)
        else:
            full_text = raw_text

        paragraphs.append({
            "index": i,
            "full_text": full_text,
            "style": _extract_paragraph_style(para),
            "runs": runs,
            "is_empty": False,
            "tagged_text": needs_tagging,
        })

    tagged_count = sum(1 for p in paragraphs if p.get("tagged_text"))
    content_count = sum(1 for p in paragraphs if not p["is_empty"])
    logger.info(f"解析完成: {len(paragraphs)} 个段落，"
                f"{content_count} 个有内容，{tagged_count} 个含格式标记")

    return {"paragraphs": paragraphs}
