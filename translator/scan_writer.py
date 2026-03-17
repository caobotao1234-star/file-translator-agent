# translator/scan_writer.py
# =============================================================
# 📘 教学笔记：扫描件 PDF → Word 写入器（v5 — 结构化重建）
# =============================================================
# v4 方案：在原图上擦除文字 + 重写译文（inpainting）
#   问题：字号不统一、位置不精确、不可编辑
#
# v5 方案：基于 Vision LLM 识别的结构，生成全新的 Word 文档
#   - 表格 → Word 表格（保持行列结构和合并单元格）
#   - 段落 → Word 段落
#   - 图片区域 → 嵌入原始页面截图
#   - 每页之间加分页符
#
# 📘 为什么输出 Word 而不是 PDF？
# 用户的实际需求是"可编辑的翻译文档"，Word 天然可编辑。
# 而且 python-docx 生成表格比在 PDF 上画表格简单得多。
# =============================================================

import io
import os
from typing import Dict, Any, List
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml
from core.logger import get_logger
from translator.format_engine import FormatEngine

logger = get_logger("scan_writer")


def _set_cell_border(cell, **kwargs):
    """
    📘 给 Word 表格单元格设置边框
    用法: _set_cell_border(cell, top={"sz": 4, "val": "single", "color": "000000"})
    """
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    tcBorders = parse_xml(f'<w:tcBorders {nsdecls("w")}></w:tcBorders>')
    for edge in ("top", "left", "bottom", "right"):
        if edge in kwargs:
            element = parse_xml(
                f'<w:{edge} {nsdecls("w")} '
                f'w:val="{kwargs[edge].get("val", "single")}" '
                f'w:sz="{kwargs[edge].get("sz", 4)}" '
                f'w:space="0" '
                f'w:color="{kwargs[edge].get("color", "000000")}"/>'
            )
            tcBorders.append(element)
    tcPr.append(tcBorders)


def _set_cell_shading(cell, color: str):
    """📘 设置单元格背景色"""
    shading = parse_xml(
        f'<w:shd {nsdecls("w")} w:fill="{color}" w:val="clear"/>'
    )
    cell._tc.get_or_add_tcPr().append(shading)


