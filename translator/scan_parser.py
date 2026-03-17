# translator/scan_parser.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 解析器（v5 — Vision LLM 结构化识别）
# =============================================================
# 核心思路：把页面图片发给 Vision LLM，让它输出结构化 JSON，
# 包括表格的行列结构、列宽比例、边框样式、合并单元格，
# 以及图片区域的坐标（用于裁剪 LOGO/二维码/印章等）。
#
# 📘 这就是用户手工操作的自动化版本：
#   用户：截图 → 发给大模型 → 大模型返回表格 → 粘贴到 Word
#   Agent：渲染页面 → Vision LLM → 解析 JSON → 生成 Word
# =============================================================

import base64
import json
import os
import io
import fitz  # PyMuPDF
from PIL import Image
from typing import Dict, Any, List, Optional
from core.llm_engine import ArkLLMEngine
from core.logger import get_logger

logger = get_logger("scan_parser")

RENDER_DPI = 200
SCAN_THRESHOLD_BLOCKS_PER_PAGE = 2

# 📘 Vision LLM 的结构化识别 prompt（v5.1 — 精确布局）
# 要求模型输出表格列宽比例、边框样式、图片区域坐标
STRUCTURE_RECOGNITION_PROMPT = """\
你是一个专业的文档结构识别助手。请仔细观察这张文档图片，精确识别其中的所有内容和布局。

请输出一个 JSON 对象，格式如下：
{
  "page_type": "table" | "mixed" | "text",
  "elements": [
    {
      "type": "table",
      "col_widths": [30, 70],
      "border": "all" | "outer" | "none",
      "rows": [
        {
          "cells": [
            {"text": "单元格内容", "colspan": 1, "rowspan": 1, "bold": false, "align": "left"},
            ...
          ]
        },
        ...
      ]
    },
    {
      "type": "paragraph",
      "text": "段落文字内容",
      "bold": false,
      "align": "left",
      "font_size": "normal"
    },
    {
      "type": "image_region",
      "description": "简要描述（如：国徽图案、二维码、证件照片、红色印章）",
      "bbox_pct": [10, 5, 40, 25]
    }
  ]
}

详细规则：

【table 类型】
- col_widths: 每列宽度的百分比数组，总和应为100。例如 [25, 25, 25, 25] 表示4列等宽
- border: 边框样式
  - "all": 所有单元格都有边框线（最常见）
  - "outer": 只有表格外边框，内部无线
  - "none": 无边框（用于布局对齐的隐形表格）
- rows: 每行是一个对象，包含 cells 数组
- cells 中每个单元格：
  - text: 该格子里的完整文字（空格子设为 ""）
  - colspan: 横向合并列数，默认 1
  - rowspan: 纵向合并行数，默认 1
  - bold: 是否加粗
  - align: "left" | "center" | "right"
  - 被合并覆盖的单元格不要输出（跳过）

【paragraph 类型】
- text: 段落完整文字
- bold: 是否加粗
- align: "left" | "center" | "right"
- font_size: "small" | "normal" | "large" | "title"

【image_region 类型】— 非文字的图片区域（LOGO、二维码、印章、照片、徽标等）
- description: 简要描述内容
- bbox_pct: 图片区域在页面中的位置，格式 [left%, top%, right%, bottom%]
  - 百分比相对于整个页面，左上角为 [0,0]，右下角为 [100,100]
  - 例如 [5, 3, 35, 20] 表示左上角在页面 5%,3% 处，右下角在 35%,20% 处

【重要】
1. elements 按从上到下的顺序排列
2. 只输出 JSON，不要输出任何其他文字
3. 确保 JSON 格式正确可解析
4. 仔细观察哪些区域有边框线、哪些没有
5. 图片区域的 bbox_pct 要尽量准确，用于从原图裁剪"""



