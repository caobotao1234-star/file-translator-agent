# translator/scan_parser.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 解析器（v5 — Vision LLM 结构化识别）
# =============================================================
# v4 用 RapidOCR 做 OCR，能识别文字但不理解页面结构。
# 证件、表格类扫描件的核心挑战不是"识别文字"，而是"理解布局"：
#   - 哪些文字属于同一个表格？
#   - 表格有几行几列？哪些单元格合并了？
#   - 哪些区域是图片/印章/签名（不需要翻译）？
#
# v5 方案：直接把页面图片发给 Vision LLM，让它输出结构化 JSON。
# 这就是用户手工操作的自动化版本：
#   用户：截图 → 发给大模型 → 大模型返回表格 → 粘贴到 Excel/Word
#   Agent：渲染页面 → 发给 Vision LLM → 解析 JSON → 生成 Word
#
# 📘 为什么 Vision LLM 比 OCR 更适合这个场景？
# OCR 只能告诉你"这里有什么字"，不能告诉你"这些字组成了什么结构"。
# Vision LLM 能同时理解文字内容和视觉布局，一步到位。
# =============================================================

import base64
import json
import os
import fitz  # PyMuPDF
from typing import Dict, Any, List, Optional
from core.llm_engine import ArkLLMEngine
from core.logger import get_logger

logger = get_logger("scan_parser")

# 📘 渲染 DPI：200 给 Vision LLM 足够清晰度
RENDER_DPI = 200

# 📘 扫描件判定阈值
SCAN_THRESHOLD_BLOCKS_PER_PAGE = 2

# 📘 Vision LLM 的结构化识别 prompt
# 要求模型输出严格的 JSON，描述页面的结构化内容
STRUCTURE_RECOGNITION_PROMPT = """\
你是一个专业的文档结构识别助手。请仔细观察这张文档图片，识别其中的所有内容结构。

请输出一个 JSON 对象，格式如下：
{
  "page_type": "table" | "mixed" | "text",
  "elements": [
    {
      "type": "table",
      "rows": [
        [{"text": "单元格内容", "colspan": 1, "rowspan": 1}, ...],
        ...
      ]
    },
    {
      "type": "paragraph",
      "text": "段落文字内容"
    },
    {
      "type": "image_region",
      "description": "图片/印章/签名/照片的简要描述"
    }
  ]
}

规则：
1. table 类型：识别表格的行列结构，包括合并单元格（colspan/rowspan）
   - 每个单元格的 text 是该格子里的完整文字
   - 如果单元格为空，text 设为 ""
   - colspan 默认 1，rowspan 默认 1，只在合并时才需要大于 1
   - 被合并覆盖的单元格不要输出（跳过）
2. paragraph 类型：表格外的独立文字段落
3. image_region 类型：图片、印章、签名、照片、徽标等非文字区域
   - description 简要描述内容（如"红色圆形印章"、"证件照片"）
4. elements 按从上到下的顺序排列
5. 只输出 JSON，不要输出任何其他文字
6. 确保 JSON 格式正确，可以被直接解析"""



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
    """
    📘 把 PDF 页面渲染成 base64 JPEG 图片
    200 DPI，A4 ≈ 1654×2339 像素，约 300-500KB
    """
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("jpeg", jpg_quality=88)
    return base64.b64encode(img_bytes).decode("utf-8")


def _render_page_to_bytes(doc: fitz.Document, page_idx: int) -> bytes:
    """
    📘 把 PDF 页面渲染成 JPEG bytes（用于嵌入 Word 文档）
    """
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("jpeg", jpg_quality=92)


