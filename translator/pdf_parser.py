# translator/pdf_parser.py
import fitz  # PyMuPDF
from typing import List, Dict, Any, Optional
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：PDF 文档解析器
# =============================================================
# PDF 和 Word/PPT 完全不同：
#   Word/PPT: 结构化文档 → 段落、Run、样式，天然有层次
#   PDF: "画布"模型 → 在坐标(x,y)用字体F画字符C，没有段落概念
#
# PyMuPDF 的 page.get_text("dict") 返回结构化数据：
#   Page → Blocks[] → Lines[] → Spans[]
#   - Block: 一个文本块（矩形区域），有 bbox 坐标
#   - Line: 块内的一行文字
#   - Span: 行内连续的同格式文字片段（类似 Word 的 Run）
#     包含: text, font, size, color, bbox, flags(bold/italic)
#
# 我们的策略：
#   1. 按 Block 为单位提取文本（一个 Block ≈ 一个段落/标题）
#   2. 保留每个 Span 的字体/大小/颜色/位置信息
#   3. 合并同一 Block 内所有 Line 的文本作为翻译单元
#   4. Key 命名: pg{page}_b{block}
#   5. 跳过图片 Block（type=1）和空文本
#
# 难点：
#   - PDF 没有"段落"概念，Block 的划分可能不完美
#   - 同一视觉段落可能被拆成多个 Block
#   - 页眉页脚也会被提取，需要过滤
# =============================================================

logger = get_logger("pdf_parser")

# 页眉页脚区域占页面高度的比例阈值
HEADER_FOOTER_RATIO = 0.06
# 太短的文本不翻译（页码、单个符号等）
MIN_TEXT_LENGTH = 2


def _extract_span_format(span: dict) -> Dict[str, Any]:
    """
    从 PyMuPDF span 字典中提取格式信息。

    📘 教学笔记：span["flags"] 是位掩码
    bit 0 (1)  = superscript
    bit 1 (2)  = italic
    bit 2 (4)  = serif font
    bit 3 (8)  = monospaced
    bit 4 (16) = bold
    """
    flags = span.get("flags", 0)
    # color 是整数 (0xRRGGBB)
    color_int = span.get("color", 0)
    color_hex = f"#{color_int:06x}"

    return {
        "font_name": span.get("font", ""),
        "font_size": round(span.get("size", 12), 1),
        "font_color": color_hex,
        "bold": bool(flags & 16),
        "italic": bool(flags & 2),
        "bbox": list(span.get("bbox", [0, 0, 0, 0])),
    }


def _is_header_footer(block_bbox: list, page_height: float) -> bool:
    """判断一个 Block 是否在页眉/页脚区域"""
    y0, y1 = block_bbox[1], block_bbox[3]
    top_threshold = page_height * HEADER_FOOTER_RATIO
    bottom_threshold = page_height * (1 - HEADER_FOOTER_RATIO)
    # 整个 block 都在顶部或底部区域
    if y1 < top_threshold or y0 > bottom_threshold:
        return True
    return False


def _merge_block_text(block: dict) -> tuple:
    """
    合并一个 Block 内所有 Line/Span 的文本。

    返回: (full_text, spans_info, is_multiline)
    - full_text: 合并后的纯文本
    - spans_info: 所有 span 的格式信息列表
    - is_multiline: 是否多行
    """
    lines = block.get("lines", [])
    all_spans = []
    line_texts = []

    for line in lines:
        line_text_parts = []
        for span in line.get("spans", []):
            text = span.get("text", "").strip()
            if not text:
                continue
            all_spans.append({
                "text": span.get("text", ""),
                "format": _extract_span_format(span),
                "origin": list(span.get("origin", [0, 0])),
            })
            line_text_parts.append(span.get("text", ""))
        if line_text_parts:
            line_texts.append("".join(line_text_parts))

    full_text = "\n".join(line_texts)
    is_multiline = len(line_texts) > 1
    return full_text, all_spans, is_multiline


def _get_dominant_format(spans_info: list) -> Dict[str, Any]:
    """
    获取一个 Block 中占主导地位的格式（按文本长度加权）。
    用于写回时作为默认格式。
    """
    if not spans_info:
        return {"font_name": "helv", "font_size": 12, "font_color": "#000000",
                "bold": False, "italic": False}

    # 按文本长度加权
    font_weights = {}
    for s in spans_info:
        key = (s["format"]["font_name"], s["format"]["font_size"],
               s["format"]["font_color"], s["format"]["bold"], s["format"]["italic"])
        font_weights[key] = font_weights.get(key, 0) + len(s["text"])

    dominant = max(font_weights, key=font_weights.get)
    return {
        "font_name": dominant[0],
        "font_size": dominant[1],
        "font_color": dominant[2],
        "bold": dominant[3],
        "italic": dominant[4],
    }


def parse_pdf(filepath: str) -> Dict[str, Any]:
    """
    解析 PDF 文档，返回所有翻译单元。

    返回格式与 docx_parser / pptx_parser 一致：
    {
        "items": [
            {
                "key": "pg0_b2",
                "type": "pdf_block",
                "full_text": "合并后的文本",
                "bbox": [x0, y0, x1, y1],
                "spans": [...],
                "dominant_format": {...},
                "is_empty": False,
            },
            ...
        ],
        "page_count": N,
    }
    """
    logger.info(f"开始解析 PDF: {filepath}")
    doc = fitz.open(filepath)

    items = []
    page_count = len(doc)
    skipped_header_footer = 0
    skipped_short = 0

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_height = page.rect.height
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        for block_idx, block in enumerate(text_dict.get("blocks", [])):
            # 跳过图片 Block
            if block.get("type") != 0:
                continue

            block_bbox = block.get("bbox", [0, 0, 0, 0])

            # 跳过页眉页脚
            if _is_header_footer(list(block_bbox), page_height):
                skipped_header_footer += 1
                continue

            full_text, spans_info, is_multiline = _merge_block_text(block)
            clean_text = full_text.strip()

            if not clean_text or len(clean_text) < MIN_TEXT_LENGTH:
                skipped_short += 1
                continue

            key = f"pg{page_idx}_b{block_idx}"
            dominant_fmt = _get_dominant_format(spans_info)

            items.append({
                "key": key,
                "type": "pdf_block",
                "full_text": clean_text,
                "bbox": list(block_bbox),
                "spans": spans_info,
                "dominant_format": dominant_fmt,
                "is_multiline": is_multiline,
                "is_empty": False,
            })

    doc.close()

    block_count = len(items)
    logger.info(
        f"PDF 解析完成: {block_count} 个文本块，{page_count} 页，"
        f"跳过页眉页脚 {skipped_header_footer} 个，跳过短文本 {skipped_short} 个"
    )

    return {"items": items, "page_count": page_count}
