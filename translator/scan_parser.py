# translator/scan_parser.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 解析器（v4 — RapidOCR 子进程模式）
# =============================================================
# 扫描件 PDF 里没有可选中的文字，每页是一张图片。
# 普通 pdf_parser 用 PyMuPDF 的 get_text("dict") 提取文本，
# 对扫描件会得到 0 个文本块。
#
# v1 用 RapidOCR v1（rapidocr-onnxruntime），DLL 在 PyQt6 + Python 3.14 下加载失败。
# v2 用 Vision LLM 做 OCR，文字识别准但位置估算不靠谱。
# v3 用火山引擎 OCR API（OCRNormal），但需要开通"视觉智能"服务，控制台 404。
#
# v4 用 RapidOCR v3 + 子进程隔离：
#   - 基于 PaddleOCR 的 ONNX 推理，纯本地运行，不需要网络
#   - 自带中英文 OCR 模型（PP-OCRv4），开箱即用
#   - bbox 精度像素级（四边形 polygon）
#
# 📘 为什么要用子进程？
# RapidOCR 依赖 onnxruntime 的 C++ DLL。
# PyQt6 也加载了一些 C++ DLL（Qt 框架）。
# 在同一个进程里，两者的 DLL 会冲突，导致加载失败。
# 解决方案：在独立的子进程里运行 RapidOCR，
# 主进程（PyQt6 GUI）通过 subprocess 调用，
# 子进程通过 stdout 返回 JSON 结果。
# 这样两套 DLL 各自在自己的进程空间里，互不干扰。
# =============================================================

import json
import os
import subprocess
import sys
import tempfile
import fitz  # PyMuPDF
from typing import Dict, Any, List
from core.logger import get_logger

logger = get_logger("scan_parser")

# 📘 OCR 渲染 DPI：150 够 OCR 引擎识别，又不会太大
# scan_writer 必须用同样的 DPI，像素坐标才能对上
OCR_RENDER_DPI = 150

# 📘 扫描件判定阈值
SCAN_THRESHOLD_BLOCKS_PER_PAGE = 2


# 📘 教学笔记：OCR 子进程的 Python 代码
# 这段代码会被写入临时文件，由子进程执行。
# 子进程独立加载 onnxruntime + RapidOCR，不受主进程 PyQt6 影响。
# 输入：命令行参数传入图片文件路径（可以多个）
# 输出：stdout 输出 JSON，每行一个页面的 OCR 结果
_OCR_WORKER_CODE = r'''
import json, sys, logging, os
logging.disable(logging.INFO)

from rapidocr import RapidOCR
engine = RapidOCR()

for img_path in sys.argv[1:]:
    try:
        img_bytes = open(img_path, "rb").read()
        result = engine(img_bytes)
        if result and result.boxes is not None:
            lines = []
            for box, text, score in zip(result.boxes, result.txts, result.scores):
                t = text.strip()
                if t:
                    lines.append({"text": t, "polygon": box.tolist(), "score": float(score)})
            print(json.dumps(lines, ensure_ascii=False))
        else:
            print("[]")
    except Exception as e:
        print(json.dumps({"error": str(e)}))
'''


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


