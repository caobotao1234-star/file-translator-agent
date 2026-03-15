# translator/pdf_writer.py
import fitz  # PyMuPDF
import re
from typing import Dict, Any, List, Optional
from translator.format_engine import FormatEngine
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：PDF 文档生成器（Redaction 策略）
# =============================================================
# PDF 不像 Word/PPT 那样可以直接修改段落文本。
# PDF 的文字是"画"上去的，修改需要：
#   1. 用 Redaction（涂抹）擦掉原文区域
#   2. 在同一位置重新写入译文
#
# PyMuPDF 的 Redaction 流程：
#   page.add_redact_annot(rect, ...) → 标记要擦除的区域
#   page.apply_redactions()          → 执行擦除（不可逆）
#   page.insert_textbox(rect, ...)   → 在擦除后的区域写入新文本
#
# 难点：
#   - 字体嵌入：PDF 可能用了特殊字体，PyMuPDF 写入时不一定有
#   - 文本溢出：译文比原文长时需要缩小字号
#   - 多行文本：需要保持行间距和对齐方式
#   - 颜色还原：需要把整数颜色转回 RGB 元组
# =============================================================

logger = get_logger("pdf_writer")

# PyMuPDF 内置的通用字体映射
# PDF 原始字体名 → PyMuPDF 可用的基础字体
# 📘 教学笔记：PDF 字体困境
# PDF 文件可能嵌入了任意字体（如"思源黑体""微软雅黑"），
# 但 PyMuPDF 写入时只能用它内置的基础字体或系统已安装的字体。
# 对于中文，我们用 "china-s"（简体中文）系列字体。
FALLBACK_FONT_MAP = {
    # 中文字体 → PyMuPDF 中文字体标识
    "SimSun": "china-ss",       # 宋体
    "SimHei": "china-ss",       # 黑体
    "Microsoft YaHei": "china-ss",
    "KaiTi": "china-ss",
    "FangSong": "china-ss",
    "DengXian": "china-ss",     # 等线
    # 英文字体 → PyMuPDF 基础字体
    "Times New Roman": "tiro",
    "Arial": "helv",
    "Calibri": "helv",
    "Helvetica": "helv",
    "Courier": "cour",
    "Courier New": "cour",
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
    """
    将 PDF 原始字体名映射为 PyMuPDF 可用的字体。

    📘 教学笔记：字体回退策略
    1. 先查 FALLBACK_FONT_MAP 精确匹配
    2. 如果原始字体名包含中文关键词，用中文字体
    3. 如果文本包含 CJK 字符，用中文字体
    4. 兜底用 helv（Helvetica）
    """
    # 精确匹配
    if font_name in FALLBACK_FONT_MAP:
        return FALLBACK_FONT_MAP[font_name]

    # 模糊匹配：字体名中包含关键词
    name_lower = font_name.lower()
    for key, val in FALLBACK_FONT_MAP.items():
        if key.lower() in name_lower:
            return val

    # CJK 内容用中文字体
    if has_cjk:
        return "china-ss"

    return "helv"


def _has_cjk(text: str) -> bool:
    """检测文本是否包含 CJK 字符"""
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3040' <= ch <= '\u30ff' or '\uac00' <= ch <= '\ud7af':
            return True
    return False


def _calc_fit_fontsize(text: str, rect: fitz.Rect, fontname: str,
                       base_size: float, min_size: float = 5.0) -> float:
    """
    📘 教学笔记：自动缩小字号以适应矩形区域
    PyMuPDF 的 insert_textbox 返回值 < 0 表示文本溢出。
    我们从原始字号开始尝试，每次缩小 0.5pt，直到不溢出或达到下限。
    """
    size = base_size
    while size > min_size:
        # 用临时页面测试是否溢出
        test_doc = fitz.open()
        test_page = test_doc.new_page(width=rect.width + 100, height=rect.height + 100)
        test_rect = fitz.Rect(10, 10, 10 + rect.width, 10 + rect.height)
        rc = test_page.insert_textbox(
            test_rect, text, fontsize=size, fontname=fontname,
        )
        test_doc.close()
        if rc >= 0:  # 没溢出
            return size
        size -= 0.5
    return min_size


def write_pdf(
    parsed_data: Dict[str, Any],
    translations: Dict[str, str],
    output_path: str,
    format_engine: FormatEngine,
    source_path: str = None,
):
    """
    基于原 PDF 生成翻译后的文件。

    策略：逐页处理，对每个已翻译的 Block：
    1. 用 Redaction 擦除原文区域
    2. 在同一区域写入译文，保持字体/大小/颜色
    3. 如果译文溢出，自动缩小字号

    📘 教学笔记：为什么不能一次性 apply_redactions？
    因为 apply_redactions 会改变页面内容，后续的 search_for 可能失效。
    所以我们按页处理：先收集该页所有要替换的 Block，一次性 redact，再逐个写入。
    """
    if not source_path:
        raise ValueError("source_path is required")

    logger.info(f"开始生成 PDF: {output_path}")
    doc = fitz.open(source_path)

    # 按页分组
    page_items = {}
    for item in parsed_data["items"]:
        key = item["key"]
        if key not in translations:
            continue
        page_idx = int(key.split("_")[0][2:])  # "pg0_b2" → 0
        if page_idx not in page_items:
            page_items[page_idx] = []
        page_items[page_idx].append(item)

    replaced_count = 0

    for page_idx, items in page_items.items():
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]

        # ---- 第一步：添加 Redaction 标注（擦除原文）----
        for item in items:
            bbox = item["bbox"]
            rect = fitz.Rect(bbox)
            # 稍微扩大擦除区域，确保完全覆盖
            rect = rect + (-1, -1, 1, 1)
            page.add_redact_annot(rect, text="", fill=(1, 1, 1))  # 白色填充

        # 执行擦除
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # ---- 第二步：写入译文 ----
        for item in items:
            key = item["key"]
            translated = translations[key]
            bbox = item["bbox"]
            rect = fitz.Rect(bbox)
            fmt = item["dominant_format"]

            font_name = fmt["font_name"]
            font_size = fmt["font_size"]
            font_color = _color_hex_to_tuple(fmt["font_color"])

            # 通过 FormatEngine 映射字体
            mapped_font = format_engine.resolve_font(font_name)
            display_font = mapped_font or font_name

            # 解析为 PyMuPDF 可用字体
            cjk = _has_cjk(translated)
            pymupdf_font = _resolve_font(display_font, cjk)

            # 自动缩小字号以适应区域
            fit_size = _calc_fit_fontsize(translated, rect, pymupdf_font, font_size)

            # 写入译文
            try:
                page.insert_textbox(
                    rect,
                    translated,
                    fontsize=fit_size,
                    fontname=pymupdf_font,
                    color=font_color,
                    align=fitz.TEXT_ALIGN_LEFT,
                )
                replaced_count += 1
            except Exception as e:
                logger.warning(f"写入失败 key={key}: {e}，尝试降级写入")
                try:
                    # 降级：用最基础的字体
                    fallback = "china-ss" if cjk else "helv"
                    page.insert_textbox(
                        rect, translated, fontsize=fit_size,
                        fontname=fallback, color=font_color,
                    )
                    replaced_count += 1
                except Exception as e2:
                    logger.error(f"降级写入也失败 key={key}: {e2}")

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    logger.info(f"PDF 生成完成: {output_path}（替换了 {replaced_count} 个文本块）")
