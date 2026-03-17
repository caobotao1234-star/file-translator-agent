# translator/scan_parser.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 解析器（v6 — CV + OCR 混合方案）
# =============================================================
# 核心思路：用 CV 做精确位置检测，用 OCR 做精确文字识别，
# LLM 只负责翻译——各司其职，发挥各自优势。
#
# 📘 v5 的问题：
#   - Vision LLM 对位置的估计不精确（bbox_pct 偏差大）
#   - 表格结构（列宽、合并单元格）靠 LLM 猜测，不可靠
#   - 每页都要调用 Vision LLM，成本高、速度慢
#
# 📘 v6 混合方案：
#   1. OpenCV 检测表格线 → 精确的行列网格 + 单元格 bbox
#   2. RapidOCR（subprocess 隔离）全页 OCR → 精确的文字 + 位置
#   3. 文字按位置分配到对应单元格
#   4. 非表格区域的文字作为段落处理
#   5. Vision LLM 作为 fallback（无表格线时）
#
# 📘 为什么 RapidOCR 要用 subprocess？
#   PyQt6 和 onnxruntime 在 Python 3.14 上有 DLL 冲突，
#   subprocess 隔离 DLL 加载，两者互不干扰。
# =============================================================

import base64
import json
import os
import io
import subprocess
import tempfile
import fitz  # PyMuPDF
import cv2
import numpy as np
from PIL import Image
from typing import Dict, Any, List, Optional, Tuple
from core.logger import get_logger

logger = get_logger("scan_parser")

RENDER_DPI = 200
SCAN_THRESHOLD_BLOCKS_PER_PAGE = 2

# 📘 OpenCV 表格线检测参数
MIN_H_LINE_LENGTH = 80    # 水平线最小长度（像素）
MIN_V_LINE_LENGTH = 40    # 垂直线最小长度（像素）
LINE_MERGE_THRESHOLD = 15  # 相近线条合并阈值（像素）
MIN_CELL_SIZE = 20         # 最小单元格尺寸（像素）


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


# =============================================================
# 📘 第一层：OpenCV 表格线检测
# =============================================================

def _detect_table_lines(gray_img: np.ndarray) -> Tuple[List[int], List[int], np.ndarray]:
    """
    📘 教学笔记：用 OpenCV 形态学操作检测表格线

    原理：
    1. 二值化（白底黑字 → 黑底白字）
    2. 用长条形 kernel 做开运算（morphologyEx OPEN）
       - 水平 kernel (宽×1) → 只保留水平线
       - 垂直 kernel (1×高) → 只保留垂直线
    3. 提取线条轮廓，获取位置和长度
    4. 合并相近的线条
    5. 📘 关键：只保留"主要"垂直线（长度超过表格高度 30% 的）
       短的垂直线段可能只在某些行存在，不是真正的列分隔线

    返回：(水平线Y坐标列表, 垂直线X坐标列表, 表格区域mask)
    """
    _, binary = cv2.threshold(gray_img, 180, 255, cv2.THRESH_BINARY_INV)

    # 📘 水平线检测
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (MIN_H_LINE_LENGTH, 1))
    h_lines_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)

    # 📘 垂直线检测
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, MIN_V_LINE_LENGTH))
    v_lines_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

    # 📘 提取水平线位置
    h_positions = _extract_line_positions(h_lines_mask, axis="h", min_length=MIN_H_LINE_LENGTH)

    # 📘 提取垂直线位置 — 带长度信息，用于过滤短线段
    v_positions = _extract_major_vertical_lines(v_lines_mask, h_positions)

    # 📘 合并表格线 mask
    table_mask = cv2.add(h_lines_mask, v_lines_mask)

    return h_positions, v_positions, table_mask