def _call_vision_llm(vision_llm: ArkLLMEngine, image_b64: str, prompt: str) -> Optional[str]:
    """
    📘 调用 Vision LLM，发送图片 + 文字 prompt，收集完整响应
    """
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_b64}",
                    },
                },
                {
                    "type": "text",
                    "text": prompt,
                },
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
    """
    📘 从 Vision LLM 响应中提取 JSON 结构
    模型有时会在 JSON 前后加 markdown 代码块标记
    """
    text = response.strip()
    # 去掉 markdown 代码块
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
    📘 教学笔记：扫描件 PDF 解析主函数（v5 — Vision LLM 结构化识别）

    流程：
    1. 逐页渲染 PDF 为图片
    2. 发给 Vision LLM，获取结构化 JSON（表格/段落/图片区域）
    3. 提取所有需要翻译的文本，生成标准 parsed_data
    4. 同时保存页面图片和结构信息，供 writer 生成 Word 文档

    📘 和 v4 的关键区别：
    v4: OCR 识别文字 → 按坐标合并段落 → 在原图上擦除+重写
    v5: Vision LLM 理解结构 → 输出表格/段落 → 生成全新的 Word 文档
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
    page_structures = []  # 每页的结构化数据
    page_images = []      # 每页的 JPEG bytes

    for page_idx in range(num_pages):
        print(f"  [🔍 第 {page_idx + 1}/{num_pages} 页] 识别中...", flush=True)

        # 1. 渲染页面
        image_b64 = _render_page_to_base64(doc, page_idx)
        image_bytes = _render_page_to_bytes(doc, page_idx)
        page_images.append(image_bytes)

        # 2. 调用 Vision LLM
        response = _call_vision_llm(vision_llm, image_b64, STRUCTURE_RECOGNITION_PROMPT)
        if not response:
            logger.warning(f"第 {page_idx + 1} 页: Vision LLM 无响应")
            page_structures.append({"page_type": "text", "elements": []})
            continue

        # 3. 解析 JSON
        structure = _parse_structure_json(response)
        if not structure:
            logger.warning(f"第 {page_idx + 1} 页: 结构化解析失败，尝试作为纯文本处理")
            # 📘 兜底：把整个响应当作纯文本
            page_structures.append({
                "page_type": "text",
                "elements": [{"type": "paragraph", "text": response}],
            })
        else:
            page_structures.append(structure)
            elem_count = len(structure.get("elements", []))
            logger.info(f"第 {page_idx + 1} 页: 识别到 {elem_count} 个元素 (类型: {structure.get('page_type', '?')})")

        # 4. 从结构中提取翻译单元
        elements = structure.get("elements", []) if structure else []
        for elem_idx, elem in enumerate(elements):
            elem_type = elem.get("type", "")

            if elem_type == "table":
                # 📘 表格：每个非空单元格是一个翻译单元
                rows = elem.get("rows", [])
                for row_idx, row in enumerate(rows):
                    for col_idx, cell in enumerate(row):
                        cell_text = cell.get("text", "").strip()
                        if cell_text:
                            key = f"pg{page_idx}_e{elem_idx}_r{row_idx}_c{col_idx}"
                            item = {
                                "key": key,
                                "type": "table_cell",
                                "full_text": cell_text,
                                "is_empty": False,
                                "dominant_format": {
                                    "font_name": "Unknown",
                                    "font_size": 10,
                                    "font_color": "#000000",
                                    "bold": False,
                                },
                            }
                            all_items.append(item)

            elif elem_type == "paragraph":
                para_text = elem.get("text", "").strip()
                if para_text:
                    key = f"pg{page_idx}_e{elem_idx}_para"
                    item = {
                        "key": key,
                        "type": "pdf_block",
                        "full_text": para_text,
                        "is_empty": False,
                        "dominant_format": {
                            "font_name": "Unknown",
                            "font_size": 11,
                            "font_color": "#000000",
                            "bold": False,
                        },
                    }
                    all_items.append(item)

            # 📘 image_region 不需要翻译，但结构信息保留给 writer

    doc.close()

    total = len(all_items)
    print(f"[🔍 识别完成] {total} 个翻译单元（{num_pages} 页）", flush=True)
    logger.info(f"扫描件解析完成: {total} 个翻译单元")

    return {
        "source": "scan_parser",
        "source_type": "scan",
        "filepath": filepath,
        "items": all_items,
        # 📘 额外数据：供 scan_writer 生成 Word 文档
        "page_structures": page_structures,
        "page_images": page_images,
    }
