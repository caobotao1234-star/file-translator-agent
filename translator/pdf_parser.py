# translator/pdf_parser.py
import fitz  # PyMuPDF
from typing import List, Dict, Any, Optional
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：PDF 文档解析器（v2 — 智能合并相邻 Block）
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
#
# 📘 v2 核心改进：相邻 Block 合并
# PDF 经常把同一段话拆成多个 Block（按行拆分）。例如：
#   Block A: "东方建科代表案例：河南省直青年人才公寓慧城苑、博学"
#   Block B: "苑、航港南苑、郑州市人才公寓滨河苑（慧康佳苑）、中海·"
#   Block C: "云鼎湖居、豫发白鹭源春晓、康桥那云溪、鹤壁市淇水花园、"
#
# 如果不合并，模型收到 3 个独立"段落"，翻译会断裂。
# 合并条件：
#   1. 相邻 Block 的字体/字号/颜色一致（同一段落的格式通常一致）
#   2. 垂直距离 < 行高的 1.5 倍（紧挨着的行）
#   3. 水平位置有重叠（同一列的文字）
#
# 合并后：
#   - full_text 拼接（不加换行，因为原文就是连续的）
#   - bbox 取并集（union）
#   - 保留所有子块的原始 bbox（sub_bboxes），writer 需要逐个擦除
# =============================================================

logger = get_logger("pdf_parser")

HEADER_FOOTER_RATIO = 0.06
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
    if y1 < top_threshold or y0 > bottom_threshold:
        return True
    return False