def _extract_line_positions(mask: np.ndarray, axis: str, min_length: int) -> List[int]:
    """
    📘 从线条 mask 中提取线条位置，合并相近的线条
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    positions = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if axis == "h" and w >= min_length:
            positions.append(y + h // 2)
        elif axis == "v" and h >= min_length:
            positions.append(x + w // 2)

    positions.sort()

    # 📘 合并相近的线条
    merged = []
    for p in positions:
        if merged and abs(p - merged[-1]) < LINE_MERGE_THRESHOLD:
            merged[-1] = (merged[-1] + p) // 2
        else:
            merged.append(p)
    return merged


def _extract_major_vertical_lines(
    v_mask: np.ndarray,
    h_positions: List[int],
) -> List[int]:
    """
    📘 教学笔记：提取主要的垂直线（过滤短线段）

    很多表格有合并单元格，内部的垂直线只在部分行存在。
    如果把所有短线段都当作列分隔线，会产生很多假列。

    策略：
    1. 提取所有垂直线的 (x, 长度)
    2. 计算表格总高度（最上水平线到最下水平线）
    3. 只保留长度 >= 表格高度 30% 的垂直线
    4. 但始终保留最左和最右的垂直线（表格边框）
    """
    contours, _ = cv2.findContours(v_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # 收集所有垂直线的 (x_center, height)
    v_lines = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if h >= MIN_V_LINE_LENGTH:
            v_lines.append((x + w // 2, h))

    if not v_lines:
        return []

    # 📘 计算表格高度
    if len(h_positions) >= 2:
        table_height = h_positions[-1] - h_positions[0]
    else:
        table_height = max(h for _, h in v_lines)

    # 📘 按 x 位置合并相近的线（取最长的）
    v_lines.sort(key=lambda t: t[0])
    merged = []
    for x, h in v_lines:
        if merged and abs(x - merged[-1][0]) < LINE_MERGE_THRESHOLD:
            # 保留更长的那条
            if h > merged[-1][1]:
                merged[-1] = (x, h)
        else:
            merged.append((x, h))

    if not merged:
        return []

    # 📘 过滤：只保留长度 >= 表格高度 30% 的线
    # 但始终保留最左和最右（表格边框）
    min_height = table_height * 0.3
    leftmost_x = merged[0][0]
    rightmost_x = merged[-1][0]

    major = []
    for x, h in merged:
        if h >= min_height or x == leftmost_x or x == rightmost_x:
            major.append(x)

    logger.debug(
        f"垂直线过滤: {len(v_lines)} 条 → 合并 {len(merged)} 条 → "
        f"主要 {len(major)} 条 (阈值: {min_height:.0f}px, 表格高度: {table_height}px)"
    )

    return major


def _build_cell_grid(
    h_positions: List[int],
    v_positions: List[int],
    img_height: int,
    img_width: int,
) -> List[List[dict]]:
    """
    📘 教学笔记：从行列线位置构建单元格网格

    输入：水平线 Y 坐标列表、垂直线 X 坐标列表
    输出：二维数组 grid[row][col]，每个元素是 {x1, y1, x2, y2}

    📘 过滤掉太小的行/列（高度或宽度 < MIN_CELL_SIZE），
    这些通常是双线或装饰线造成的假行/列。
    """
    # 过滤太小的间距
    def filter_positions(positions):
        if len(positions) < 2:
            return positions
        filtered = [positions[0]]
        for p in positions[1:]:
            if p - filtered[-1] >= MIN_CELL_SIZE:
                filtered.append(p)
            else:
                # 合并到前一个（取中间值）
                filtered[-1] = (filtered[-1] + p) // 2
        return filtered

    h_pos = filter_positions(h_positions)
    v_pos = filter_positions(v_positions)

    if len(h_pos) < 2 or len(v_pos) < 2:
        return []

    grid = []
    for r in range(len(h_pos) - 1):
        row = []
        for c in range(len(v_pos) - 1):
            row.append({
                "x1": v_pos[c],
                "y1": h_pos[r],
                "x2": v_pos[c + 1],
                "y2": h_pos[r + 1],
            })
        grid.append(row)
    return grid




# =============================================================
# 📘 第二层：RapidOCR 文字识别（subprocess 隔离）
# =============================================================

def _ocr_page_subprocess(img_path: str) -> List[dict]:
    """
    📘 教学笔记：通过 subprocess 调用 RapidOCR

    为什么用 subprocess？
    PyQt6 和 onnxruntime 在 Python 3.14 上有 DLL 冲突。
    subprocess 创建独立进程，DLL 加载互不干扰。

    返回: [{"text": "...", "bbox": [x1, y1, x2, y2], "confidence": 0.95}, ...]
    """
    # 📘 用临时脚本文件代替 -c 参数，避免 Windows 路径转义问题
    script_content = '''
import json, sys
from rapidocr_onnxruntime import RapidOCR
engine = RapidOCR()
img_path = sys.argv[1]
result, _ = engine(img_path)
texts = []
if result:
    for line in result:
        bbox = line[0]
        x1 = min(p[0] for p in bbox)
        y1 = min(p[1] for p in bbox)
        x2 = max(p[0] for p in bbox)
        y2 = max(p[1] for p in bbox)
        conf = line[2]
        if isinstance(conf, str):
            try:
                conf = float(conf)
            except ValueError:
                conf = 0.0
        texts.append({
            "text": line[1],
            "bbox": [int(x1), int(y1), int(x2), int(y2)],
            "confidence": round(float(conf), 3)
        })
print(json.dumps(texts, ensure_ascii=False))
'''
    # 📘 写临时脚本文件
    script_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    )
    script_file.write(script_content)
    script_file.close()

    # 📘 找到 Python 解释器路径
    python_exe = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "volcengine", "Scripts", "python.exe",
    )
    if not os.path.exists(python_exe):
        # fallback: 用 sys.executable
        import sys
        python_exe = sys.executable

    try:
        result = subprocess.run(
            [python_exe, script_file.name, img_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        if result.stderr:
            logger.warning(f"OCR subprocess stderr: {result.stderr[:300]}")
    except subprocess.TimeoutExpired:
        logger.error("OCR subprocess 超时")
    except Exception as e:
        logger.error(f"OCR subprocess 失败: {e}")
    finally:
        try:
            os.unlink(script_file.name)
        except OSError:
            pass
    return []






# =============================================================
# 📘 第四层：图片区域检测
# =============================================================

def _detect_image_regions(
    img: np.ndarray,
    gray: np.ndarray,
    table_bbox: Optional[Tuple[int, int, int, int]],
    ocr_results: List[dict],
) -> List[dict]:
    """
    📘 教学笔记：检测非文字非表格的图片区域（LOGO、二维码、印章等）

    策略：
    1. 找到页面中颜色丰富（非灰度）的区域
    2. 面积足够大的连通区域 → 可能是图片
    3. 📘 v6.1: 不再排除表格内部的图片区域
       出生证明等文档的国徽、二维码都在表格内部

    简化实现：用颜色饱和度检测。
    扫描件的文字和表格线通常是黑白的，
    而 LOGO、印章、照片通常有颜色。
    """
    regions = []
    h, w = img.shape[:2]

    # 📘 转 HSV，提取饱和度通道
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]

    # 📘 高饱和度区域（有颜色的区域）
    _, sat_mask = cv2.threshold(saturation, 50, 255, cv2.THRESH_BINARY)

    # 📘 膨胀连接相近的彩色像素
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    sat_mask = cv2.dilate(sat_mask, kernel, iterations=2)

    # 📘 找连通区域
    contours, _ = cv2.findContours(sat_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cw * ch

        # 📘 过滤太小的区域（至少 50x50 像素）
        if cw < 50 or ch < 50:
            continue

        # 📘 过滤太大的区域（超过页面 50% 可能是背景）
        if area > h * w * 0.5:
            continue

        # 📘 计算 bbox 百分比（用于裁剪）
        bbox_pct = [
            round(x / w * 100, 1),
            round(y / h * 100, 1),
            round((x + cw) / w * 100, 1),
            round((y + ch) / h * 100, 1),
        ]

        regions.append({
            "type": "image_region",
            "description": "彩色图片区域",
            "bbox_pct": bbox_pct,
            "bbox_px": [x, y, x + cw, y + ch],
        })

    return regions


def _crop_image_region(page_img: Image.Image, bbox_pct: List[float]) -> Optional[bytes]:
    """
    📘 从页面图片中裁剪指定区域
    bbox_pct: [left%, top%, right%, bottom%]
    """
    if not bbox_pct or len(bbox_pct) != 4:
        return None

    w, h = page_img.size
    left = int(w * bbox_pct[0] / 100)
    top = int(h * bbox_pct[1] / 100)
    right = int(w * bbox_pct[2] / 100)
    bottom = int(h * bbox_pct[3] / 100)

    left = max(0, min(left, w - 1))
    top = max(0, min(top, h - 1))
    right = max(left + 1, min(right, w))
    bottom = max(top + 1, min(bottom, h))

    if right - left < 10 or bottom - top < 10:
        return None

    try:
        cropped = page_img.crop((left, top, right, bottom))
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"图片裁剪失败: {e}")
        return None


# =============================================================
# 📘 第五层：Vision LLM Fallback
# =============================================================

# 📘 当 OpenCV 检测不到表格线时（无边框表格、纯文本页面等），
# 回退到 Vision LLM 方案（v5 的逻辑）。
STRUCTURE_RECOGNITION_PROMPT = """\
你是一个专业的文档结构识别助手。请仔细观察这张文档图片，精确识别其中的所有内容和布局。

