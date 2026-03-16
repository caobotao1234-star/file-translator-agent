# translator/scan_parser.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 解析器（v3 — 火山引擎 OCR API）
# =============================================================
# 扫描件 PDF 里没有可选中的文字，每页是一张图片。
# 普通 pdf_parser 用 PyMuPDF 的 get_text("dict") 提取文本，
# 对扫描件会得到 0 个文本块。
#
# v1 用 RapidOCR（ONNX），但 onnxruntime DLL 在 PyQt6 + Python 3.14 下加载失败。
# v2 用 Vision LLM 做 OCR，文字识别准但位置估算不靠谱（百分比坐标误差大）。
#
# v3 改用火山引擎 OCR API（OCRNormal）：
#   - 专业 OCR 引擎，bbox 精度像素级
#   - 返回每行文字的精确多边形坐标（polygon）
#   - 不需要额外安装包（volcengine SDK 已在 requirements.txt）
#   - 用 AK/SK 认证（和 Ark API Key 是两套体系）
#
# 📘 为什么专业 OCR 比 Vision LLM 好？
# Vision LLM 擅长"理解"图片内容，但不擅长精确定位。
# 它给出的位置是"大概在页面中间偏上"这种模糊描述。
# 专业 OCR 引擎用的是文字检测模型（如 EAST/DB），
# 输出的是像素级精确的 bounding box，误差通常 < 3px。
# 翻译场景需要精确 bbox 来擦除原文和写入译文，所以必须用专业 OCR。
# =============================================================

import base64
import json
import os
import fitz  # PyMuPDF
from typing import Dict, Any, List, Optional
from core.logger import get_logger

logger = get_logger("scan_parser")

# 📘 OCR 渲染 DPI：150 够 OCR 引擎识别，又不会太大
# scan_writer 必须用同样的 DPI，像素坐标才能对上
OCR_RENDER_DPI = 150

# 📘 扫描件判定阈值
SCAN_THRESHOLD_BLOCKS_PER_PAGE = 2


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


def _get_visual_service():
    """
    📘 教学笔记：初始化火山引擎视觉智能服务

    OCR API 用 AK/SK 认证（和 Ark LLM 的 API Key 是两套体系）。
    AK/SK 从 .env 读取，也支持环境变量 VOLC_ACCESSKEY / VOLC_SECRETKEY。
    """
    from volcengine.visual.VisualService import VisualService

    ak = os.getenv("VOLC_ACCESSKEY", "")
    sk = os.getenv("VOLC_SECRETKEY", "")

    if not ak or not sk:
        raise ValueError(
            "扫描件 OCR 需要火山引擎 AK/SK。\n"
            "请在 .env 中设置 VOLC_ACCESSKEY 和 VOLC_SECRETKEY。\n"
            "获取方式：火山引擎控制台 → 访问控制 → 访问密钥"
        )

    vs = VisualService()
    vs.set_ak(ak)
    vs.set_sk(sk)
    return vs


def _render_page_to_base64(doc: fitz.Document, page_idx: int) -> str:
    """渲染 PDF 页面为 base64 JPEG"""
    page = doc[page_idx]
    zoom = OCR_RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("jpeg", jpg_quality=90)
    return base64.b64encode(img_bytes).decode("utf-8")


def _call_ocr_api(visual_service, img_b64: str, page_idx: int) -> List[dict]:
    """
    📘 教学笔记：调用火山引擎 OCRNormal API

    请求参数：image_base64（图片的 base64 编码）
    返回格式：
    {
      "code": 10000,
      "data": {
        "line_texts": ["第一行文字", "第二行文字", ...],
        "line_rects": [
          [[x1,y1], [x2,y2], [x3,y3], [x4,y4]],  // 四个角的像素坐标
          ...
        ]
      }
    }

    📘 line_rects 是四边形（polygon），不是矩形。
    四个点顺序：左上、右上、右下、左下。
    我们取外接矩形（min/max）作为 bbox。
    """
    try:
        resp = visual_service.ocr_normal({"image_base64": img_b64})
    except Exception as e:
        logger.error(f"OCR API 调用失败 (第 {page_idx + 1} 页): {e}")
        return []

    code = resp.get("code", 0)
    if code != 10000:
        msg = resp.get("message", "未知错误")
        logger.error(f"OCR API 返回错误 (第 {page_idx + 1} 页): code={code}, message={msg}")
        return []

    data = resp.get("data", {})
    line_texts = data.get("line_texts", [])
    line_rects = data.get("line_rects", [])

    if len(line_texts) != len(line_rects):
        logger.warning(
            f"OCR 结果数量不匹配: {len(line_texts)} texts vs {len(line_rects)} rects"
        )

    results = []
    for i in range(min(len(line_texts), len(line_rects))):
        text = line_texts[i].strip()
        if not text:
            continue

        polygon = line_rects[i]  # [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
        results.append({
            "text": text,
            "polygon": polygon,
        })

    return results


def _polygon_to_bbox_px(polygon: List[List[float]]) -> List[float]:
    """
    📘 四边形 polygon → 外接矩形 [x0, y0, x1, y1]（像素坐标）

    polygon: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
    返回: [x_min, y_min, x_max, y_max]
    """
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _bbox_px_to_pt(bbox_px: List[float], zoom: float) -> List[float]:
    """像素坐标 → PDF 点坐标（除以 zoom）"""
    return [v / zoom for v in bbox_px]


