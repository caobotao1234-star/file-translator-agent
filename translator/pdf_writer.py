# translator/pdf_writer.py
import fitz  # PyMuPDF
import re
from typing import Dict, Any, List, Optional, Tuple
from translator.format_engine import FormatEngine
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：PDF 文档生成器（v4 — 原位写入策略）
# =============================================================
# v1~v3 的教训：
#   v1: 原始 bbox 写入 → 英文放不下，空白
#   v2: 扩展写入 rect，redaction 用原始 bbox → 相邻块互相擦除
#   v3: 扩展 rect 同时用于 redaction 和写入 → 文本框变大覆盖其他内容，
#       白底 fill 在图片上很丑，文字位置错乱
#
# v4 核心原则：严格保持原始排版
#   1. 不扩展 rect — 写入区域 = 原始 bbox（±1pt 余量）
#   2. 用 insert_htmlbox + scale_low=0 自动缩小文本适配
#   3. Redaction 用 fill=False（透明背景），不产生白底
#   4. 清理 <rN> 标记（PDF 不需要 Run 级别标记）
#   5. 文字位置严格对齐原始 bbox，不会跑到别处
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

# 📘 清理 <rN> 标记的正则
# PDF 文本不含 Run 标记，但翻译 prompt 里提到了 <rN> 规则，
# 模型偶尔会在 PDF 译文中幻觉生成这些标记。
_RN_TAG_RE = re.compile(r"</?r\d+>")


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


def _clean_rn_tags(text: str) -> str:
    """
    📘 教学笔记：清理 <rN> 标记
    PDF 的文本块是纯文本，不像 Word 有 Run 概念。
    但翻译 prompt 里有 <rN> 格式标记规则（给 Word 用的），
    模型偶尔会在 PDF 译文中幻觉生成这些标记，如 "<r5>签约仪式</r5>"。
    这里统一清理掉。
    """
    return _RN_TAG_RE.sub("", text)


def _build_html_text(text: str, fontsize: float, font_color: str,
                     bold: bool) -> str:
    """
    📘 构建 HTML 文本用于 insert_htmlbox。
    换行符 → <br>，特殊字符转义。
    """
    safe_text = (text
                 .replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))
    safe_text = safe_text.replace("\n", "<br>")
    weight = "bold" if bold else "normal"
    return (
        f'<p style="font-size:{fontsize}pt; color:{font_color}; '
        f'font-weight:{weight}; font-family:sans-serif; '
        f'margin:0; padding:0; line-height:1.15;">'
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

    📘 v4 策略：
    1. Redaction 用原始 bbox + fill=False（透明，不产生白底）
    2. 写入严格用原始 bbox（不扩展，保持排版）
    3. insert_htmlbox + scale_low=0 自动缩小字号适配
    4. 清理 <rN> 标记
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
        page_idx = int(key.split("_")[0][2:])
        if page_idx not in page_items:
            page_items[page_idx] = []
        page_items[page_idx].append(item)

    replaced_count = 0
    failed_count = 0

    for page_idx, items in page_items.items():
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]

        # ---- 预计算 ----
        block_info_list = []
        for item in items:
            key = item["key"]
            raw_translated = translations[key]
            # 📘 清理 <rN> 标记
            translated = _clean_rn_tags(raw_translated)

            bbox = item["bbox"]
            rect = fitz.Rect(bbox)
            fmt = item["dominant_format"]

            font_name = fmt["font_name"]
            font_size = fmt["font_size"]
            font_color_hex = fmt["font_color"]
            bold = fmt.get("bold", False)

            mapped_font = format_engine.resolve_font(font_name)
            display_font = mapped_font or font_name
            cjk = _has_cjk(translated)
            pymupdf_font = _resolve_font(display_font, cjk)

            block_info_list.append({
                "key": key,
                "translated": translated,
                "rect": rect,
                "font_size": font_size,
                "font_color_hex": font_color_hex,
                "font_color_tuple": _color_hex_to_tuple(font_color_hex),
                "bold": bold,
                "pymupdf_font": pymupdf_font,
                "cjk": cjk,
            })

        # ---- 第一步：Redaction（擦除原文，透明背景）----
        # 📘 v4 关键：fill=False → 透明背景
        # 之前 fill=(1,1,1) 会在图片/彩色背景上产生白色方块
        # fill=False 只删除文字，不填充任何颜色，保持原始背景
        for info in block_info_list:
            redact_rect = info["rect"] + (-1, -1, 1, 1)
            page.add_redact_annot(redact_rect, text="", fill=False)

        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # ---- 第二步：写入译文（严格原位）----
        for info in block_info_list:
            key = info["key"]
            translated = info["translated"]
            rect = info["rect"]
            success = False

            # 📘 写入区域 = 原始 bbox + 微小余量
            # 不扩展！宁可缩小字号，也不破坏排版
            write_rect = rect + (-0.5, -0.5, 0.5, 0.5)

            # 方案 A：insert_htmlbox（自动缩放，最可靠）
            try:
                html = _build_html_text(
                    translated, info["font_size"],
                    info["font_color_hex"], info["bold"],
                )
                result = page.insert_htmlbox(
                    write_rect, html, scale_low=0, overlay=True
                )
                spare_height = result[0] if isinstance(result, tuple) else result
                if spare_height >= 0:
                    success = True
                    if isinstance(result, tuple) and result[1] < 0.3:
                        logger.debug(
                            f"文本大幅缩放 key={key}, scale={result[1]:.2f}, "
                            f"text={translated[:20]}"
                        )
            except Exception as e:
                logger.debug(f"insert_htmlbox 异常 key={key}: {e}")

            # 方案 B：insert_textbox 降级
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
                        # 缩小到一半再试
                        half_size = max(4.0, info["font_size"] * 0.5)
                        rc2 = page.insert_textbox(
                            write_rect, translated,
                            fontsize=half_size,
                            fontname=info["pymupdf_font"],
                            color=info["font_color_tuple"],
                            align=fitz.TEXT_ALIGN_LEFT,
                            overlay=True,
                        )
                        if rc2 >= 0:
                            success = True
                except Exception as e:
                    logger.debug(f"insert_textbox 降级异常 key={key}: {e}")

            # 方案 C：最小字号兜底
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
                    f"rect={rect.width:.0f}x{rect.height:.0f}, "
                    f"text={translated[:30]}"
                )

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

    if failed_count:
        logger.warning(
            f"PDF 生成完成: {output_path}"
            f"（成功 {replaced_count}，失败 {failed_count}）"
        )
    else:
        logger.info(
            f"PDF 生成完成: {output_path}"
            f"（成功写入 {replaced_count} 个文本块）"
        )