请输出一个 JSON 对象，格式如下：
{
  "page_type": "table" | "mixed" | "text",
  "elements": [
    {
      "type": "table",
      "col_widths": [30, 70],
      "border": "none",
      "rows": [
        {
          "cells": [
            {"text": "单元格内容", "colspan": 1, "rowspan": 1, "bold": false, "align": "left"}
          ]
        }
      ]
    },
    {
      "type": "paragraph",
      "text": "段落文字内容",
      "bold": false,
      "align": "left",
      "font_size": "normal"
    }
  ]
}

详细规则：
- col_widths: 每列宽度百分比，总和100
- border: "all" | "outer" | "none"
- cells: text/colspan/rowspan/bold/align
- 被合并覆盖的单元格不要输出
- elements 按从上到下排列
- 只输出 JSON"""


def _call_vision_llm(vision_llm, image_b64: str, prompt: str) -> Optional[str]:
    """📘 调用 Vision LLM（fallback 用）"""
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
    """📘 从 Vision LLM 响应中提取 JSON"""
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
        logger.warning(f"JSON 解析失败: {text[:300]}...")
    return None


# =============================================================
# 📘 主函数：解析扫描件 PDF
# =============================================================

def _render_page_to_base64(doc: fitz.Document, page_idx: int) -> str:
    """📘 把 PDF 页面渲染成 base64 JPEG"""
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("jpeg", jpg_quality=88)
    return base64.b64encode(img_bytes).decode("utf-8")


def _render_page_to_pil(doc: fitz.Document, page_idx: int) -> Image.Image:
    """📘 把 PDF 页面渲染成 PIL Image"""
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    return Image.open(io.BytesIO(img_bytes))


def _render_page_to_jpeg_bytes(doc: fitz.Document, page_idx: int) -> bytes:
    """📘 把 PDF 页面渲染成 JPEG bytes"""
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("jpeg", jpg_quality=92)


def _render_page_to_cv(doc: fitz.Document, page_idx: int) -> np.ndarray:
    """📘 把 PDF 页面渲染成 OpenCV numpy array (BGR)"""
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    nparr = np.frombuffer(img_bytes, np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)


def _process_page_cv_ocr(
    doc: fitz.Document,
    page_idx: int,
    vision_llm=None,
) -> Tuple[dict, List[dict]]:
    """
    📘 教学笔记：处理单页（CV + OCR 混合方案）

    流程：
    1. 渲染页面为图片
    2. OpenCV 检测表格线 → 行列网格
    3. RapidOCR 全页 OCR → 文字 + 位置
    4. 文字分配到单元格
    5. 检测图片区域
    6. 如果没检测到表格线 → fallback 到 Vision LLM

    返回: (page_structure, translation_items)
    """
    # 1. 渲染
    cv_img = _render_page_to_cv(doc, page_idx)
    pil_img = _render_page_to_pil(doc, page_idx)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    h, w = cv_img.shape[:2]

    # 2. OpenCV 表格线检测
    h_positions, v_positions, table_mask = _detect_table_lines(gray)
    has_table = len(h_positions) >= 2 and len(v_positions) >= 2

    logger.info(
        f"第 {page_idx + 1} 页: "
        f"{'检测到表格' if has_table else '未检测到表格线'} "
        f"({len(h_positions)} 水平线, {len(v_positions)} 垂直线)"
    )

    # 3. RapidOCR 全页 OCR
    tmp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    cv2.imwrite(tmp_file.name, cv_img)
    tmp_file.close()

    ocr_results = _ocr_page_subprocess(tmp_file.name)
    os.unlink(tmp_file.name)

    logger.info(f"第 {page_idx + 1} 页: OCR 识别到 {len(ocr_results)} 个文字块")

    if has_table and ocr_results:
        # ===== CV + OCR 路径 =====
        return _process_with_cv(
            cv_img, gray, pil_img, h_positions, v_positions,
            ocr_results, page_idx, h, w,
        )
    elif ocr_results:
        # ===== 纯 OCR 路径（无表格线，但有文字）=====
        # 尝试 Vision LLM fallback
        if vision_llm:
            return _process_with_vision_fallback(
                doc, page_idx, pil_img, vision_llm, ocr_results,
            )
        else:
            # 没有 Vision LLM，把所有 OCR 文字作为段落
            return _process_ocr_only(ocr_results, page_idx, h, w)
    else:
        # ===== 空白页 =====
        return {"page_type": "empty", "elements": []}, []


def _group_texts_by_line(ocr_texts: List[dict], line_threshold: int = 15) -> str:
    """
    📘 教学笔记：把 OCR 文字块按 Y 坐标分组成行

    同一行的文字（Y 坐标差 < threshold）用空格连接，
    不同行的文字用换行符连接。

    这样 "Name" 和 "ZHUYUHENG" 在同一行会变成 "Name ZHUYUHENG"，
    而 "Name ZHUYUHENG" 和 "Sex MALE" 在不同行会用换行分隔。
    """
    if not ocr_texts:
        return ""

    # 按 Y 坐标排序
    sorted_texts = sorted(ocr_texts, key=lambda t: (t["bbox"][1], t["bbox"][0]))

    lines = []
    current_line = [sorted_texts[0]]
    current_y = sorted_texts[0]["bbox"][1]

    for t in sorted_texts[1:]:
        t_y = t["bbox"][1]
        if abs(t_y - current_y) <= line_threshold:
            # 同一行
            current_line.append(t)
        else:
            # 新行
            # 按 X 坐标排序当前行
            current_line.sort(key=lambda x: x["bbox"][0])
            lines.append(" ".join(x["text"] for x in current_line))
            current_line = [t]
            current_y = t_y

    # 最后一行
    current_line.sort(key=lambda x: x["bbox"][0])
    lines.append(" ".join(x["text"] for x in current_line))

    return "\n".join(lines)


def _process_with_cv(
    cv_img: np.ndarray,
    gray: np.ndarray,
    pil_img: Image.Image,
    h_positions: List[int],
    v_positions: List[int],
    ocr_results: List[dict],
    page_idx: int,
    img_h: int,
    img_w: int,
) -> Tuple[dict, List[dict]]:
    """
    📘 CV + OCR 混合处理：表格线 + OCR 文字 → 结构化数据

    📘 v6 核心路径：
    1. 用主要垂直线构建基础网格
    2. 对每一行，检测该行范围内实际存在的垂直线
       → 每行可以有不同的列数（反映合并单元格）
    3. OCR 文字按位置分配到对应单元格
    """
    # 1. 构建基础网格
    grid = _build_cell_grid(h_positions, v_positions, img_h, img_w)
    if not grid:
        return _process_ocr_only(ocr_results, page_idx, img_h, img_w)

    # 2. Per-row vertical line detection
    # 📘 对每一行，检测该行范围内哪些垂直线实际存在
    # 📘 用 binary 图像直接检测（而不是形态学处理后的 mask），
    # 因为细线在形态学处理后可能被削弱。
    _, binary_for_check = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)

    row_structures = []  # 每行的实际单元格列表
    base_cols = len(grid[0]) if grid else 0

    for r_idx, row in enumerate(grid):
        y1 = row[0]["y1"]
        y2 = row[0]["y2"]
        row_height = y2 - y1

        # 📘 检查每个内部垂直线在这一行是否存在
        actual_cells = []
        current_x1 = row[0]["x1"]

        for c_idx in range(base_cols):
            cell = row[c_idx]
            border_x = cell["x2"]  # 右边界

            # 最后一列的右边界是表格右边框，始终存在
            if c_idx == base_cols - 1:
                actual_cells.append({
                    "x1": current_x1, "y1": y1,
                    "x2": border_x, "y2": y2,
                })
                break

            # 📘 检查这个边界在当前行范围内是否有垂直线
            # 用窄窗口（±2px）直接在 binary 图像上检测
            check_y1 = y1 + min(5, row_height // 4)
            check_y2 = y2 - min(5, row_height // 4)
            if check_y2 <= check_y1:
                check_y1, check_y2 = y1, y2

            x_start = max(0, border_x - 2)
            x_end = min(binary_for_check.shape[1], border_x + 2)
            strip = binary_for_check[check_y1:check_y2, x_start:x_end]
            line_ratio = np.count_nonzero(strip) / max(strip.size, 1)

            # 📘 阈值 0.1：细线在 4px 宽的窗口中占比约 25%，
            # 但考虑到线可能不完全连续，用 0.1 作为阈值
            if line_ratio >= 0.1:
                actual_cells.append({
                    "x1": current_x1, "y1": y1,
                    "x2": border_x, "y2": y2,
                })
                current_x1 = border_x
            # else: 没有垂直线 → 合并到下一列

        row_structures.append(actual_cells)

    # 3. 分配 OCR 文字到单元格
    table_bbox = (
        grid[0][0]["x1"], grid[0][0]["y1"],
        grid[-1][-1]["x2"], grid[-1][-1]["y2"],
    )
    outside_texts = []

    for cells in row_structures:
        for cell in cells:
            cell["texts"] = []

    for ocr_item in ocr_results:
        bbox = ocr_item["bbox"]
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2

        if not (table_bbox[0] <= cx <= table_bbox[2] and
                table_bbox[1] <= cy <= table_bbox[3]):
            outside_texts.append(ocr_item)
            continue

        assigned = False
        for cells in row_structures:
            for cell in cells:
                if (cell["x1"] <= cx <= cell["x2"] and
                    cell["y1"] <= cy <= cell["y2"]):
                    cell["texts"].append(ocr_item)
                    assigned = True
                    break
            if assigned:
                break

        if not assigned:
            outside_texts.append(ocr_item)

    # 4. 检测图片区域
    image_regions = _detect_image_regions(cv_img, gray, table_bbox, ocr_results)
    for region in image_regions:
        cropped = _crop_image_region(pil_img, region["bbox_pct"])
        if cropped:
            region["cropped_image"] = cropped

    # 5. 构建 page_structure 和 translation_items
    elements = []
    items = []

    # 📘 表格外上方/下方的文字
    above_texts = [t for t in outside_texts if t["bbox"][3] < table_bbox[1]]
    below_texts = [t for t in outside_texts if t["bbox"][1] > table_bbox[3]]
    side_texts = [t for t in outside_texts
                  if t not in above_texts and t not in below_texts]

    # 上方段落
    for t_idx, t in enumerate(sorted(above_texts, key=lambda x: x["bbox"][1])):
        elem = {
            "type": "paragraph", "text": t["text"],
            "bold": False, "align": "center", "font_size": "normal",
        }
        elements.append(elem)
        key = f"pg{page_idx}_above{t_idx}_para"
        items.append({
            "key": key, "type": "pdf_block",
            "full_text": t["text"], "is_empty": False,
            "dominant_format": {
                "font_name": "Unknown", "font_size": 11,
                "font_color": "#000000", "bold": False,
            },
        })

    # 📘 图片区域（表格上方）
    for region in [r for r in image_regions if r["bbox_px"][3] < table_bbox[1]]:
        elements.append(region)    # 📘 表格 — 用 per-row 结构
    # 计算最大列数（用于 col_widths）
    max_cols = max(len(cells) for cells in row_structures) if row_structures else 0
    total_width = table_bbox[2] - table_bbox[0]

    # 📘 col_widths 用最多列的那一行来计算
    col_widths = []
    for cells in row_structures:
        if len(cells) == max_cols:
            col_widths = [
                round((c["x2"] - c["x1"]) / total_width * 100, 1)
                for c in cells
            ]
            break
    if not col_widths and max_cols > 0:
        col_widths = [round(100 / max_cols, 1)] * max_cols

    table_elem = {
        "type": "table",
        "col_widths": col_widths,
        "border": "all",
        "rows": [],
    }

    for r_idx, cells in enumerate(row_structures):
        row_cells = []
        # 📘 计算 colspan（相对于 max_cols）
        for c_idx, cell in enumerate(cells):
            cell_texts = cell.get("texts", [])
            cell_texts.sort(key=lambda t: (t["bbox"][1], t["bbox"][0]))
            # 📘 v6.1: 按 Y 坐标分组，同一行用空格连接，不同行用换行
            combined_text = _group_texts_by_line(cell_texts)

            # 📘 计算 colspan
            cell_width = cell["x2"] - cell["x1"]
            if max_cols == len(cells):
                colspan = 1
            else:
                # 估算这个 cell 跨了几列
                colspan = max(1, round(cell_width / (total_width / max_cols)))

            cell_info = {
                "text": combined_text,
                "colspan": colspan,
                "rowspan": 1,
                "bold": False,
                "align": "left",
            }
            row_cells.append(cell_info)

            if combined_text.strip():
                key = f"pg{page_idx}_e0_r{r_idx}_c{c_idx}"
                items.append({
                    "key": key, "type": "table_cell",
                    "full_text": combined_text, "is_empty": False,
                    "dominant_format": {
                        "font_name": "Unknown", "font_size": 10,
                        "font_color": "#000000", "bold": False,
                    },
                })

        table_elem["rows"].append({"cells": row_cells})

    elements.append(table_elem)

    # 📘 所有图片区域（包括表格内部的国徽、二维码等）
    # 按 Y 坐标排序，放在表格后面
    for region in sorted(image_regions, key=lambda r: r["bbox_px"][1]):
        # 跳过已经放在表格上方的
        if region["bbox_px"][3] < table_bbox[1]:
            continue
        elements.append(region)

    # 下方段落
    for t_idx, t in enumerate(sorted(below_texts, key=lambda x: x["bbox"][1])):
        elem = {
            "type": "paragraph", "text": t["text"],
            "bold": False, "align": "left", "font_size": "normal",
        }
        elements.append(elem)
        key = f"pg{page_idx}_below{t_idx}_para"
        items.append({
            "key": key, "type": "pdf_block",
            "full_text": t["text"], "is_empty": False,
            "dominant_format": {
                "font_name": "Unknown", "font_size": 11,
                "font_color": "#000000", "bold": False,
            },
        })

    # 侧面文字
    for t_idx, t in enumerate(side_texts):
        key = f"pg{page_idx}_side{t_idx}_para"
        items.append({
            "key": key, "type": "pdf_block",
            "full_text": t["text"], "is_empty": False,
            "dominant_format": {
                "font_name": "Unknown", "font_size": 11,
                "font_color": "#000000", "bold": False,
            },
        })

    page_structure = {
        "page_type": "table" if row_structures else "text",
        "elements": elements,
    }

    return page_structure, items


def _process_ocr_only(
    ocr_results: List[dict],
    page_idx: int,
    img_h: int,
    img_w: int,
) -> Tuple[dict, List[dict]]:
    """📘 纯 OCR 路径：没有表格线，所有文字作为段落"""
    elements = []
    items = []

    # 按 Y 坐标排序
    sorted_results = sorted(ocr_results, key=lambda t: (t["bbox"][1], t["bbox"][0]))

    for t_idx, t in enumerate(sorted_results):
        elem = {
            "type": "paragraph",
            "text": t["text"],
            "bold": False,
            "align": "left",
            "font_size": "normal",
        }
        elements.append(elem)
        key = f"pg{page_idx}_e{t_idx}_para"
        items.append({
            "key": key,
            "type": "pdf_block",
            "full_text": t["text"],
            "is_empty": False,
            "dominant_format": {
                "font_name": "Unknown", "font_size": 11,
                "font_color": "#000000", "bold": False,
            },
        })

    return {"page_type": "text", "elements": elements}, items


def _process_with_vision_fallback(
    doc: fitz.Document,
    page_idx: int,
    pil_img: Image.Image,
    vision_llm,
    ocr_results: List[dict],
) -> Tuple[dict, List[dict]]:
    """
    📘 Vision LLM Fallback：无表格线时用 LLM 识别结构

    📘 这里有个巧妙的设计：
    即使用 Vision LLM 识别结构，文字内容仍然用 OCR 的结果。
    因为 OCR 的文字识别比 LLM 更准确（尤其是数字、日期等）。
    LLM 只负责理解布局结构（哪些是表格、哪些是段落）。
    """
    image_b64 = _render_page_to_base64(doc, page_idx)
    response = _call_vision_llm(vision_llm, image_b64, STRUCTURE_RECOGNITION_PROMPT)

    if not response:
        return _process_ocr_only(ocr_results, page_idx, *pil_img.size[::-1])

    structure = _parse_structure_json(response)
    if not structure:
        return _process_ocr_only(ocr_results, page_idx, *pil_img.size[::-1])

    # 📘 用 Vision LLM 的结构，但提取翻译单元
    items = []
    elements = structure.get("elements", [])

    for elem_idx, elem in enumerate(elements):
        elem_type = elem.get("type", "")

        if elem_type == "table":
            rows = elem.get("rows", [])
            for row_idx, row in enumerate(rows):
                cells = row.get("cells", row) if isinstance(row, dict) else row
                if isinstance(cells, dict):
                    cells = cells.get("cells", [])
                for col_idx, cell in enumerate(cells):
                    cell_text = cell.get("text", "").strip()
                    if cell_text:
                        key = f"pg{page_idx}_e{elem_idx}_r{row_idx}_c{col_idx}"
                        items.append({
                            "key": key,
                            "type": "table_cell",
                            "full_text": cell_text,
                            "is_empty": False,
                            "dominant_format": {
                                "font_name": "Unknown", "font_size": 10,
                                "font_color": "#000000",
                                "bold": cell.get("bold", False),
                            },
                        })

        elif elem_type == "paragraph":
            para_text = elem.get("text", "").strip()
            if para_text:
                key = f"pg{page_idx}_e{elem_idx}_para"
                items.append({
                    "key": key,
                    "type": "pdf_block",
                    "full_text": para_text,
                    "is_empty": False,
                    "dominant_format": {
                        "font_name": "Unknown", "font_size": 11,
                        "font_color": "#000000",
                        "bold": elem.get("bold", False),
                    },
                })

        elif elem_type == "image_region":
            bbox_pct = elem.get("bbox_pct")
            if bbox_pct:
                cropped = _crop_image_region(pil_img, bbox_pct)
                if cropped:
                    elem["cropped_image"] = cropped

    return structure, items


# =============================================================
# 📘 公开接口
# =============================================================

def parse_scan_pdf(filepath: str, vision_llm=None) -> Dict[str, Any]:
    """
    📘 教学笔记：扫描件 PDF 解析主函数（v6 — CV + OCR 混合方案）

    流程：
    1. 逐页渲染 PDF 为图片
    2. OpenCV 检测表格线 → 精确的行列网格
    3. RapidOCR（subprocess）全页 OCR → 精确的文字 + 位置
    4. 文字按位置分配到单元格
    5. 检测图片区域（LOGO/二维码/印章等）
    6. 无表格线时 fallback 到 Vision LLM

    📘 v6 vs v5 的区别：
    - v5: 全靠 Vision LLM（位置不精确，结构靠猜）
    - v6: CV 精确检测表格线 + OCR 精确识别文字 + LLM 只做 fallback
    - v6 的表格列宽、行高、合并单元格都是像素级精确的
    """
    logger.info(f"开始扫描件解析 (v6 CV+OCR): {filepath}")
    print(f"[🔍 扫描件识别] CV + OCR 混合方案...", flush=True)

    doc = fitz.open(filepath)
    num_pages = len(doc)

    all_items = []
    page_structures = []
    page_images = []

    for page_idx in range(num_pages):
        print(
            f"  [🔍 第 {page_idx + 1}/{num_pages} 页] "
            f"OpenCV 表格检测 + RapidOCR 文字识别...",
            flush=True,
        )

        # 保存整页图片（用于 Word 文档中的参考图）
        page_images.append(_render_page_to_jpeg_bytes(doc, page_idx))

        # 处理单页
        page_structure, page_items = _process_page_cv_ocr(
            doc, page_idx, vision_llm=vision_llm,
        )

        page_structures.append(page_structure)
        all_items.extend(page_items)

        elem_count = len(page_structure.get("elements", []))
        logger.info(
            f"第 {page_idx + 1} 页: {elem_count} 个元素, "
            f"{len(page_items)} 个翻译单元 "
            f"(类型: {page_structure.get('page_type', '?')})"
        )

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
