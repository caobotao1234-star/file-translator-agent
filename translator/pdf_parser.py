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
# 📘 太小的文本块可能是图标/Logo 内嵌文字，跳过
# 面积 < 此阈值（pt²）的块不翻译
MIN_BLOCK_AREA = 100  # 约 10x10 pt


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

def _detect_alignment(block_bbox: list, spans_info: list) -> str:
    """
    📘 教学笔记：从 span 位置推断文本对齐方式

    PDF 没有"对齐"属性，但可以从文本在 block 内的位置推断：
    - 文本起始 x 接近 block 左边界 → 左对齐
    - 文本起始 x 远离左边界，且结束 x 接近右边界 → 右对齐
    - 文本在 block 中间 → 居中

    对于数字、短文本块尤其重要——它们经常是居中或右对齐的。
    """
    if not spans_info:
        return "left"

    block_x0 = block_bbox[0]
    block_x1 = block_bbox[2]
    block_width = block_x1 - block_x0
    if block_width < 5:
        return "left"

    # 取所有 span 的 bbox 范围
    text_x0 = min(s["format"]["bbox"][0] for s in spans_info)
    text_x1 = max(s["format"]["bbox"][2] for s in spans_info)

    left_margin = text_x0 - block_x0
    right_margin = block_x1 - text_x1
    text_width = text_x1 - text_x0

    # 📘 如果文本几乎填满 block，无法判断对齐，默认左对齐
    if text_width > block_width * 0.85:
        return "left"

    # 📘 左右边距差异判断
    margin_diff = abs(left_margin - right_margin)
    tolerance = block_width * 0.1  # 10% 容差

    if margin_diff < tolerance:
        return "center"
    elif left_margin > right_margin + tolerance:
        return "right"
    else:
        return "left"



