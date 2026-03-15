# translator/pdf_writer.py
import fitz  # PyMuPDF
from typing import Dict, Any, List, Optional, Tuple
from translator.format_engine import FormatEngine
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：PDF 文档生成器（v3 — insert_htmlbox 策略）
# =============================================================
# PDF 翻译的核心难题：中文紧凑，英文松散。
# "公司简介" 只需 33pt 宽，"Company Profile" 需要 ~80pt。
#
# v1: 用原始 bbox 写入 → 英文放不下，大量空白
# v2: 扩展写入 rect，但 redaction 仍用原始 bbox
#     → 相邻块的 redaction 擦掉了扩展区域的文本
# v3: 改用 insert_htmlbox + scale_low=0
#     → PyMuPDF 自动缩放文本以适应区域，不会出现"放不下就不显示"
#     → redaction 和写入都用扩展 rect，避免互相擦除
#
# insert_htmlbox vs insert_textbox:
#   - insert_textbox: 放不下就不写（rc < 0），开发者自己处理
#   - insert_htmlbox: scale_low=0 时自动缩小文本直到放得下
#     返回 (spare_height, scale)，spare_height=-1 才是真的失败
#   - insert_htmlbox 还支持 HTML 样式（粗体、颜色等）
# =============================================================

logger = get_logger("pdf_writer")

FALLBACK_FONT_MAP = {
    "SimSun": "china-ss",
    "SimHei": "china-ss",
    "Microsoft YaHei": "china-ss",
    "KaiTi": "china-ss",
    "FangSong": "china-ss",
    "DengXian": "china-ss",
    "Times New Roman": "tiro",
    "Arial": "helv",
    "Calibri": "helv",
    "Helvetica": "helv",
    "Courier": "cour",
    "Courier New": "cour",
}


# 📘 教学笔记：HTML 字体族映射
# insert_htmlbox 用 CSS font-family，不是 PyMuPDF 内部字体名。
# CJK 字体需要用 "china-ss" 等 PyMuPDF 内置名，
# 但 HTML 模式下用 CSS 通用族名更可靠。
HTML_FONT_FAMILY_MAP = {
    "china-ss": "sans-serif",
    "tiro": "serif",
    "helv": "sans-serif",
    "cour": "monospace",
}