def _estimate_font_size(bbox_height_pt: float, text: str) -> float:
    """
    📘 从 bbox 高度估算字号

    单行文本：字号 ≈ bbox 高度 × 0.75（考虑行距）
    多行文本：字号 ≈ bbox 高度 / 行数 × 0.75
    """
    line_count = max(1, text.count("\n") + 1)
    per_line_height = bbox_height_pt / line_count
    return max(6.0, round(per_line_height * 0.75, 1))


def _merge_nearby_lines(
    ocr_lines: List[dict],
    zoom: float,
    page_width_pt: float,
    merge_threshold_pt: float = 3.0,
) -> List[dict]:
    """
    📘 教学笔记：合并相邻行为段落

    OCR 返回的是行级结果，但翻译需要段落级。
    合并策略：
    - 两行的 x 范围重叠（水平对齐）
    - 两行的 y 间距 < 阈值（垂直相邻）
    - 合并后的文本用换行符连接
    - bbox 取所有行的外接矩形

    📘 为什么要合并？
    如果不合并，"装配式建筑全生态产业链服务商" 可能被拆成两行分别翻译，
    译文就会变成两个独立的短句，失去上下文。
    合并后作为一个段落翻译，质量更好。
    """
    if not ocr_lines:
        return []

    # 先转换为统一格式
    items = []
    for line in ocr_lines:
        bbox_px = _polygon_to_bbox_px(line["polygon"])
        bbox_pt = _bbox_px_to_pt(bbox_px, zoom)
        items.append({
            "text": line["text"],
            "bbox_px": bbox_px,
            "bbox_pt": bbox_pt,
        })

    # 按 y 坐标排序
    items.sort(key=lambda it: it["bbox_pt"][1])

    merged = []
    current = items[0]

    for i in range(1, len(items)):
        nxt = items[i]

        # 当前块和下一块的垂直间距
        gap_y = nxt["bbox_pt"][1] - current["bbox_pt"][3]

        # 水平重叠检测：两个块的 x 范围是否有交集
        x_overlap = (
            min(current["bbox_pt"][2], nxt["bbox_pt"][2])
            - max(current["bbox_pt"][0], nxt["bbox_pt"][0])
        )
        # 当前块的高度（用于估算行距阈值）
        current_height = current["bbox_pt"][3] - current["bbox_pt"][1]
        # 📘 动态阈值：行距 < 当前块高度的 1.2 倍 → 认为是同一段落
        dynamic_threshold = max(merge_threshold_pt, current_height * 1.2)

        if gap_y < dynamic_threshold and x_overlap > 0:
            # 合并
            current["text"] += "\n" + nxt["text"]
            current["bbox_px"] = [
                min(current["bbox_px"][0], nxt["bbox_px"][0]),
                min(current["bbox_px"][1], nxt["bbox_px"][1]),
                max(current["bbox_px"][2], nxt["bbox_px"][2]),
                max(current["bbox_px"][3], nxt["bbox_px"][3]),
            ]
            current["bbox_pt"] = [
                min(current["bbox_pt"][0], nxt["bbox_pt"][0]),
                min(current["bbox_pt"][1], nxt["bbox_pt"][1]),
                max(current["bbox_pt"][2], nxt["bbox_pt"][2]),
                max(current["bbox_pt"][3], nxt["bbox_pt"][3]),
            ]
        else:
            merged.append(current)
            current = nxt

    merged.append(current)
    return merged


def parse_scan_pdf(filepath: str, vision_llm=None) -> Dict[str, Any]:
    """
    📘 教学笔记：扫描件 PDF 解析主函数（v3 — 火山引擎 OCR API）

    用专业 OCR 引擎识别每页的文字和精确位置。
    输出格式和 pdf_parser.parse_pdf() 完全一致。

    参数 vision_llm 保留但不再使用（兼容旧调用）。
    """
    # 📘 加载 .env（确保 VOLC_ACCESSKEY/VOLC_SECRETKEY 可用）
    from dotenv import load_dotenv
    load_dotenv()

    visual_service = _get_visual_service()

    logger.info(f"开始扫描件 OCR 解析: {filepath}")
    print(f"[🔍 OCR 解析] 正在用火山引擎 OCR 识别扫描件文字...", flush=True)

    doc = fitz.open(filepath)
    zoom = OCR_RENDER_DPI / 72.0

    all_items = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_width_pt = page.rect.width
        page_height_pt = page.rect.height

        # 1. 渲染页面为图片
        img_b64 = _render_page_to_base64(doc, page_idx)

        print(f"  [🔍 第 {page_idx + 1} 页] OCR 识别中...", flush=True)

        # 2. 调用火山引擎 OCR API
        ocr_lines = _call_ocr_api(visual_service, img_b64, page_idx)
        if not ocr_lines:
            logger.debug(f"第 {page_idx + 1} 页: 未识别到文字")
            continue

        logger.info(f"第 {page_idx + 1} 页: 识别到 {len(ocr_lines)} 行文字")

        # 3. 合并相邻行为段落
        merged_blocks = _merge_nearby_lines(ocr_lines, zoom, page_width_pt)
        logger.info(f"第 {page_idx + 1} 页: 合并为 {len(merged_blocks)} 个段落")

        # 4. 转换为标准 parsed_data 格式
        for block_idx, block in enumerate(merged_blocks):
            text = block["text"]
            bbox_pt = block["bbox_pt"]
            bbox_px = block["bbox_px"]

            height_pt = bbox_pt[3] - bbox_pt[1]
            font_size = _estimate_font_size(height_pt, text)
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
                    "bold": font_size >= 14,
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
