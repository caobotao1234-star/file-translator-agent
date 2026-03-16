# translator/scan_parser.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 解析器
# =============================================================
# 扫描件 PDF 里没有可选中的文字，每页是一张图片。
# 普通 pdf_parser 用 PyMuPDF 的 get_text("dict") 提取文本，
# 对扫描件会得到 0 个文本块。
#
# 本模块的策略：
#   1. detect_scan_pdf(): 检测 PDF 是否为扫描件
#   2. parse_scan_pdf(): 用 OCR 识别每页文字 + 位置
#   3. 输出和 pdf_parser.parse_pdf() 完全相同的数据结构
#      这样后续的翻译、写入、排版审校全部复用
#
# OCR 引擎：RapidOCR（PaddleOCR 的 ONNX 移植版）
#   - 不依赖 PaddlePaddle，兼容 Python 3.14
#   - 离线运行，免费，中英文识别效果好
#   - 输出每个文字块的 bbox 和置信度
# =============================================================

import os
import sys
import fitz  # PyMuPDF
import numpy as np
from typing import Dict, Any, List, Optional
from core.logger import get_logger

# 📘 教学笔记：修复 PyQt6 环境下 ONNX Runtime DLL 加载失败
# PyQt6 启动时会修改 DLL 搜索路径，导致 onnxruntime 的 C++ DLL 找不到。
# 解决方案：在 import onnxruntime 之前，把相关 DLL 目录加到搜索路径。
if sys.platform == "win32":
    # 📘 把 venv 和系统的 DLL 目录都加上
    _dll_dirs = [
        os.path.join(sys.prefix, "DLLs"),
        os.path.join(sys.prefix, "Library", "bin"),
        os.path.join(sys.prefix, "Scripts"),
        os.path.join(sys.prefix, "Lib", "site-packages", "onnxruntime", "capi"),
    ]
    for d in _dll_dirs:
        if os.path.isdir(d):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
    # 📘 也把 PATH 里的目录加上（兜底）
    for d in os.environ.get("PATH", "").split(";"):
        if d and os.path.isdir(d):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass

logger = get_logger("scan_parser")

# 📘 OCR 渲染 DPI：200 比 150 更清晰，OCR 识别率更高
OCR_RENDER_DPI = 200

# 📘 扫描件判定阈值：如果每页平均文本块数 < 此值，判定为扫描件
SCAN_THRESHOLD_BLOCKS_PER_PAGE = 2


def detect_scan_pdf(filepath: str) -> bool:
    """
    📘 教学笔记：检测 PDF 是否为扫描件

    策略：用 PyMuPDF 提取每页的文本块数量。
    如果平均每页文本块数 < 阈值（2），判定为扫描件。

    为什么不直接看 get_text() 是否为空？
    因为有些扫描件 PDF 带了一层很差的 OCR 文本层（几个乱码字符），
    不能简单用"有没有文字"来判断。要看文本块数量是否合理。
    """
    try:
        doc = fitz.open(filepath)
        if len(doc) == 0:
            doc.close()
            return False

        total_blocks = 0
        for page in doc:
            # 只统计文本块（type=0），忽略图片块（type=1）
            blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            for block in blocks.get("blocks", []):
                if block.get("type") == 0:  # 文本块
                    # 检查是否有实际文字内容
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


def _render_page_to_numpy(doc: fitz.Document, page_idx: int) -> np.ndarray:
    """渲染 PDF 页面为 numpy 数组（RGB），供 OCR 使用"""
    page = doc[page_idx]
    zoom = OCR_RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    # PyMuPDF pixmap → numpy array (H, W, 3)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    if pix.n == 4:  # RGBA → RGB
        img = img[:, :, :3]
    return img