def _color_hex_to_tuple(hex_color: str) -> tuple:
    """'#ff0000' → (1.0, 0.0, 0.0)"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return (0, 0, 0)
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return (r, g, b)


def _resolve_font(font_name: str, has_cjk: bool) -> str:
    """将 PDF 原始字体名映射为 PyMuPDF 可用的字体。"""
    if font_name in FALLBACK_FONT_MAP:
        return FALLBACK_FONT_MAP[font_name]
    name_lower = font_name.lower()
    for key, val in FALLBACK_FONT_MAP.items():
        if key.lower() in name_lower:
            return val
    if has_cjk:
        return "china-ss"
    return "helv"


def _has_cjk(text: str) -> bool:
    """检测文本是否包含 CJK 字符"""
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3040' <= ch <= '\u30ff' or '\uac00' <= ch <= '\ud7af':
            return True
    return False


def _estimate_text_width(text: str, fontsize: float, fontname: str) -> float:
    """估算文本渲染宽度，取最长行。"""
    max_width = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            w = fitz.get_text_length(line, fontname=fontname, fontsize=fontsize)
        except Exception:
            w = len(line) * fontsize * 0.6
        if w > max_width:
            max_width = w
    return max_width


def _calc_write_rect(
    text: str, original_rect: fitz.Rect, fontname: str,
    fontsize: float, page_rect: fitz.Rect,
) -> fitz.Rect:
    """
    📘 教学笔记：计算写入区域（同时用于 redaction 和写入）

    策略：
    1. 估算译文需要的宽度
    2. 宽度不够 → 向右扩展（不超出页面）
    3. 右边放不下 → 向左扩展
    4. 高度不够 → 向下扩展
    5. 加 15% 余量，给 insert_htmlbox 的自动缩放留空间
    """
    needed_width = _estimate_text_width(text, fontsize, fontname)
    needed_width *= 1.15  # 15% 余量

    rect = fitz.Rect(original_rect)
    page_w = page_rect.width
    page_h = page_rect.height

    # ---- 扩展宽度 ----
    if needed_width > rect.width:
        extra = needed_width - rect.width
        new_x1 = rect.x1 + extra
        if new_x1 <= page_w - 5:
            rect.x1 = new_x1
        else:
            rect.x1 = page_w - 5
            remaining = needed_width - (rect.x1 - rect.x0)
            if remaining > 0:
                rect.x0 = max(5, rect.x0 - remaining)

    # ---- 扩展高度 ----
    line_count = text.count("\n") + 1
    needed_height = max(line_count * fontsize * 1.4, fontsize * 1.5)
    if needed_height > rect.height:
        new_y1 = rect.y0 + needed_height
        rect.y1 = min(new_y1, page_h - 5)

    return rect


def _build_html_text(text: str, fontsize: float, font_color: str,
                     bold: bool, pymupdf_font: str) -> str:
    """
    📘 教学笔记：构建 HTML 文本用于 insert_htmlbox
    
    insert_htmlbox 接受 HTML 字符串，支持 CSS 样式。
    我们用内联样式控制字号、颜色、粗体等。
    换行符 \\n 在 HTML 中无效，需要转为 <br>。
    特殊字符需要转义（<, >, &）。
    """
    # CSS font-family
    css_family = HTML_FONT_FAMILY_MAP.get(pymupdf_font, "sans-serif")

    # 转义 HTML 特殊字符
    safe_text = (text
                 .replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))
    # 换行符 → <br>
    safe_text = safe_text.replace("\n", "<br>")

    weight = "bold" if bold else "normal"

    return (
        f'<p style="font-size:{fontsize}pt; color:{font_color}; '
        f'font-weight:{weight}; font-family:{css_family}; '
        f'margin:0; padding:0; line-height:1.2;">'
        f'{safe_text}</p>'
    )


def write_pdf(
    parsed_data: Dict[str, Any],
    translations: Dict[str, str],
    output_path: str,
    format_engine: FormatEngine,
    source_path: str = None,
):
    """
    基于原 PDF 生成翻译后的文件。

    📘 v3 策略：
    1. 预计算所有块的扩展 rect
    2. Redaction 用扩展 rect（确保写入区域被完全清空）
    3. 写入用 insert_htmlbox + scale_low=0（自动缩放，不会"放不下就消失"）
    4. 降级方案：insert_htmlbox 失败 → insert_textbox 兜底
    """
    if not source_path:
        raise ValueError("source_path is required")

    logger.info(f"开始生成 PDF: {output_path}")
    doc = fitz.open(source_path)

    # ---- 按页分组 ----
    page_items: Dict[int, List[dict]] = {}
    for item in parsed_data["items"]:
        key = item["key"]
        if key not in translations:
            continue
        page_idx = int(key.split("_")[0][2:])  # "pg0_b2" → 0
        if page_idx not in page_items:
            page_items[page_idx] = []
        page_items[page_idx].append(item)

    replaced_count = 0
    failed_count = 0

    for page_idx, items in page_items.items():
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]
        page_rect = page.rect

        # ---- 预计算：为每个块准备写入信息 ----
        block_info_list = []
        for item in items:
            key = item["key"]
            translated = translations[key]
            bbox = item["bbox"]
            original_rect = fitz.Rect(bbox)
            fmt = item["dominant_format"]

            font_name = fmt["font_name"]
            font_size = fmt["font_size"]
            font_color_hex = fmt["font_color"]
            font_color_tuple = _color_hex_to_tuple(font_color_hex)
            bold = fmt.get("bold", False)

            mapped_font = format_engine.resolve_font(font_name)
            display_font = mapped_font or font_name
            cjk = _has_cjk(translated)
            pymupdf_font = _resolve_font(display_font, cjk)

            # 计算扩展写入区域
            write_rect = _calc_write_rect(
                translated, original_rect, pymupdf_font, font_size, page_rect
            )

            block_info_list.append({
                "key": key,
                "translated": translated,
                "original_rect": original_rect,
                "write_rect": write_rect,
                "font_size": font_size,
                "font_color_hex": font_color_hex,
                "font_color_tuple": font_color_tuple,
                "bold": bold,
                "pymupdf_font": pymupdf_font,
                "cjk": cjk,
            })

        # ---- 第一步：Redaction（用扩展 rect 擦除）----
        # 📘 v3 关键：redaction 和写入用同一个 rect
        # 这样不会出现"A 的写入区域被 B 的 redaction 擦掉"的问题
        for info in block_info_list:
            redact_rect = info["write_rect"] + (-1, -1, 1, 1)
            page.add_redact_annot(redact_rect, text="", fill=(1, 1, 1))

        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # ---- 第二步：写入译文（insert_htmlbox 优先）----
        for info in block_info_list:
            key = info["key"]
            translated = info["translated"]
            write_rect = info["write_rect"]
            success = False

            # 方案 A：insert_htmlbox（自动缩放，最可靠）
            try:
                html = _build_html_text(
                    translated, info["font_size"], info["font_color_hex"],
                    info["bold"], info["pymupdf_font"],
                )
                result = page.insert_htmlbox(
                    write_rect, html, scale_low=0, overlay=True
                )
                # result = (spare_height, scale)
                spare_height = result[0] if isinstance(result, tuple) else result
                if spare_height >= 0:
                    success = True
                    if isinstance(result, tuple) and result[1] < 0.5:
                        logger.debug(
                            f"文本大幅缩放 key={key}, scale={result[1]:.2f}"
                        )
                else:
                    logger.debug(f"insert_htmlbox 失败 key={key}, spare={spare_height}")
            except Exception as e:
                logger.debug(f"insert_htmlbox 异常 key={key}: {e}")

            # 方案 B：insert_textbox 降级（如果 htmlbox 失败）
            if not success:
                try:
                    rc = page.insert_textbox(
                        write_rect, translated,
                        fontsize=info["font_size"],
                        fontname=info["pymupdf_font"],
                        color=info["font_color_tuple"],
                        align=fitz.TEXT_ALIGN_LEFT,
                        overlay=True,
                    )
                    if rc >= 0:
                        success = True
                    else:
                        # 缩小字号再试一次
                        rc2 = page.insert_textbox(
                            write_rect, translated,
                            fontsize=max(4.0, info["font_size"] * 0.5),
                            fontname=info["pymupdf_font"],
                            color=info["font_color_tuple"],
                            align=fitz.TEXT_ALIGN_LEFT,
                            overlay=True,
                        )
                        if rc2 >= 0:
                            success = True
                except Exception as e:
                    logger.debug(f"insert_textbox 降级异常 key={key}: {e}")

            # 方案 C：最后一搏 — 用最基础的字体和最小字号
            if not success:
                try:
                    fallback = "china-ss" if info["cjk"] else "helv"
                    page.insert_textbox(
                        write_rect, translated,
                        fontsize=4.0, fontname=fallback,
                        color=info["font_color_tuple"],
                        overlay=True,
                    )
                    success = True
                except Exception as e2:
                    logger.error(f"所有写入方案均失败 key={key}: {e2}")

            if success:
                replaced_count += 1
            else:
                failed_count += 1
                logger.warning(
                    f"文本写入失败 key={key}, "
                    f"rect={write_rect.width:.0f}x{write_rect.height:.0f}, "
                    f"text={translated[:30]}"
                )

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

    if failed_count:
        logger.warning(f"PDF 生成完成: {output_path}（成功 {replaced_count}，失败 {failed_count}）")
    else:
        logger.info(f"PDF 生成完成: {output_path}（成功写入 {replaced_count} 个文本块）")
