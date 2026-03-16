# translator/scan_parser.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 解析器（v4 — RapidOCR v3）
# =============================================================
# 扫描件 PDF 里没有可选中的文字，每页是一张图片。
# 普通 pdf_parser 用 PyMuPDF 的 get_text("dict") 提取文本，
# 对扫描件会得到 0 个文本块。
#
# v1 用 RapidOCR v1（rapidocr-onnxruntime），DLL 在 PyQt6 + Python 3.14 下加载失败。
# v2 用 Vision LLM 做 OCR，文字识别准但位置估算不靠谱。
# v3 用火山引擎 OCR API（OCRNormal），但需要开通"视觉智能"服务，控制台 404。
#
# v4 改用 RapidOCR v3（新包名 rapidocr，和 v1 的 rapidocr-onnxruntime 不同）：
#   - 基于 PaddleOCR 的 ONNX 推理，纯本地运行，不需要网络
#   - 自带中英文 OCR 模型（PP-OCRv4），开箱即用
#   - bbox 精度像素级（四边形 polygon）
#   - 不需要 AK/SK 认证，不需要开通任何云服务
#   - v3 修复了 v1 在 Python 3.14 下的 DLL 兼容性问题
#
# 📘 为什么 RapidOCR v3 能用而 v1 不行？
# v1（rapidocr-onnxruntime）直接依赖 onnxruntime 的 C++ DLL，
# 在 Python 3.14 + PyQt6 环境下 DLL 加载冲突。
# v3（rapidocr）重构了引擎加载方式，解决了兼容性问题。
# =============================================================

import fitz  # PyMuPDF
from typing import Dict, Any, List
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


def _get_ocr_engine():
    """
    📘 教学笔记：初始化 RapidOCR v3 引擎

    RapidOCR v3 自带 PP-OCRv4 模型（中英文），首次运行会自动下载。
    引擎是线程安全的，可以复用。
    这里用模块级缓存，避免每页都重新加载模型。
    """
    import logging
    # 📘 抑制 RapidOCR 的 INFO 日志（模型加载信息），避免刷屏
    # 必须在 import 之前设置，因为 RapidOCR 在 import 时就会输出日志
    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    logging.getLogger("rapidocr").setLevel(logging.WARNING)

    from rapidocr import RapidOCR
    return RapidOCR()


# 📘 模块级缓存：OCR 引擎只初始化一次
_ocr_engine = None


def _ensure_ocr_engine():
    """懒加载 OCR 引擎"""
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine = _get_ocr_engine()
    return _ocr_engine


def _render_page_to_png(doc: fitz.Document, page_idx: int) -> bytes:
    """渲染 PDF 页面为 PNG bytes（RapidOCR 接受 bytes 输入）"""
    page = doc[page_idx]
    zoom = OCR_RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("png")


def _call_ocr(engine, img_bytes: bytes, page_idx: int) -> List[dict]:
    """
    📘 教学笔记：调用 RapidOCR 识别单页

    RapidOCR v3 的返回格式：
      result.boxes:  numpy array, shape (N, 4, 2) — 每行 4 个角点的像素坐标
      result.txts:   list[str] — 每行的文字
      result.scores: list[float] — 每行的置信度

    boxes 的 4 个点顺序：左上、右上、右下、左下（和火山 OCR API 一样）。
    """
    try:
        result = engine(img_bytes)
    except Exception as e:
        logger.error(f"OCR 识别失败 (第 {page_idx + 1} 页): {e}")
        return []

    if result is None or result.boxes is None or len(result.boxes) == 0:
        return []

    lines = []
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        text = text.strip()
        if not text:
            continue
        # box: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]] (numpy → list)
        polygon = box.tolist()
        lines.append({
            "text": text,
            "polygon": polygon,
            "score": float(score),
        })

    return lines


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
    📘 教学笔记：扫描件 PDF 解析主函数（v4 — RapidOCR v3）

    用本地 OCR 引擎识别每页的文字和精确位置。
    输出格式和 pdf_parser.parse_pdf() 完全一致。

    参数 vision_llm 保留但不再使用（兼容旧调用）。
    """
    engine = _ensure_ocr_engine()

    logger.info(f"开始扫描件 OCR 解析: {filepath}")
    print(f"[🔍 OCR 解析] 正在用 RapidOCR 识别扫描件文字...", flush=True)

    doc = fitz.open(filepath)
    zoom = OCR_RENDER_DPI / 72.0

    all_items = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_width_pt = page.rect.width

        # 1. 渲染页面为图片
        img_bytes = _render_page_to_png(doc, page_idx)

        print(f"  [🔍 第 {page_idx + 1} 页] OCR 识别中...", flush=True)

        # 2. 调用 RapidOCR
        ocr_lines = _call_ocr(engine, img_bytes, page_idx)
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