def _ocr_page(ocr_engine, img: np.ndarray) -> List[dict]:
    """
    📘 教学笔记：对单页图片做 OCR

    RapidOCR 返回格式：
      result = [
        [[[x0,y0],[x1,y1],[x2,y2],[x3,y3]], text, confidence],
        ...
      ]
    其中四个点是文字框的四角坐标（左上、右上、右下、左下）。
    我们转换为 [x0, y0, x1, y1] 的 bbox 格式（左上角 + 右下角）。
    """
    result, _ = ocr_engine(img)
    if not result:
        return []

    blocks = []
    for item in result:
        points, text, confidence = item
        if not text or not text.strip():
            continue
        if confidence < 0.5:  # 📘 低置信度的识别结果丢弃
            continue

        # 四角坐标 → bbox
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        bbox = [min(xs), min(ys), max(xs), max(ys)]

        blocks.append({
            "text": text.strip(),
            "bbox": bbox,  # 📘 注意：这是像素坐标，需要转换为 PDF 点坐标
            "confidence": confidence,
        })

    return blocks


def _estimate_font_size(bbox_height_pt: float) -> float:
    """
    📘 从 bbox 高度估算字号

    OCR 给的是文字框高度，字号 ≈ 框高 × 0.75（经验值）。
    因为文字框包含行间距，实际字号比框高小。
    """
    return max(6.0, round(bbox_height_pt * 0.75, 1))


def _should_merge_ocr_blocks(a: dict, b: dict, page_width: float) -> bool:
    """
    📘 判断两个 OCR 块是否应该合并为一个段落

    合并条件：
    1. 垂直距离小（y 方向间距 < 行高的 1.5 倍）
    2. 水平范围重叠（x 方向有交集，说明是同一列）
    3. 两个块都不是很短（避免把标题和正文合并）
    """
    a_bbox = a["bbox_pt"]
    b_bbox = b["bbox_pt"]

    a_height = a_bbox[3] - a_bbox[1]
    b_height = b_bbox[3] - b_bbox[1]
    avg_height = (a_height + b_height) / 2

    # 垂直间距
    vertical_gap = b_bbox[1] - a_bbox[3]
    if vertical_gap < 0 or vertical_gap > avg_height * 1.5:
        return False

    # 水平重叠
    x_overlap = min(a_bbox[2], b_bbox[2]) - max(a_bbox[0], b_bbox[0])
    min_width = min(a_bbox[2] - a_bbox[0], b_bbox[2] - b_bbox[0])
    if min_width > 0 and x_overlap / min_width < 0.5:
        return False

    # 字号差异不能太大（避免标题和正文合并）
    size_ratio = max(a_height, b_height) / max(min(a_height, b_height), 1)
    if size_ratio > 1.5:
        return False

    return True


def _merge_ocr_blocks(blocks: List[dict], page_width: float) -> List[dict]:
    """
    📘 合并相邻的 OCR 块为段落

    OCR 通常按行识别，一个段落会被拆成多行。
    这里把垂直相邻、水平对齐的行合并为一个段落。
    """
    if not blocks:
        return []

    # 按 y 坐标排序
    sorted_blocks = sorted(blocks, key=lambda b: (b["bbox_pt"][1], b["bbox_pt"][0]))

    merged = [sorted_blocks[0]]
    for block in sorted_blocks[1:]:
        if _should_merge_ocr_blocks(merged[-1], block, page_width):
            # 合并：扩展 bbox，拼接文本
            prev = merged[-1]
            prev["text"] += "\n" + block["text"]
            prev["bbox_pt"] = [
                min(prev["bbox_pt"][0], block["bbox_pt"][0]),
                min(prev["bbox_pt"][1], block["bbox_pt"][1]),
                max(prev["bbox_pt"][2], block["bbox_pt"][2]),
                max(prev["bbox_pt"][3], block["bbox_pt"][3]),
            ]
            prev["bbox_px"] = [
                min(prev["bbox_px"][0], block["bbox_px"][0]),
                min(prev["bbox_px"][1], block["bbox_px"][1]),
                max(prev["bbox_px"][2], block["bbox_px"][2]),
                max(prev["bbox_px"][3], block["bbox_px"][3]),
            ]
            # 📘 子块 bbox 列表：scan_writer 需要逐个擦除
            prev["sub_bboxes_pt"].append(block["bbox_pt"])
            prev["sub_bboxes_px"].append(block["bbox_px"])
        else:
            merged.append(block)

    return merged