def _add_table_to_doc(doc: Document, table_data: dict, translations: Dict[str, str],
                      page_idx: int, elem_idx: int):
    """
    📘 教学笔记：把结构化表格数据写入 Word 文档

    处理合并单元格的策略：
    1. 先创建一个 max_rows × max_cols 的完整表格
    2. 遍历结构化数据，填入译文
    3. 对 colspan > 1 或 rowspan > 1 的单元格执行合并
    """
    rows_data = table_data.get("rows", [])
    if not rows_data:
        return

    # 📘 计算表格实际列数（考虑 colspan）
    max_cols = 0
    for row in rows_data:
        col_count = sum(cell.get("colspan", 1) for cell in row)
        max_cols = max(max_cols, col_count)

    num_rows = len(rows_data)
    if max_cols == 0 or num_rows == 0:
        return

    # 创建表格
    table = doc.add_table(rows=num_rows, cols=max_cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"

    # 📘 跟踪哪些单元格被 rowspan 占用
    # occupied[row][col] = True 表示该位置被上方的 rowspan 覆盖
    occupied = [[False] * max_cols for _ in range(num_rows)]

    for row_idx, row_data in enumerate(rows_data):
        col_cursor = 0  # 当前列位置
        cell_data_idx = 0  # 当前处理的 cell 数据索引

        for cell_data in row_data:
            # 跳过被 rowspan 占用的列
            while col_cursor < max_cols and occupied[row_idx][col_cursor]:
                col_cursor += 1

            if col_cursor >= max_cols:
                break

            cell_text = cell_data.get("text", "").strip()
            colspan = cell_data.get("colspan", 1)
            rowspan = cell_data.get("rowspan", 1)

            # 📘 查找译文
            key = f"pg{page_idx}_e{elem_idx}_r{row_idx}_c{cell_data_idx}"
            translated = translations.get(key, cell_text)

            # 写入单元格
            cell = table.cell(row_idx, col_cursor)
            cell.text = translated
            # 设置字体
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in paragraph.runs:
                    run.font.size = Pt(10)
                    run.font.name = "Microsoft YaHei"
                    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

            # 📘 合并单元格
            if colspan > 1 or rowspan > 1:
                end_row = min(row_idx + rowspan - 1, num_rows - 1)
                end_col = min(col_cursor + colspan - 1, max_cols - 1)
                try:
                    merge_cell = table.cell(end_row, end_col)
                    cell.merge(merge_cell)
                except Exception as e:
                    logger.debug(f"单元格合并失败 [{row_idx},{col_cursor}]->[{end_row},{end_col}]: {e}")

            # 标记被占用的位置
            for r in range(row_idx, min(row_idx + rowspan, num_rows)):
                for c in range(col_cursor, min(col_cursor + colspan, max_cols)):
                    if r != row_idx or c != col_cursor:
                        occupied[r][c] = True

            col_cursor += colspan
            cell_data_idx += 1

    # 📘 表格后加一个空段落作为间距
    doc.add_paragraph()


def _add_paragraph_to_doc(doc: Document, elem: dict, translations: Dict[str, str],
                          page_idx: int, elem_idx: int):
    """📘 把段落文本写入 Word 文档"""
    key = f"pg{page_idx}_e{elem_idx}_para"
    original_text = elem.get("text", "").strip()
    translated = translations.get(key, original_text)

    para = doc.add_paragraph()
    run = para.add_run(translated)
    run.font.size = Pt(11)
    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")


def _add_image_to_doc(doc: Document, page_image_bytes: bytes, elem: dict):
    """
    📘 把原始页面图片嵌入 Word 文档（用于图片区域）

    📘 教学笔记：为什么嵌入整页图片而不是裁剪？
    Vision LLM 返回的是语义描述（"红色印章"），没有精确坐标。
    裁剪需要坐标，而我们没有。所以嵌入整页图片作为参考。
    但如果同一页已经嵌入过图片，就不重复嵌入了。
    """
    description = elem.get("description", "图片区域")
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run(f"[{description}]")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(128, 128, 128)
    run.italic = True


def write_scan_pdf(
    parsed_data: Dict[str, Any],
    translations: Dict[str, str],
    output_path: str,
    format_engine: FormatEngine,
    source_path: str = None,
    layout_overrides: Dict[str, dict] = None,
    scan_mode: str = "adaptive",
    fixed_fontsize: float = 10.0,
):
    """
    📘 教学笔记：扫描件写入主函数（v5 — 生成 Word 文档）

    📘 核心思路：
    不在原图上修改，而是基于 Vision LLM 识别的结构，
    生成一个全新的 Word 文档。每页包含：
    1. 页面标题（"第 X 页"）
    2. 原始页面图片（作为参考）
    3. 结构化内容（表格/段落的译文）
    4. 分页符

    输出路径自动改为 .docx（即使传入的是 .pdf 路径）
    """
    page_structures = parsed_data.get("page_structures", [])
    page_images = parsed_data.get("page_images", [])

    if not page_structures:
        raise ValueError("parsed_data 中缺少 page_structures，请确认使用了 v5 scan_parser")

    # 📘 输出路径强制改为 .docx
    base, ext = os.path.splitext(output_path)
    if ext.lower() != ".docx":
        output_path = base + ".docx"

    logger.info(f"开始生成扫描件 Word 文档: {output_path}")
    print(f"[📝 扫描件写入] 生成 Word 文档...", flush=True)

    doc = Document()

    # 📘 设置默认字体
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Microsoft YaHei"
    font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

    # 📘 设置页边距（窄边距，给表格更多空间）
    for section in doc.sections:
        section.top_margin = Cm(1.5)
        section.bottom_margin = Cm(1.5)
        section.left_margin = Cm(1.5)
        section.right_margin = Cm(1.5)

    translated_count = 0

    for page_idx, structure in enumerate(page_structures):
        if page_idx > 0:
            # 📘 分页符
            doc.add_page_break()

        # 📘 页面标题
        heading = doc.add_heading(f"第 {page_idx + 1} 页", level=2)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # 📘 嵌入原始页面图片（作为参考）
        if page_idx < len(page_images) and page_images[page_idx]:
            img_stream = io.BytesIO(page_images[page_idx])
            para = doc.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = para.add_run()
            # 📘 图片宽度设为页面宽度的 90%（约 16cm）
            run.add_picture(img_stream, width=Cm(16))

            # 分隔线
            sep = doc.add_paragraph()
            sep.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = sep.add_run("— 以下为译文 —")
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(128, 128, 128)

        # 📘 按结构写入内容
        elements = structure.get("elements", [])
        for elem_idx, elem in enumerate(elements):
            elem_type = elem.get("type", "")

            if elem_type == "table":
                _add_table_to_doc(doc, elem, translations, page_idx, elem_idx)
                # 统计翻译数
                for row_idx, row in enumerate(elem.get("rows", [])):
                    for col_idx, cell in enumerate(row):
                        key = f"pg{page_idx}_e{elem_idx}_r{row_idx}_c{col_idx}"
                        if key in translations:
                            translated_count += 1

            elif elem_type == "paragraph":
                _add_paragraph_to_doc(doc, elem, translations, page_idx, elem_idx)
                key = f"pg{page_idx}_e{elem_idx}_para"
                if key in translations:
                    translated_count += 1

            elif elem_type == "image_region":
                _add_image_to_doc(doc, page_images[page_idx] if page_idx < len(page_images) else b"", elem)

    doc.save(output_path)

    logger.info(f"扫描件 Word 文档生成完成: {output_path} (翻译 {translated_count} 个单元)")
    print(f"[✅ 扫描件写入完成] 生成 Word 文档，翻译了 {translated_count} 个单元", flush=True)

    return output_path
