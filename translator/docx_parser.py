# translator/docx_parser.py
from docx import Document
from docx.shared import Pt, RGBColor
from typing import List, Dict, Any, Optional
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：Word 文档解析器
# =============================================================
# Word 文档的结构（简化版）：
#
#   Document
#     └── Paragraph（段落）
#           └── Run（文本片段）
#
# 一个段落可以包含多个 Run，每个 Run 有自己的格式：
#   "这是一段 **加粗** 和 *斜体* 混排的文字"
#   → Run1: "这是一段 "（普通）
#   → Run2: "加粗"（bold=True）
#   → Run3: " 和 "（普通）
#   → Run4: "斜体"（italic=True）
#   → Run5: " 混排的文字"（普通）
#
# 我们的解析策略：
#   - 以段落为翻译单位（一段一段翻，保持段落结构）
#   - 记录每个 Run 的格式信息（字体、字号、加粗、斜体、颜色等）
#   - 翻译后按照原始 Run 的格式比例重新分配格式
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

    # 段落格式
    pf = paragraph.paragraph_format
    if pf:
        style["line_spacing"] = pf.line_spacing
        style["space_before"] = pf.space_before.pt if pf.space_before else None
        style["space_after"] = pf.space_after.pt if pf.space_after else None
        style["first_line_indent"] = pf.first_line_indent.pt if pf.first_line_indent else None

    return style


def parse_docx(filepath: str) -> Dict[str, Any]:
    """
    解析 Word 文档，返回结构化数据。

    返回格式：
    {
        "paragraphs": [
            {
                "index": 0,
                "full_text": "这是一段完整的文字",
                "style": { 段落样式信息 },
                "runs": [
                    {
                        "text": "这是一段",
                        "format": { Run 格式信息 }
                    },
                    ...
                ]
            },
            ...
        ]
    }
    """
    logger.info(f"开始解析文档: {filepath}")
    doc = Document(filepath)

    paragraphs = []
    for i, para in enumerate(doc.paragraphs):
        full_text = para.text.strip()

        # 跳过空段落
        if not full_text:
            paragraphs.append({
                "index": i,
                "full_text": "",
                "style": _extract_paragraph_style(para),
                "runs": [],
                "is_empty": True,
            })
            continue

        runs = []
        for run in para.runs:
            if not run.text:
                continue
            runs.append({
                "text": run.text,
                "format": _extract_run_format(run),
            })

        paragraphs.append({
            "index": i,
            "full_text": full_text,
            "style": _extract_paragraph_style(para),
            "runs": runs,
            "is_empty": False,
        })

    logger.info(f"解析完成: {len(paragraphs)} 个段落，"
                f"其中 {sum(1 for p in paragraphs if not p['is_empty'])} 个有内容")

    return {"paragraphs": paragraphs}
