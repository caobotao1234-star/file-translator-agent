# translator/scan_parser.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 解析器（v2 — Vision LLM OCR）
# =============================================================
# 扫描件 PDF 里没有可选中的文字，每页是一张图片。
# 普通 pdf_parser 用 PyMuPDF 的 get_text("dict") 提取文本，
# 对扫描件会得到 0 个文本块。
#
# v1 用 RapidOCR（ONNX），但在 PyQt6 + Python 3.14 环境下
# onnxruntime 的 DLL 加载失败。
#
# v2 改用多模态 Vision LLM 做 OCR：
#   - 直接复用用户已有的 doubao-seed-1.8 模型
#   - 不需要额外安装任何包
#   - 能识别中英文混合文本
#   - 输出结构化 JSON（文字 + 在页面中的相对位置）
#   - 位置精度不如传统 OCR，但对翻译场景够用
#
# 📘 为什么 Vision LLM 做 OCR 可行？
# 传统 OCR 的优势是精确的 bbox 坐标（像素级）。
# 但扫描件翻译场景中，我们不需要像素级精度：
#   - 擦除原文用的是 OCR 行级 bbox（粗粒度就够）
#   - 写入译文用的是合并后的段落 bbox
#   - Vision LLM 能给出"上/中/下"、"左/中/右"的区域描述
#   - 结合页面尺寸可以估算出合理的 bbox
# =============================================================

import base64
import json
import fitz  # PyMuPDF
import numpy as np
from typing import Dict, Any, List, Optional
from core.llm_engine import ArkLLMEngine
from core.logger import get_logger

logger = get_logger("scan_parser")

# 📘 OCR 渲染 DPI：150 够 Vision LLM 看清文字
OCR_RENDER_DPI = 150

# 📘 扫描件判定阈值
SCAN_THRESHOLD_BLOCKS_PER_PAGE = 2

# 📘 Vision OCR Prompt：让 LLM 识别图片中的所有文字并给出位置
VISION_OCR_PROMPT = """你是专业的 OCR 文字识别专家。请仔细识别这张图片中的所有文字内容。

要求：
1. 识别图片中每一个独立的文字区域（标题、正文段落、表格文字、页眉页脚等）
2. 对每个文字区域，给出：
   - text: 识别出的完整文字内容（保持原文，不要翻译）
   - position: 文字在页面中的位置，用百分比表示
     - x: 左边界距页面左侧的百分比（0-100）
     - y: 上边界距页面顶部的百分比（0-100）
     - w: 宽度占页面宽度的百分比（0-100）
     - h: 高度占页面高度的百分比（0-100）
   - font_size_hint: 估算的字号大小（"large"=标题, "medium"=正文, "small"=注释/页脚）

3. 按从上到下、从左到右的阅读顺序排列
4. 不要遗漏任何文字，包括小字、水印、印章上的文字
5. 如果某段文字跨多行，合并为一个区域

输出格式：严格 JSON 数组。不要输出其他内容。
示例：
[
  {
    "text": "出生医学证明",
    "position": {"x": 25, "y": 5, "w": 50, "h": 6},
    "font_size_hint": "large"
  },
  {
    "text": "姓名：张三\\n性别：男\\n出生日期：2024年1月1日",
    "position": {"x": 10, "y": 20, "w": 80, "h": 15},
    "font_size_hint": "medium"
  }
]"""


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
    """渲染 PDF 页面为 base64 JPEG"""
    page = doc[page_idx]
    zoom = OCR_RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("jpeg", jpg_quality=90)
    return base64.b64encode(img_bytes).decode("utf-8")


def _render_page_to_numpy(doc: fitz.Document, page_idx: int) -> np.ndarray:
    """渲染 PDF 页面为 numpy 数组（RGB），供 scan_writer 使用"""
    page = doc[page_idx]
    zoom = OCR_RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    return img


def _call_vision_ocr(vision_llm: ArkLLMEngine, img_b64: str, page_idx: int) -> List[dict]:
    """
    📘 教学笔记：用 Vision LLM 做 OCR

    发送页面图片给多模态模型，让它识别所有文字并给出位置。
    返回结构化的文字块列表。
    """
    messages = [
        {"role": "system", "content": VISION_OCR_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"请识别第 {page_idx + 1} 页中的所有文字。"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                },
            ],
        },
    ]

    full_text = ""
    try:
        for chunk in vision_llm.stream_chat(messages):
            if chunk["type"] == "text":
                full_text += chunk["content"]
    except Exception as e:
        logger.error(f"Vision OCR 调用失败 (第 {page_idx + 1} 页): {e}")
        return []

    # 解析 JSON
    text = full_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        logger.warning(f"Vision OCR 结果解析失败 (第 {page_idx + 1} 页): {text[:200]}")
    return []