def _merge_block_text(block: dict) -> tuple:
    """
    合并一个 Block 内所有 Line/Span 的文本。
    返回: (full_text, spans_info, is_multiline)
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
    """获取一个 Block 中占主导地位的格式（按文本长度加权）。"""
    if not spans_info:
        return {"font_name": "helv", "font_size": 12, "font_color": "#000000",
                "bold": False, "italic": False}

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


def _should_merge(block_a: dict, block_b: dict) -> bool:
    """
    📘 教学笔记：判断两个相邻 Block 是否应该合并为同一段落。

    PDF 把同一段话按行拆成多个 Block 是常见现象。
    判断依据：
    1. 字体/字号/颜色一致（同一段落格式通常一致）
    2. 垂直距离小（B 的顶部紧接 A 的底部，间距 < 行高 * 1.5）
    3. 水平位置有重叠（同一列的文字，x 范围有交集）
    4. A 的文本不以句号/问号/感叹号等结尾（结尾标点说明是完整句子）

    不合并的情况：
    - 标题和正文（字号不同）
    - 不同列的文字（x 范围无交集）
    - 两段独立的话（A 以句号结尾）
    """
    fmt_a = block_a["dominant_format"]
    fmt_b = block_b["dominant_format"]

    # 条件 1：字号必须一致（允许 0.5pt 误差）
    if abs(fmt_a["font_size"] - fmt_b["font_size"]) > 0.5:
        return False

    # 条件 2：颜色必须一致
    if fmt_a["font_color"] != fmt_b["font_color"]:
        return False

    # 条件 3：粗体状态一致
    if fmt_a["bold"] != fmt_b["bold"]:
        return False

    bbox_a = block_a["bbox"]  # [x0, y0, x1, y1]
    bbox_b = block_b["bbox"]

    # 条件 4：垂直距离 — B 的顶部应该紧接 A 的底部
    line_height = fmt_a["font_size"] * 1.5
    vertical_gap = bbox_b[1] - bbox_a[3]  # B.y0 - A.y1
    if vertical_gap < -2 or vertical_gap > line_height:
        # 负值太大说明 B 在 A 上面（不是"下一行"）
        # 正值太大说明间距太远（不是同一段）
        return False

    # 条件 5：水平位置有重叠（同一列）
    x_overlap = min(bbox_a[2], bbox_b[2]) - max(bbox_a[0], bbox_b[0])
    min_width = min(bbox_a[2] - bbox_a[0], bbox_b[2] - bbox_b[0])
    if min_width > 0 and x_overlap / min_width < 0.3:
        # 水平重叠不到 30%，说明不在同一列
        return False

    # 条件 6：A 的文本不以终止标点结尾
    text_a = block_a["full_text"].rstrip()
    if text_a and text_a[-1] in "。！？.!?；;：:）)】」》":
        return False

    return True


def _merge_items(items: List[dict]) -> List[dict]:
    """
    📘 教学笔记：合并相邻的同段落 Block

    遍历所有 Block，如果当前 Block 和上一个 Block 满足合并条件，
    就把它们合并成一个。合并后：
    - full_text 直接拼接（不加换行，因为原文是连续的）
    - bbox 取并集
    - sub_bboxes 记录所有原始子块的 bbox（writer 需要逐个擦除）
    - spans 合并
    - key 用第一个子块的 key
    """
    if not items:
        return items

    merged = [items[0]]
    # 确保第一个 item 有 sub_bboxes
    if "sub_bboxes" not in merged[0]:
        merged[0]["sub_bboxes"] = [merged[0]["bbox"][:]]

    for i in range(1, len(items)):
        current = items[i]
        prev = merged[-1]

        if _should_merge(prev, current):
            # 合并文本（去掉换行，直接拼接）
            prev["full_text"] = prev["full_text"].rstrip() + current["full_text"].lstrip()

            # 合并 bbox（取并集）
            prev["bbox"] = [
                min(prev["bbox"][0], current["bbox"][0]),
                min(prev["bbox"][1], current["bbox"][1]),
                max(prev["bbox"][2], current["bbox"][2]),
                max(prev["bbox"][3], current["bbox"][3]),
            ]

            # 记录子块 bbox
            prev["sub_bboxes"].append(current["bbox"][:])

            # 合并 spans
            prev["spans"].extend(current.get("spans", []))
            prev["is_multiline"] = True

            # 重新计算主导格式
            prev["dominant_format"] = _get_dominant_format(prev["spans"])

            logger.debug(
                f"合并 Block: {prev['key']} + {current['key']} "
                f"→ '{prev['full_text'][:40]}...'"
            )
        else:
            if "sub_bboxes" not in current:
                current["sub_bboxes"] = [current["bbox"][:]]
            merged.append(current)

    return merged


def parse_pdf(filepath: str) -> Dict[str, Any]:
    """
    解析 PDF 文档，返回所有翻译单元。

    📘 v2: 解析后会对相邻 Block 做智能合并，
    避免同一段话被拆成多个翻译单元导致翻译断裂。
    """
    logger.info(f"开始解析 PDF: {filepath}")
    doc = fitz.open(filepath)

    raw_items = []
    page_count = len(doc)
    skipped_header_footer = 0
    skipped_short = 0

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_height = page.rect.height
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        page_items = []
        for block_idx, block in enumerate(text_dict.get("blocks", [])):
            if block.get("type") != 0:
                continue

            block_bbox = block.get("bbox", [0, 0, 0, 0])

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

            page_items.append({
                "key": key,
                "type": "pdf_block",
                "full_text": clean_text,
                "bbox": list(block_bbox),
                "sub_bboxes": [list(block_bbox)],
                "spans": spans_info,
                "dominant_format": dominant_fmt,
                "is_multiline": is_multiline,
                "is_empty": False,
            })

        # 📘 在每页内部做合并（不跨页合并）
        merged_page_items = _merge_items(page_items)
        raw_items.extend(merged_page_items)

    doc.close()

    block_count = len(raw_items)
    logger.info(
        f"PDF 解析完成: {block_count} 个文本块（合并后），{page_count} 页，"
        f"跳过页眉页脚 {skipped_header_footer} 个，跳过短文本 {skipped_short} 个"
    )

    return {"items": raw_items, "page_count": page_count}