def detect_scan_pdf(filepath: str) -> bool:
    """
    📘 检测 PDF 是否为扫描件
    策略：每页平均文本块数 < 阈值 → 扫描件
    """
    try:
        doc = fitz.open(filepath)
        if len(doc) == 0:
            doc.close()
            return False

        total_blocks = 0
        for page in doc:
            blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            for block in blocks.get("blocks", []):
                if block.get("type") == 0:
                    has_text = False
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            if span.get("text", "").strip():
                                has_text = True
                                break
                        if has_text:
                            break
                    if has_text:
                        total_blocks += 1

        avg_blocks = total_blocks / len(doc)
        doc.close()

        is_scan = avg_blocks < SCAN_THRESHOLD_BLOCKS_PER_PAGE
        if is_scan:
            logger.info(
                f"检测为扫描件 PDF: 平均 {avg_blocks:.1f} 个文本块/页 "
                f"(阈值 {SCAN_THRESHOLD_BLOCKS_PER_PAGE})"
            )
        else:
            logger.debug(f"检测为普通 PDF: 平均 {avg_blocks:.1f} 个文本块/页")
        return is_scan

    except Exception as e:
        logger.error(f"扫描件检测失败: {e}")
        return False


def _render_page_to_base64(doc: fitz.Document, page_idx: int) -> str:
    """📘 把 PDF 页面渲染成 base64 JPEG"""
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("jpeg", jpg_quality=88)
    return base64.b64encode(img_bytes).decode("utf-8")


def _render_page_to_pil(doc: fitz.Document, page_idx: int) -> Image.Image:
    """📘 把 PDF 页面渲染成 PIL Image（用于裁剪图片区域）"""
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    return Image.open(io.BytesIO(img_bytes))


def _render_page_to_jpeg_bytes(doc: fitz.Document, page_idx: int) -> bytes:
    """📘 把 PDF 页面渲染成 JPEG bytes（用于嵌入 Word）"""
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("jpeg", jpg_quality=92)


def _crop_image_region(page_img: Image.Image, bbox_pct: List[float]) -> Optional[bytes]:
    """
    📘 教学笔记：从页面图片中裁剪指定区域

    bbox_pct 格式: [left%, top%, right%, bottom%]
    百分比相对于整个页面尺寸。

    返回裁剪后的 JPEG bytes，失败返回 None。
    """
    if not bbox_pct or len(bbox_pct) != 4:
        return None

    w, h = page_img.size
    left = int(w * bbox_pct[0] / 100)
    top = int(h * bbox_pct[1] / 100)
    right = int(w * bbox_pct[2] / 100)
    bottom = int(h * bbox_pct[3] / 100)

    # 安全边界
    left = max(0, min(left, w - 1))
    top = max(0, min(top, h - 1))
    right = max(left + 1, min(right, w))
    bottom = max(top + 1, min(bottom, h))

    if right - left < 5 or bottom - top < 5:
        return None

    try:
        cropped = page_img.crop((left, top, right, bottom))
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"图片裁剪失败: {e}")
        return None


def _call_vision_llm(vision_llm: ArkLLMEngine, image_b64: str, prompt: str) -> Optional[str]:
    """📘 调用 Vision LLM，发送图片 + 文字 prompt"""
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                },
                {"type": "text", "text": prompt},
            ],
        }
    ]

    full_text = ""
    try:
        for chunk in vision_llm.stream_chat(messages):
            if chunk["type"] == "text":
                full_text += chunk["content"]
    except Exception as e:
        logger.error(f"Vision LLM 调用失败: {e}")
        return None
    return full_text


def _parse_structure_json(response: str) -> Optional[dict]:
    """📘 从 Vision LLM 响应中提取 JSON 结构"""
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict) and "elements" in result:
            return result
    except json.JSONDecodeError:
        logger.warning(f"结构化 JSON 解析失败: {text[:300]}...")
    return None


