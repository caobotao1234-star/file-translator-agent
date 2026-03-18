# translator/scan_writer.py
# =============================================================
# 📘 教学笔记：扫描件 PDF → Word 写入器（v7.1 — 精确布局还原）
# =============================================================
# 📘 v7.1 改进：
#   - 每个单元格独立控制四条边框（per-cell borders）
#   - 每行文字独立对齐（per-line alignment）
#   - 图片按 image_position 放在文字前/后/中间
#   - 竖版文字支持（vertical text direction）
#   - 精确列宽比例
# =============================================================

import io
import os
from typing import Dict, Any, List
from docx import Document
from docx.shared import Pt, Cm, Emu, RGBColor
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml
from core.logger import get_logger
from translator.format_engine import FormatEngine

logger = get_logger("scan_writer")

# 📘 页面可用宽度（A4 纸宽 21cm - 左右边距各 1.5cm = 18cm）
PAGE_CONTENT_WIDTH_CM = 18.0


def _set_cell_borders(cell, top=None, bottom=None, left=None, right=None):
    """
    📘 设置单元格边框
    每个参数是 dict: {"sz": 4, "val": "single", "color": "000000"}
    或 None 表示无边框
    """
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    borders_xml = f'<w:tcBorders {nsdecls("w")}>'
    for edge, style in [("top", top), ("left", left), ("bottom", bottom), ("right", right)]:
        if style:
            borders_xml += (
                f'<w:{edge} w:val="{style.get("val", "single")}" '
                f'w:sz="{style.get("sz", 4)}" w:space="0" '
                f'w:color="{style.get("color", "000000")}"/>'
            )
        else:
            borders_xml += f'<w:{edge} w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
    borders_xml += '</w:tcBorders>'
    tcBorders = parse_xml(borders_xml)
    tcPr.append(tcBorders)


def _set_cell_borders_from_dict(cell, borders: dict):
    """
    📘 v7.1 新增：从 Vision LLM 返回的 borders dict 设置边框
    borders: {"top": true/false, "bottom": true/false, "left": true/false, "right": true/false}
    """
    border_on = {"sz": 4, "val": "single", "color": "000000"}
    _set_cell_borders(
        cell,
        top=border_on if borders.get("top", True) else None,
        bottom=border_on if borders.get("bottom", True) else None,
        left=border_on if borders.get("left", True) else None,
        right=border_on if borders.get("right", True) else None,
    )


def _set_cell_vertical_text(cell):
    """
    📘 v7.1 新增：设置单元格文字方向为竖版（从上到下）

    📘 教学笔记：
    Word 中竖版文字通过 tcPr 的 textDirection 属性实现。
    btLr = Bottom to Top, Left to Right（竖版，从下到上读）
    tbRl = Top to Bottom, Right to Left（竖版，从上到下读，东亚竖排）
    """
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    text_dir = parse_xml(f'<w:textDirection {nsdecls("w")} w:val="tbRlV"/>')
    tcPr.append(text_dir)


def _set_cell_width(cell, width_cm: float):
    """📘 设置单元格宽度"""
    width_emu = int(width_cm * 360000)  # 1cm = 360000 EMU
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    tcW = parse_xml(f'<w:tcW {nsdecls("w")} w:w="{width_emu}" w:type="dxa"/>')
    tcPr.append(tcW)


def _get_alignment(align_str: str):
    """📘 字符串 → WD_ALIGN_PARAGRAPH 枚举"""
    mapping = {
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
        "left": WD_ALIGN_PARAGRAPH.LEFT,
    }
    return mapping.get(align_str, WD_ALIGN_PARAGRAPH.LEFT)


def _get_font_size(size_str: str) -> float:
    """📘 字号描述 → pt 值"""
    mapping = {"small": 8, "normal": 10.5, "large": 14, "title": 18}
    return mapping.get(size_str, 10.5)


def _set_run_font(run, bold: bool = False, size_pt: float = 10, font_name: str = "Microsoft YaHei"):
    """📘 设置 run 的字体属性"""
    run.font.size = Pt(size_pt)
    run.font.name = font_name
    run.font.bold = bold
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)