def _position_to_bbox(position: dict, page_width: float, page_height: float) -> List[float]:
    """
    📘 把 Vision LLM 给出的百分比位置转换为 PDF 点坐标

    position: {"x": 25, "y": 5, "w": 50, "h": 6}  (百分比)
    返回: [x0, y0, x1, y1]  (PDF 点坐标)
    """
    x_pct = max(0, min(100, position.get("x", 0)))
    y_pct = max(0, min(100, position.get("y", 0)))
    w_pct = max(1, min(100, position.get("w", 10)))
    h_pct = max(1, min(100, position.get("h", 5)))

    x0 = page_width * x_pct / 100.0
    y0 = page_height * y_pct / 100.0
    x1 = x0 + page_width * w_pct / 100.0
    y1 = y0 + page_height * h_pct / 100.0

    return [x0, y0, x1, y1]


def _position_to_bbox_px(position: dict, img_width: int, img_height: int) -> List[float]:
    """百分比位置 → 像素坐标（scan_writer 用）"""
    x_pct = max(0, min(100, position.get("x", 0)))
    y_pct = max(0, min(100, position.get("y", 0)))
    w_pct = max(1, min(100, position.get("w", 10)))
    h_pct = max(1, min(100, position.get("h", 5)))

    x0 = img_width * x_pct / 100.0
    y0 = img_height * y_pct / 100.0
    x1 = x0 + img_width * w_pct / 100.0
    y1 = y0 + img_height * h_pct / 100.0

    return [x0, y0, x1, y1]


def _font_size_from_hint(hint: str, bbox_height_pt: float) -> float:
    """从 LLM 的字号提示估算实际字号"""
    hint_map = {"large": 0.6, "medium": 0.7, "small": 0.75}
    factor = hint_map.get(hint, 0.7)
    return max(6.0, round(bbox_height_pt * factor, 1))


def parse_scan_pdf(filepath: str, vision_llm: ArkLLMEngine = None) -> Dict[str, Any]:
    """
    📘 教学笔记：扫描件 PDF 解析主函数（v2 — Vision LLM OCR）

    用多模态 Vision LLM 识别每页的文字和位置。
    如果没有传入 vision_llm，自动创建一个（用默认模型）。

    输出格式和 pdf_parser.parse_pdf() 完全一致。
    """
    # 📘 如果没有传入 vision_llm，用默认配置创建
    if vision_llm is None:
        from config.settings import Config
        model_id = Config.VISION_MODEL_ID or Config.DEFAULT_MODEL_ID
        vision_llm = ArkLLMEngine(api_key=Config.ARK_API_KEY, model_id=model_id)
        logger.info(f"扫描件 OCR 使用模型: {model_id}")

    logger.info(f"开始扫描件 Vision OCR 解析: {filepath}")
    print(f"[🔍 OCR 解析] 正在用 Vision 模型识别扫描件文字...", flush=True)

    doc = fitz.open(filepath)
    zoom = OCR_RENDER_DPI / 72.0

    all_items = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_width_pt = page.rect.width
        page_height_pt = page.rect.height

        # 1. 渲染页面为图片
        img_b64 = _render_page_to_base64(doc, page_idx)

        # 📘 也渲染 numpy 版本，用于计算像素坐标
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img_w_px, img_h_px = pix.w, pix.h

        print(f"  [🔍 第 {page_idx + 1} 页] Vision OCR 识别中...", flush=True)

        # 2. Vision LLM OCR
        ocr_results = _call_vision_ocr(vision_llm, img_b64, page_idx)
        if not ocr_results:
            logger.debug(f"第 {page_idx + 1} 页: 未识别到文字")
            continue

        logger.info(f"第 {page_idx + 1} 页: 识别到 {len(ocr_results)} 个文字区域")

        # 3. 转换为标准 parsed_data 格式
        for block_idx, block in enumerate(ocr_results):
            text = block.get("text", "").strip()
            if not text:
                continue

            position = block.get("position", {})
            font_hint = block.get("font_size_hint", "medium")

            # 百分比 → PDF 点坐标
            bbox_pt = _position_to_bbox(position, page_width_pt, page_height_pt)
            bbox_px = _position_to_bbox_px(position, img_w_px, img_h_px)

            height_pt = bbox_pt[3] - bbox_pt[1]
            font_size = _font_size_from_hint(font_hint, height_pt)
            is_multiline = "\n" in text or height_pt > font_size * 2.5

            alignment = "justify" if is_multiline else "left"

            item = {
                "key": f"pg{page_idx}_b{block_idx}",
                "type": "pdf_block",
                "full_text": text,
                "bbox": bbox_pt,
                "text_bbox": bbox_pt,
                "sub_bboxes": [bbox_pt],
                "sub_bboxes_px": [bbox_px],
                "dominant_format": {
                    "font_name": "Unknown",
                    "font_size": font_size,
                    "font_color": "#000000",
                    "bold": font_hint == "large",
                    "bbox": bbox_pt,
                },
                "alignment": alignment,
                "is_multiline": is_multiline,
                "is_empty": False,
            }
            all_items.append(item)

    doc.close()

    total = len(all_items)
    print(f"[🔍 OCR 解析完成] {total} 个文字段落", flush=True)
    logger.info(f"扫描件解析完成: {total} 个翻译单元")

    return {
        "source": "scan_parser",
        "source_type": "scan",
        "filepath": filepath,
        "items": all_items,
    }