def parse_scan_pdf(filepath: str, vision_llm: ArkLLMEngine = None) -> Dict[str, Any]:
    """
    📘 教学笔记：扫描件 PDF 解析主函数（v5.1 — 精确布局 + 图片裁剪）

    流程：
    1. 逐页渲染 PDF 为图片
    2. 发给 Vision LLM，获取结构化 JSON（含列宽、边框、图片坐标）
    3. 裁剪图片区域（LOGO/二维码/印章等）
    4. 提取翻译单元，生成标准 parsed_data
    """
    if vision_llm is None:
        raise ValueError(
            "扫描件翻译需要 Vision 模型。"
            "请在 GUI 的「排版审校」下拉框中选择一个多模态模型。"
        )

    logger.info(f"开始扫描件结构化识别: {filepath}")
    print(f"[🔍 扫描件识别] 正在用 Vision LLM 识别页面结构...", flush=True)

    doc = fitz.open(filepath)
    num_pages = len(doc)

    all_items = []
    page_structures = []
    page_images = []       # 每页的 JPEG bytes（整页参考图）
    page_pil_images = []   # 每页的 PIL Image（用于裁剪）

    for page_idx in range(num_pages):
        print(f"  [🔍 第 {page_idx + 1}/{num_pages} 页] 识别中...", flush=True)

        # 1. 渲染页面
        image_b64 = _render_page_to_base64(doc, page_idx)
        image_bytes = _render_page_to_jpeg_bytes(doc, page_idx)
        pil_img = _render_page_to_pil(doc, page_idx)
        page_images.append(image_bytes)
        page_pil_images.append(pil_img)

        # 2. 调用 Vision LLM
        response = _call_vision_llm(vision_llm, image_b64, STRUCTURE_RECOGNITION_PROMPT)
        if not response:
            logger.warning(f"第 {page_idx + 1} 页: Vision LLM 无响应")
            page_structures.append({"page_type": "text", "elements": []})
            continue

        # 3. 解析 JSON
        structure = _parse_structure_json(response)
        if not structure:
            logger.warning(f"第 {page_idx + 1} 页: 结构化解析失败，作为纯文本处理")
            page_structures.append({
                "page_type": "text",
                "elements": [{"type": "paragraph", "text": response}],
            })
        else:
            page_structures.append(structure)
            elem_count = len(structure.get("elements", []))
            logger.info(f"第 {page_idx + 1} 页: 识别到 {elem_count} 个元素 (类型: {structure.get('page_type', '?')})")

        # 4. 裁剪图片区域
        elements = structure.get("elements", []) if structure else []
        for elem_idx, elem in enumerate(elements):
            if elem.get("type") == "image_region":
                bbox_pct = elem.get("bbox_pct")
                if bbox_pct:
                    cropped_bytes = _crop_image_region(pil_img, bbox_pct)
                    if cropped_bytes:
                        elem["cropped_image"] = cropped_bytes
                        logger.debug(f"第 {page_idx+1} 页: 裁剪图片 '{elem.get('description', '?')}' ({len(cropped_bytes)} bytes)")

        # 5. 提取翻译单元
        for elem_idx, elem in enumerate(elements):
            elem_type = elem.get("type", "")

            if elem_type == "table":
                rows = elem.get("rows", [])
                for row_idx, row in enumerate(rows):
                    cells = row.get("cells", row) if isinstance(row, dict) else row
                    # 📘 兼容两种格式：row 可能是 {"cells": [...]} 或直接是 [...]
                    if isinstance(cells, dict):
                        cells = cells.get("cells", [])
                    for col_idx, cell in enumerate(cells):
                        cell_text = cell.get("text", "").strip()
                        if cell_text:
                            key = f"pg{page_idx}_e{elem_idx}_r{row_idx}_c{col_idx}"
                            all_items.append({
                                "key": key,
                                "type": "table_cell",
                                "full_text": cell_text,
                                "is_empty": False,
                                "dominant_format": {
                                    "font_name": "Unknown",
                                    "font_size": 10,
                                    "font_color": "#000000",
                                    "bold": cell.get("bold", False),
                                },
                            })

            elif elem_type == "paragraph":
                para_text = elem.get("text", "").strip()
                if para_text:
                    key = f"pg{page_idx}_e{elem_idx}_para"
                    all_items.append({
                        "key": key,
                        "type": "pdf_block",
                        "full_text": para_text,
                        "is_empty": False,
                        "dominant_format": {
                            "font_name": "Unknown",
                            "font_size": 11,
                            "font_color": "#000000",
                            "bold": elem.get("bold", False),
                        },
                    })

    doc.close()

    total = len(all_items)
    print(f"[🔍 识别完成] {total} 个翻译单元（{num_pages} 页）", flush=True)
    logger.info(f"扫描件解析完成: {total} 个翻译单元")

    return {
        "source": "scan_parser",
        "source_type": "scan",
        "filepath": filepath,
        "items": all_items,
        "page_structures": page_structures,
        "page_images": page_images,
    }