def _add_image_to_cell(cell, image_bytes: bytes, align: str = "center"):
    """
    📘 v7 新增：把裁剪好的图片嵌入到表格单元格中
    """
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(image_bytes))
        w_px, h_px = img.size
        max_w_cm = 4.0
        w_cm = min(max_w_cm, w_px * 2.54 / 200)

        para = cell.add_paragraph()
        para.alignment = _get_alignment(align)
        para.paragraph_format.space_before = Pt(2)
        para.paragraph_format.space_after = Pt(2)
        run = para.add_run()
        img_stream = io.BytesIO(image_bytes)
        run.add_picture(img_stream, width=Cm(w_cm))
        return para
    except Exception as e:
        logger.warning(f"单元格图片嵌入失败: {e}")
        return None


def _add_table_to_doc(doc: Document, table_data: dict, translations: Dict[str, str],
                      page_idx: int, elem_idx: int):
    """
    📘 教学笔记：把结构化表格数据写入 Word 文档（v7.1 完整版）

    📘 v7.1 全部功能：
    1. 每个单元格独立控制四条边框（per-cell borders）
    2. 每行文字独立对齐（per-line alignment via "lines" array）
    3. 图片按 image_position 放在文字前/后
    4. 竖版文字支持（vertical text direction）
    5. 精确列宽比例
    """
    rows_data = table_data.get("rows", [])
    if not rows_data:
        return

    col_widths_pct = table_data.get("col_widths", [])

    # 📘 计算实际列数（考虑 colspan）
    max_cols = 0
    for row in rows_data:
        cells = row.get("cells", row) if isinstance(row, dict) else row
        if isinstance(cells, dict):
            cells = cells.get("cells", [])
        col_count = sum(cell.get("colspan", 1) for cell in cells)
        max_cols = max(max_cols, col_count)

    num_rows = len(rows_data)
    if max_cols == 0 or num_rows == 0:
        return

    # 📘 计算列宽（cm）
    if col_widths_pct and len(col_widths_pct) == max_cols:
        total_pct = sum(col_widths_pct)
        col_widths_cm = [PAGE_CONTENT_WIDTH_CM * p / total_pct for p in col_widths_pct]
    else:
        col_widths_cm = [PAGE_CONTENT_WIDTH_CM / max_cols] * max_cols

    # 创建表格
    table = doc.add_table(rows=num_rows, cols=max_cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # 📘 设置表格总宽度
    tbl = table._tbl
    tblPr = tbl.tblPr if tbl.tblPr is not None else parse_xml(f'<w:tblPr {nsdecls("w")}/>') 
    total_width_twips = int(PAGE_CONTENT_WIDTH_CM * 567)
    tblW = parse_xml(f'<w:tblW {nsdecls("w")} w:w="{total_width_twips}" w:type="dxa"/>')
    tblPr.append(tblW)

    # 📘 跟踪被 rowspan 占用的位置
    occupied = [[False] * max_cols for _ in range(num_rows)]

    for row_idx, row in enumerate(rows_data):
        cells = row.get("cells", row) if isinstance(row, dict) else row
        if isinstance(cells, dict):
            cells = cells.get("cells", [])

        col_cursor = 0
        cell_data_idx = 0

        for cell_data in cells:
            while col_cursor < max_cols and occupied[row_idx][col_cursor]:
                col_cursor += 1
            if col_cursor >= max_cols:
                break

            colspan = cell_data.get("colspan", 1)
            rowspan = cell_data.get("rowspan", 1)
            cell_bold = cell_data.get("bold", False)
            cell_align = cell_data.get("align", "left")
            is_vertical = cell_data.get("vertical", False)

            # 📘 获取文字内容：支持 "lines" 数组（per-line alignment）或 "text" 简写
            cell_lines = cell_data.get("lines")
            if cell_lines and isinstance(cell_lines, list):
                original_text = "\n".join(l.get("text", "") for l in cell_lines)
            else:
                original_text = cell_data.get("text", "").strip()
                cell_lines = None

            # 📘 查找译文
            key = f"pg{page_idx}_e{elem_idx}_r{row_idx}_c{cell_data_idx}"
            translated = translations.get(key, original_text)

            # 获取单元格
            cell = table.cell(row_idx, col_cursor)

            # 📘 合并单元格
            if colspan > 1 or rowspan > 1:
                end_row = min(row_idx + rowspan - 1, num_rows - 1)
                end_col = min(col_cursor + colspan - 1, max_cols - 1)
                try:
                    cell = cell.merge(table.cell(end_row, end_col))
                except Exception as e:
                    logger.debug(f"合并失败 [{row_idx},{col_cursor}]->[{end_row},{end_col}]: {e}")

            # 📘 竖版文字
            if is_vertical:
                _set_cell_vertical_text(cell)

            # 📘 图片处理
            has_image = cell_data.get("has_image", False)
            cropped_image = cell_data.get("cropped_image")
            image_position = cell_data.get("image_position", "after")

            # 📘 写入内容
            cell.text = ""

            # 📘 图片在文字前面
            if has_image and cropped_image and image_position == "before":
                _add_image_to_cell(cell, cropped_image, cell_align)

            # 📘 写入文字（支持 per-line alignment）
            trans_lines = translated.split("\n")
            if cell_lines and len(cell_lines) == len(trans_lines):
                # 📘 per-line alignment：每行用原文的对齐方式
                for line_idx, (trans_text, orig_line) in enumerate(zip(trans_lines, cell_lines)):
                    line_align = orig_line.get("align", cell_align)
                    if line_idx == 0:
                        para = cell.paragraphs[0]
                    else:
                        para = cell.add_paragraph()
                    para.alignment = _get_alignment(line_align)
                    para.paragraph_format.space_before = Pt(0)
                    para.paragraph_format.space_after = Pt(1)
                    run = para.add_run(trans_text.strip())
                    _set_run_font(run, bold=cell_bold, size_pt=10)
            else:
                # 📘 简写模式：所有行用同一个对齐
                for line_idx, line_text in enumerate(trans_lines):
                    if line_idx == 0:
                        para = cell.paragraphs[0]
                    else:
                        para = cell.add_paragraph()
                    para.alignment = _get_alignment(cell_align)
                    para.paragraph_format.space_before = Pt(0)
                    para.paragraph_format.space_after = Pt(1)
                    run = para.add_run(line_text.strip())
                    _set_run_font(run, bold=cell_bold, size_pt=10)

            # 📘 图片在文字后面（默认）
            if has_image and cropped_image and image_position != "before":
                _add_image_to_cell(cell, cropped_image, cell_align)

            # 📘 设置列宽（仅第一行设置即可）
            if row_idx == 0 and col_cursor < len(col_widths_cm):
                w = sum(col_widths_cm[col_cursor:col_cursor + colspan])
                _set_cell_width(cell, w)

            # 📘 v7.1: 每个单元格独立控制边框
            borders = cell_data.get("borders")
            if borders and isinstance(borders, dict):
                _set_cell_borders_from_dict(cell, borders)
            else:
                border_on = {"sz": 4, "val": "single", "color": "000000"}
                _set_cell_borders(cell, top=border_on, bottom=border_on,
                                  left=border_on, right=border_on)

            # 标记被占用的位置
            for r in range(row_idx, min(row_idx + rowspan, num_rows)):
                for c in range(col_cursor, min(col_cursor + colspan, max_cols)):
                    if r != row_idx or c != col_cursor:
                        occupied[r][c] = True

            col_cursor += colspan
            cell_data_idx += 1

    # 表格后加间距
    doc.add_paragraph()


def _add_paragraph_to_doc(doc: Document, elem: dict, translations: Dict[str, str],
                          page_idx: int, elem_idx: int):
    """📘 把段落文本写入 Word 文档（带格式）"""
    key = f"pg{page_idx}_e{elem_idx}_para"
    original_text = elem.get("text", "").strip()
    translated = translations.get(key, original_text)

    para = doc.add_paragraph()
    para.alignment = _get_alignment(elem.get("align", "left"))
    run = para.add_run(translated)
    size_pt = _get_font_size(elem.get("font_size", "normal"))
    _set_run_font(run, bold=elem.get("bold", False), size_pt=size_pt)


def _add_image_to_doc(doc: Document, elem: dict):
    """
    📘 教学笔记：嵌入裁剪的图片到 Word 文档

    如果 Vision LLM 返回了 bbox_pct 且裁剪成功，
    elem["cropped_image"] 里就有裁剪后的 JPEG bytes。
    直接嵌入 Word，宽度按裁剪图片的宽高比自适应。
    """
    cropped = elem.get("cropped_image")
    description = elem.get("description", "图片区域")

    if cropped:
        para = doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = para.add_run()
        img_stream = io.BytesIO(cropped)
        # 📘 图片宽度限制在 8cm 以内，避免太大
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(cropped))
            w_px, h_px = img.size
            # 按比例缩放，最大宽度 8cm
            max_w_cm = 8.0
            aspect = h_px / w_px if w_px > 0 else 1
            w_cm = min(max_w_cm, w_px * 2.54 / 200)  # 200 DPI
            run.add_picture(img_stream, width=Cm(w_cm))
        except Exception:
            run.add_picture(img_stream, width=Cm(6))
    else:
        # 📘 没有裁剪图片，用文字占位
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
    📘 扫描件写入主函数（v7.1 — 精确布局还原）

    生成 Word 文档，每页包含：
    1. 原始页面图片（参考）
    2. 结构化译文（per-cell 边框、per-line 对齐、图片位置、竖版文字）
    3. 分页符
    """
    page_structures = parsed_data.get("page_structures", [])
    page_images = parsed_data.get("page_images", [])

    if not page_structures:
        raise ValueError("parsed_data 中缺少 page_structures")

    # 📘 输出路径强制 .docx
    base, ext = os.path.splitext(output_path)
    if ext.lower() != ".docx":
        output_path = base + ".docx"

    logger.info(f"开始生成扫描件 Word 文档: {output_path}")
    print(f"[📝 扫描件写入] 生成 Word 文档（精确布局）...", flush=True)

    doc = Document()

    # 📘 默认字体
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Microsoft YaHei"
    font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

    # 📘 窄边距
    for section in doc.sections:
        section.top_margin = Cm(1.5)
        section.bottom_margin = Cm(1.5)
        section.left_margin = Cm(1.5)
        section.right_margin = Cm(1.5)

    translated_count = 0

    for page_idx, structure in enumerate(page_structures):
        if page_idx > 0:
            doc.add_page_break()

        # 📘 页面标题
        heading = doc.add_heading(f"第 {page_idx + 1} 页", level=2)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # 📘 嵌入原始页面图片（参考）
        if page_idx < len(page_images) and page_images[page_idx]:
            img_stream = io.BytesIO(page_images[page_idx])
            para = doc.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = para.add_run()
            run.add_picture(img_stream, width=Cm(16))

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
                for row_idx, row in enumerate(elem.get("rows", [])):
                    cells = row.get("cells", row) if isinstance(row, dict) else row
                    if isinstance(cells, dict):
                        cells = cells.get("cells", [])
                    for col_idx, cell in enumerate(cells):
                        key = f"pg{page_idx}_e{elem_idx}_r{row_idx}_c{col_idx}"
                        if key in translations:
                            translated_count += 1

            elif elem_type == "paragraph":
                _add_paragraph_to_doc(doc, elem, translations, page_idx, elem_idx)
                key = f"pg{page_idx}_e{elem_idx}_para"
                if key in translations:
                    translated_count += 1

            elif elem_type == "image_region":
                _add_image_to_doc(doc, elem)

    doc.save(output_path)

    logger.info(f"扫描件 Word 文档生成完成: {output_path} (翻译 {translated_count} 个单元)")
    print(f"[✅ 扫描件写入完成] 生成 Word 文档，翻译了 {translated_count} 个单元", flush=True)

    return output_path