def parse_scan_pdf(filepath: str) -> Dict[str, Any]:
    """
    📘 教学笔记：扫描件 PDF 解析主函数

    输出格式和 pdf_parser.parse_pdf() 完全一致：
    {
        "source": "scan_parser",
        "source_type": "scan",       ← 标记为扫描件，writer 据此切换擦除策略
        "filepath": "...",
        "items": [
            {
                "key": "pg0_b0",
                "type": "pdf_block",
                "full_text": "识别出的文字",
                "bbox": [x0, y0, x1, y1],      # PDF 点坐标
                "text_bbox": [x0, y0, x1, y1],  # 同 bbox
                "sub_bboxes": [[...]],           # 合并前的子块 bbox
                "sub_bboxes_px": [[...]],        # 像素坐标版（scan_writer 用）
                "dominant_format": {
                    "font_name": "Unknown",
                    "font_size": 12.0,
                    "font_color": "#000000",
                    "bold": False,
                    "bbox": [x0, y0, x1, y1],
                },
                "alignment": "left" / "justify",
                "is_multiline": True/False,
                "is_empty": False,
            },
            ...
        ]
    }
    """
    from rapidocr_onnxruntime import RapidOCR

    logger.info(f"开始扫描件 OCR 解析: {filepath}")
    print(f"[🔍 OCR 解析] 正在识别扫描件文字...", flush=True)

    ocr_engine = RapidOCR()
    doc = fitz.open(filepath)
    zoom = OCR_RENDER_DPI / 72.0  # 像素坐标 → PDF 点坐标的缩放因子

    all_items = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_width_pt = page.rect.width
        page_height_pt = page.rect.height

        # 1. 渲染页面为图片
        img = _render_page_to_numpy(doc, page_idx)
        logger.debug(f"第 {page_idx + 1} 页: 图片尺寸 {img.shape}")

        # 2. OCR 识别
        ocr_blocks = _ocr_page(ocr_engine, img)
        if not ocr_blocks:
            logger.debug(f"第 {page_idx + 1} 页: OCR 未识别到文字")
            continue

        logger.info(f"第 {page_idx + 1} 页: OCR 识别到 {len(ocr_blocks)} 个文字块")

        # 3. 像素坐标 → PDF 点坐标
        for block in ocr_blocks:
            px_bbox = block["bbox"]
            pt_bbox = [
                px_bbox[0] / zoom,
                px_bbox[1] / zoom,
                px_bbox[2] / zoom,
                px_bbox[3] / zoom,
            ]
            block["bbox_pt"] = pt_bbox
            block["bbox_px"] = px_bbox
            block["sub_bboxes_pt"] = [pt_bbox]
            block["sub_bboxes_px"] = [px_bbox]

        # 4. 合并相邻行为段落
        merged = _merge_ocr_blocks(ocr_blocks, page_width_pt)
        logger.debug(f"第 {page_idx + 1} 页: 合并后 {len(merged)} 个段落")

        # 5. 转换为标准 parsed_data 格式
        for block_idx, block in enumerate(merged):
            bbox_pt = block["bbox_pt"]
            height_pt = bbox_pt[3] - bbox_pt[1]
            font_size = _estimate_font_size(height_pt)
            is_multiline = "\n" in block["text"]

            # 📘 多行文本用两端对齐，单行用左对齐
            alignment = "justify" if is_multiline else "left"

            item = {
                "key": f"pg{page_idx}_b{block_idx}",
                "type": "pdf_block",
                "full_text": block["text"],
                "bbox": bbox_pt,
                "text_bbox": bbox_pt,
                "sub_bboxes": block["sub_bboxes_pt"],
                "sub_bboxes_px": block["sub_bboxes_px"],
                "dominant_format": {
                    "font_name": "Unknown",
                    "font_size": font_size,
                    "font_color": "#000000",
                    "bold": False,
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