def _should_merge(block_a: dict, block_b: dict) -> bool:
    """
    📘 教学笔记：判断两个相邻 Block 是否应该合并为同一段落。

    v4 修复：更严格的合并条件，防止跨列合并。
    
    合并条件（全部满足才合并）：
    1. 字体/字号/颜色/粗体一致
    2. 垂直距离小（紧挨着的行）
    3. 左边界对齐（绝对偏差 < 字号的 3 倍，约一个缩进）
    4. A 的文本不以终止标点结尾
    5. A 不是纯数字/短文本
    6. 宽度差异不能太大
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
        return False

    # 📘 条件 5（v4 修复）：左边界严格对齐
    # 同一段落的续行，左边界应该非常接近（最多差一个缩进）。
    # 用绝对值而不是页宽百分比，因为百分比对双栏布局太宽松。
    # 3 倍字号 ≈ 一个段落缩进的距离，足够容纳首行缩进。
    x0_diff = abs(bbox_a[0] - bbox_b[0])
    max_x0_diff = fmt_a["font_size"] * 3
    if x0_diff > max_x0_diff:
        return False

    # 条件 6：A 的文本不以终止标点结尾
    # 📘 v4 例外：如果 B 很短（< 10 字符），可能是段落末尾的"孤儿行"
    # （如 "原梦！"），这种情况即使 A 以句号结尾也应该合并。
    text_a = block_a["full_text"].rstrip()
    text_b = block_b["full_text"].strip()
    b_is_orphan = len(text_b.replace(" ", "").replace("\n", "")) < 10
    if text_a and text_a[-1] in "。！？.!?；;：:）)】」》" and not b_is_orphan:
        return False

    # 📘 条件 7：A 不是纯数字/短独立文本
    stripped_a = text_a.replace(" ", "").replace("\n", "")
    if len(stripped_a) <= 4:
        digit_count = sum(1 for c in stripped_a if c.isdigit() or c in ".%-")
        if digit_count >= len(stripped_a) * 0.5:
            return False

    # 📘 条件 8：两个块的宽度差异不能太大
    width_a = bbox_a[2] - bbox_a[0]
    width_b = bbox_b[2] - bbox_b[0]
    if min(width_a, width_b) > 0:
        width_ratio = max(width_a, width_b) / min(width_a, width_b)
        if width_ratio > 3:
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

            # 合并 text_bbox（取并集）
            prev["text_bbox"] = [
                min(prev["text_bbox"][0], current.get("text_bbox", current["bbox"])[0]),
                min(prev["text_bbox"][1], current.get("text_bbox", current["bbox"])[1]),
                max(prev["text_bbox"][2], current.get("text_bbox", current["bbox"])[2]),
                max(prev["text_bbox"][3], current.get("text_bbox", current["bbox"])[3]),
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


def _split_wide_block(block: dict, block_idx: int, page_idx: int) -> List[dict]:
    """
    📘 教学笔记：水平拆分宽 Block

    PyMuPDF 经常把同一行高度的不相邻文本归到同一个 block。
    例如页面左边的"集团简介"和右边的"领导关怀&战略合作"
    会变成一个 block，bbox 横跨整页。

    检测方法：分析 block 内所有 span 的 x 坐标，
    如果存在大间隙（> 3 倍字号），就在间隙处拆分。

    返回拆分后的多个"伪 block"字典列表。
    如果不需要拆分，返回 [block]（原样）。
    """
    lines = block.get("lines", [])
    if not lines:
        return [block]

    # 收集所有 span 及其 x 坐标
    all_spans_with_x = []
    for line in lines:
        for span in line.get("spans", []):
            text = span.get("text", "").strip()
            if not text:
                continue
            sb = span.get("bbox", [0, 0, 0, 0])
            font_size = span.get("size", 12)
            all_spans_with_x.append({
                "span": span,
                "x0": sb[0],
                "x1": sb[2],
                "y0": sb[1],
                "y1": sb[3],
                "font_size": font_size,
            })

    if len(all_spans_with_x) < 2:
        return [block]

    # 按 x0 排序，找最大间隙
    sorted_spans = sorted(all_spans_with_x, key=lambda s: s["x0"])
    avg_font_size = sum(s["font_size"] for s in sorted_spans) / len(sorted_spans)
    gap_threshold = avg_font_size * 3  # 3 倍字号以上的间隙认为是分隔

    # 找所有大间隙的位置
    split_points = []
    for i in range(1, len(sorted_spans)):
        gap = sorted_spans[i]["x0"] - sorted_spans[i - 1]["x1"]
        if gap > gap_threshold:
            split_points.append(i)

    if not split_points:
        return [block]

    # 按间隙拆分 span 组
    groups = []
    prev = 0
    for sp in split_points:
        groups.append(sorted_spans[prev:sp])
        prev = sp
    groups.append(sorted_spans[prev:])

    # 为每组构建伪 block
    result_blocks = []
    for gi, group in enumerate(groups):
        if not group:
            continue
        # 构建这组 span 的 bbox
        g_x0 = min(s["x0"] for s in group)
        g_y0 = min(s["y0"] for s in group)
        g_x1 = max(s["x1"] for s in group)
        g_y1 = max(s["y1"] for s in group)

        # 筛选属于这组的 lines 和 spans
        group_x_range = (g_x0 - 1, g_x1 + 1)
        new_lines = []
        for line in lines:
            new_spans = []
            for span in line.get("spans", []):
                sb = span.get("bbox", [0, 0, 0, 0])
                # span 的中心 x 在这组范围内
                span_cx = (sb[0] + sb[2]) / 2
                if group_x_range[0] <= span_cx <= group_x_range[1]:
                    new_spans.append(span)
            if new_spans:
                new_lines.append({"spans": new_spans})

        if new_lines:
            new_block = dict(block)
            new_block["lines"] = new_lines
            new_block["bbox"] = (g_x0, g_y0, g_x1, g_y1)
            result_blocks.append(new_block)

    if len(result_blocks) > 1:
        logger.debug(
            f"拆分宽 Block pg{page_idx}_b{block_idx}: "
            f"1 → {len(result_blocks)} 个子块"
        )

    return result_blocks if result_blocks else [block]


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

            # 📘 v3: 水平拆分宽 Block
            # PyMuPDF 会把同一行高度的不相邻文本归到同一个 block，
            # 导致左右两组独立文本被当成一个翻译单元。
            # 先拆分，再逐个处理。
            sub_blocks = _split_wide_block(block, block_idx, page_idx)

            for sub_idx, sub_block in enumerate(sub_blocks):
                sub_bbox = sub_block.get("bbox", [0, 0, 0, 0])

                full_text, spans_info, is_multiline = _merge_block_text(sub_block)
                clean_text = full_text.strip()

                if not clean_text or len(clean_text) < MIN_TEXT_LENGTH:
                    skipped_short += 1
                    continue

                # 📘 跳过面积太小的块（可能是图标/Logo 内嵌文字）
                block_w = sub_bbox[2] - sub_bbox[0]
                block_h = sub_bbox[3] - sub_bbox[1]
                if block_w * block_h < MIN_BLOCK_AREA:
                    skipped_short += 1
                    continue

                # 拆分后的 key 带子索引，避免重复
                if len(sub_blocks) > 1:
                    key = f"pg{page_idx}_b{block_idx}s{sub_idx}"
                else:
                    key = f"pg{page_idx}_b{block_idx}"

                dominant_fmt = _get_dominant_format(spans_info)
                alignment = _detect_alignment(list(sub_bbox), spans_info)

                # 📘 text_bbox — 文本实际占据的精确区域
                if spans_info:
                    text_bbox = [
                        min(s["format"]["bbox"][0] for s in spans_info),
                        min(s["format"]["bbox"][1] for s in spans_info),
                        max(s["format"]["bbox"][2] for s in spans_info),
                        max(s["format"]["bbox"][3] for s in spans_info),
                    ]
                else:
                    text_bbox = list(sub_bbox)

                page_items.append({
                    "key": key,
                    "type": "pdf_block",
                    "full_text": clean_text,
                    "bbox": list(sub_bbox),
                    "text_bbox": text_bbox,
                    "sub_bboxes": [list(sub_bbox)],
                    "spans": spans_info,
                    "dominant_format": dominant_fmt,
                    "alignment": alignment,
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