def _run_ocr_subprocess(img_paths: List[str]) -> List[List[dict]]:
    """
    📘 教学笔记：在子进程中运行 RapidOCR

    为什么用子进程而不是直接 import？
    因为 onnxruntime 的 DLL 和 PyQt6 的 DLL 在同一进程中会冲突。
    子进程有独立的内存空间，DLL 互不干扰。

    流程：
    1. 把 OCR worker 代码写入临时 .py 文件
    2. 用 subprocess 启动子进程，传入图片路径
    3. 子进程逐张图片 OCR，每张输出一行 JSON
    4. 主进程解析 JSON 得到结果

    📘 性能考虑：
    子进程启动有开销（~2秒加载模型），但只启动一次。
    所有页面的图片路径一次性传入，子进程批量处理。
    """
    # 写入临时 worker 脚本
    worker_fd, worker_path = tempfile.mkstemp(suffix='.py', prefix='ocr_worker_')
    try:
        with os.fdopen(worker_fd, 'w', encoding='utf-8') as f:
            f.write(_OCR_WORKER_CODE)

        # 启动子进程
        cmd = [sys.executable, worker_path] + img_paths
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 分钟超时（大文档可能很多页）
        )

        if result.returncode != 0:
            logger.error(f"OCR 子进程失败: {result.stderr[:500]}")
            return [[] for _ in img_paths]

        # 解析输出：每行一个 JSON
        all_results = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                all_results.append([])
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict) and 'error' in data:
                    logger.error(f"OCR 子进程错误: {data['error']}")
                    all_results.append([])
                else:
                    all_results.append(data)
            except json.JSONDecodeError:
                logger.warning(f"OCR 输出解析失败: {line[:100]}")
                all_results.append([])

        # 补齐缺失的结果
        while len(all_results) < len(img_paths):
            all_results.append([])

        return all_results

    finally:
        # 清理临时文件
        try:
            os.unlink(worker_path)
        except OSError:
            pass


def _polygon_to_bbox_px(polygon: List[List[float]]) -> List[float]:
    """
    📘 四边形 polygon → 外接矩形 [x0, y0, x1, y1]（像素坐标）
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
    """
    if not ocr_lines:
        return []

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
        gap_y = nxt["bbox_pt"][1] - current["bbox_pt"][3]
        x_overlap = (
            min(current["bbox_pt"][2], nxt["bbox_pt"][2])
            - max(current["bbox_pt"][0], nxt["bbox_pt"][0])
        )
        current_height = current["bbox_pt"][3] - current["bbox_pt"][1]
        dynamic_threshold = max(merge_threshold_pt, current_height * 1.2)

        if gap_y < dynamic_threshold and x_overlap > 0:
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
    📘 教学笔记：扫描件 PDF 解析主函数（v4 — RapidOCR 子进程模式）

    流程：
    1. 逐页渲染 PDF 为临时 PNG 图片
    2. 一次性启动 OCR 子进程，批量识别所有页面
    3. 合并相邻行为段落
    4. 转换为标准 parsed_data 格式

    参数 vision_llm 保留但不再使用（兼容旧调用）。
    """
    logger.info(f"开始扫描件 OCR 解析: {filepath}")
    print(f"[🔍 OCR 解析] 正在用 RapidOCR 识别扫描件文字...", flush=True)

    doc = fitz.open(filepath)
    zoom = OCR_RENDER_DPI / 72.0

    # 1. 渲染所有页面为临时 PNG
    tmp_dir = tempfile.mkdtemp(prefix='scan_ocr_')
    img_paths = []
    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            img_path = os.path.join(tmp_dir, f'page_{page_idx}.png')
            pix.save(img_path)
            img_paths.append(img_path)

        # 2. 一次性 OCR 所有页面（子进程只启动一次，模型只加载一次）
        print(f"  [🔍 OCR] 共 {len(img_paths)} 页，子进程识别中...", flush=True)
        all_ocr_results = _run_ocr_subprocess(img_paths)

    finally:
        # 清理临时图片
        import shutil
        try:
            shutil.rmtree(tmp_dir)
        except OSError:
            pass

    # 3. 处理 OCR 结果
    all_items = []
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_width_pt = page.rect.width

        ocr_lines = all_ocr_results[page_idx] if page_idx < len(all_ocr_results) else []
        if not ocr_lines:
            logger.debug(f"第 {page_idx + 1} 页: 未识别到文字")
            continue

        logger.info(f"第 {page_idx + 1} 页: 识别到 {len(ocr_lines)} 行文字")

        # 合并相邻行为段落
        merged_blocks = _merge_nearby_lines(ocr_lines, zoom, page_width_pt)
        logger.info(f"第 {page_idx + 1} 页: 合并为 {len(merged_blocks)} 个段落")

        # 转换为标准 parsed_data 格式
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
