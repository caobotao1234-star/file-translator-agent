# translator/docx_writer.py
import re
from docx import Document
from docx.shared import Pt, RGBColor
from docx.oxml.ns import qn
from typing import List, Dict, Any, Optional
from translator.format_engine import FormatEngine
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：Word 文档生成器（v3 — Run 级别格式保真）
# =============================================================
# v2 的问题：
#   多 Run 段落把译文全塞进第一个 Run，其余 Run 清空，
#   导致加粗、斜体、不同字体等混排格式丢失。
#
# v3 的策略：
#   - 译文带有 <r0>...</r0><r1>...</r1> 标记
#   - 用正则解析标记，拆分出每个 Run 的译文
#   - 按编号对应回原始 Run，替换文本，格式天然保留
#   - 如果 LLM 返回的标记数量不匹配，降级到 v2 策略（兜底）
# =============================================================

logger = get_logger("docx_writer")

# 匹配 <rN>内容</rN> 的正则
RUN_TAG_PATTERN = re.compile(r'<r(\d+)>(.*?)</r\1>', re.DOTALL)


def _parse_tagged_text(text: str) -> Optional[Dict[int, str]]:
    """
    解析带标记的译文，返回 {Run编号: 译文} 字典。

    输入: "<r0>Bold text</r0><r1>normal text</r1>"
    输出: {0: "Bold text", 1: "normal text"}

    如果文本不包含标记，返回 None。
    """
    matches = RUN_TAG_PATTERN.findall(text)
    if not matches:
        return None

    result = {}
    for idx_str, content in matches:
        result[int(idx_str)] = content
    return result


def _remap_run_font(run, format_engine: FormatEngine, style_name: str = None):
    """只替换 Run 的字体名（中文→英文映射），其他格式全部保留。"""
    original_font = run.font.name
    if original_font:
        new_font = format_engine.resolve_font(original_font, style_name)
        if new_font:
            run.font.name = new_font
            # 确保 rPr 存在
            if run._element.rPr is not None and run._element.rPr.rFonts is not None:
                run._element.rPr.rFonts.set(qn('w:eastAsia'), new_font)


def write_docx(
    parsed_data: Dict[str, Any],
    translations: Dict[int, str],
    output_path: str,
    format_engine: FormatEngine,
    source_path: str = None,
):
    """
    基于原文档生成翻译后的 Word 文档（Run 级别格式保真）。

    策略：
    - 单 Run 段落：直接替换文本
    - 多 Run 段落（带标记）：按标记拆分，逐 Run 替换
    - 标记解析失败时：降级为全文塞第一个 Run（兜底）
    """
    if not source_path:
        raise ValueError("source_path 不能为空，需要原文档来克隆格式")

    logger.info(f"开始生成文档（Run 级别格式保真）: {output_path}")
    doc = Document(source_path)

    for para_data in parsed_data["paragraphs"]:
        idx = para_data["index"]

        if para_data["is_empty"] or idx not in translations:
            continue

        if idx >= len(doc.paragraphs):
            logger.warning(f"段落索引 {idx} 超出文档范围，跳过")
            continue

        paragraph = doc.paragraphs[idx]
        translated_text = translations[idx]
        style_name = para_data["style"].get("style_name")
        is_tagged = para_data.get("tagged_text", False)
        original_runs = paragraph.runs

        if len(original_runs) == 0:
            paragraph.add_run(translated_text)
            continue

        # ---- 多 Run 段落：尝试按标记拆分 ----
        if is_tagged and len(original_runs) > 1:
            run_texts = _parse_tagged_text(translated_text)

            if run_texts is not None:
                # 收集原文档中非空 Run 的索引映射
                # parsed runs 的编号 → 实际 paragraph.runs 的索引
                non_empty_indices = []
                for ri, run in enumerate(original_runs):
                    if run.text:
                        non_empty_indices.append(ri)

                success = True
                for tag_idx, text in run_texts.items():
                    if tag_idx < len(non_empty_indices):
                        actual_idx = non_empty_indices[tag_idx]
                        original_runs[actual_idx].text = text
                        _remap_run_font(original_runs[actual_idx], format_engine, style_name)
                    else:
                        # 标记编号超出 Run 数量，降级
                        logger.warning(f"段落 {idx}: 标记 r{tag_idx} 超出 Run 范围，降级处理")
                        success = False
                        break

                if success:
                    # 清空没有对应标记的多余 Run
                    tagged_indices = set()
                    for tag_idx in run_texts.keys():
                        if tag_idx < len(non_empty_indices):
                            tagged_indices.add(non_empty_indices[tag_idx])
                    for ri, run in enumerate(original_runs):
                        if run.text and ri not in tagged_indices:
                            run.text = ""
                    continue

            # 标记解析失败，降级
            logger.warning(f"段落 {idx}: 标记解析失败，降级为整段替换")
            # 去掉残留的标记
            clean_text = RUN_TAG_PATTERN.sub(r'\2', translated_text)
            original_runs[0].text = clean_text
            _remap_run_font(original_runs[0], format_engine, style_name)
            for run in original_runs[1:]:
                run.text = ""
            continue

        # ---- 单 Run 段落或格式统一的多 Run：整段替换 ----
        # 去掉可能残留的标记（以防万一）
        clean_text = RUN_TAG_PATTERN.sub(r'\2', translated_text)
        original_runs[0].text = clean_text
        _remap_run_font(original_runs[0], format_engine, style_name)
        for run in original_runs[1:]:
            run.text = ""

    doc.save(output_path)
    logger.info(f"文档生成完成: {output_path}")
