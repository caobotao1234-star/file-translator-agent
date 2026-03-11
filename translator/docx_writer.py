# translator/docx_writer.py
import copy
from docx import Document
from docx.shared import Pt, RGBColor
from docx.oxml.ns import qn
from typing import List, Dict, Any, Optional
from translator.format_engine import FormatEngine
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：Word 文档生成器（v2 — 基于原文档克隆）
# =============================================================
# v1 的问题：
#   - 用 Document() 创建空白文档，标题会变蓝（Word 默认样式）
#   - 编号列表信息丢失（numbering 存在 XML 里，新文档没有）
#
# v2 的策略：
#   - 直接打开原文档，在原文档上修改
#   - 清空每个段落的 Run 文本，填入译文
#   - 这样段落的样式、编号、颜色等格式天然保留
#   - 只对字体名做映射替换（中文字体→英文字体）
#   - 最后另存为新文件（不覆盖原文件）
#
# 这个思路的好处：
#   - 格式保真度极高，因为我们没有"重建"任何东西
#   - 编号、列表、缩进、颜色全部自动继承
#   - 代码也更简单了
# =============================================================

logger = get_logger("docx_writer")


def _remap_run_font(run, format_engine: FormatEngine, style_name: str = None):
    """
    只替换 Run 的字体名（中文→英文映射），其他格式全部保留。
    """
    original_font = run.font.name
    if original_font:
        new_font = format_engine.resolve_font(original_font, style_name)
        if new_font:
            run.font.name = new_font
            # 同时设置东亚字体属性，确保 Word 正确渲染
            run._element.rPr.rFonts.set(qn('w:eastAsia'), new_font)


def write_docx(
    parsed_data: Dict[str, Any],
    translations: Dict[int, str],
    output_path: str,
    format_engine: FormatEngine,
    source_path: str = None,
):
    """
    基于原文档生成翻译后的 Word 文档。

    策略：打开原文档 → 逐段替换文本 → 映射字体 → 另存为新文件。
    段落结构、编号、颜色等格式全部从原文档继承。

    参数：
        parsed_data: docx_parser.parse_docx() 的返回值
        translations: {段落index: 翻译后的文本} 字典
        output_path: 输出文件路径
        format_engine: 格式映射引擎
        source_path: 原文档路径（用于克隆）
    """
    if not source_path:
        raise ValueError("source_path 不能为空，需要原文档来克隆格式")

    logger.info(f"开始生成文档（基于原文档克隆）: {output_path}")
    doc = Document(source_path)

    for para_data in parsed_data["paragraphs"]:
        idx = para_data["index"]

        # 跳过空段落和没有翻译的段落
        if para_data["is_empty"] or idx not in translations:
            continue

        # 安全检查：确保 index 没有越界
        if idx >= len(doc.paragraphs):
            logger.warning(f"段落索引 {idx} 超出文档范围，跳过")
            continue

        paragraph = doc.paragraphs[idx]
        translated_text = translations[idx]
        style_name = para_data["style"].get("style_name")

        # 获取原始 Run 信息
        original_runs = paragraph.runs

        if len(original_runs) == 0:
            # 没有 Run（罕见情况），直接加一个
            run = paragraph.add_run(translated_text)
            continue

        if len(original_runs) == 1:
            # 最常见：只有一个 Run，直接替换文本，格式自动保留
            original_runs[0].text = translated_text
            _remap_run_font(original_runs[0], format_engine, style_name)
        else:
            # 多个 Run（混排）：把译文放到第一个 Run，清空其余 Run
            # 这样第一个 Run 的格式（通常是段落主格式）会应用到整段译文
            original_runs[0].text = translated_text
            _remap_run_font(original_runs[0], format_engine, style_name)
            for run in original_runs[1:]:
                run.text = ""

    doc.save(output_path)
    logger.info(f"文档生成完成: {output_path}")
