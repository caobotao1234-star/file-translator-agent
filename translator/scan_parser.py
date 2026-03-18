# translator/scan_parser.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 解析器（v7 — CV + OCR + Vision LLM 混合方案）
# =============================================================
# 📘 v6 的问题：
#   - 纯 CV 只能检测到画出的线，隐藏的列分隔线看不到
#   - 列宽只能按检测到的线来算，不反映真实布局
#   - 图片只是堆在文档末尾，没有放进正确的位置
#   - 文字对齐（左/右/居中）无法判断
#
# 📘 v7 混合方案（三层协作）：
#   1. OpenCV：检测可见表格线 + 图片区域（像素级精确）
#   2. RapidOCR（subprocess 隔离）：精确文字 + 位置
#   3. Vision LLM（核心）：理解完整表格结构
#      - 输入：页面图片 + CV 检测到的线 + OCR 文字列表
#      - 输出：完整表格结构（含隐藏列、列宽比例、合并单元格、
#              文字对齐、图片在哪个单元格）
#
# 📘 为什么 Vision LLM 是核心？
#   人类看文档能理解"这里虽然没画线，但其实是两列"，
#   Vision LLM 也能做到这一点。CV 和 OCR 提供精确数据，
#   LLM 提供"理解力"——各司其职。
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
MIN_H_LINE_LENGTH = 80
MIN_V_LINE_LENGTH = 40
LINE_MERGE_THRESHOLD = 15
MIN_CELL_SIZE = 20


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
# 📘 第一层：OpenCV 表格线检测 + 图片区域检测
# =============================================================

def _detect_table_lines(gray_img: np.ndarray) -> Tuple[List[int], List[int], np.ndarray]:
    """
    📘 用 OpenCV 形态学操作检测可见的表格线
    返回：(水平线Y坐标列表, 垂直线X坐标列表, 表格区域mask)
    """
    _, binary = cv2.threshold(gray_img, 180, 255, cv2.THRESH_BINARY_INV)

    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (MIN_H_LINE_LENGTH, 1))
    h_lines_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)

    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, MIN_V_LINE_LENGTH))
    v_lines_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

    h_positions = _extract_line_positions(h_lines_mask, axis="h", min_length=MIN_H_LINE_LENGTH)
    v_positions = _extract_line_positions(v_lines_mask, axis="v", min_length=MIN_V_LINE_LENGTH)

    table_mask = cv2.add(h_lines_mask, v_lines_mask)
    return h_positions, v_positions, table_mask


def _extract_line_positions(mask: np.ndarray, axis: str, min_length: int) -> List[int]:
    """📘 从线条 mask 中提取线条位置，合并相近的线条"""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    positions = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if axis == "h" and w >= min_length:
            positions.append(y + h // 2)
        elif axis == "v" and h >= min_length:
            positions.append(x + w // 2)

    positions.sort()

    merged = []
    for p in positions:
        if merged and abs(p - merged[-1]) < LINE_MERGE_THRESHOLD:
            merged[-1] = (merged[-1] + p) // 2
        else:
            merged.append(p)
    return merged


def _detect_image_regions(
    img: np.ndarray,
    gray: np.ndarray,
    ocr_results: List[dict],
) -> List[dict]:
    """
    📘 检测非文字的图片区域（LOGO、二维码、印章等）
    用颜色饱和度检测：文字和表格线通常是黑白的，
    而 LOGO、印章、照片通常有颜色。
    """
    regions = []
    h, w = img.shape[:2]

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    _, sat_mask = cv2.threshold(saturation, 50, 255, cv2.THRESH_BINARY)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    sat_mask = cv2.dilate(sat_mask, kernel, iterations=2)

    contours, _ = cv2.findContours(sat_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cw * ch

        if cw < 50 or ch < 50:
            continue
        if area > h * w * 0.5:
            continue

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
    """📘 从页面图片中裁剪指定区域"""
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
# 📘 第二层：RapidOCR 文字识别（subprocess 隔离）
# =============================================================

def _ocr_page_subprocess(img_path: str) -> List[dict]:
    """
    📘 通过 subprocess 调用 RapidOCR
    PyQt6 和 onnxruntime 在 Python 3.14 上有 DLL 冲突，
    subprocess 隔离 DLL 加载。
    返回: [{"text": "...", "bbox": [x1, y1, x2, y2], "confidence": 0.95}, ...]
    """
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
    script_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    )
    script_file.write(script_content)
    script_file.close()

    python_exe = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "volcengine", "Scripts", "python.exe",
    )
    if not os.path.exists(python_exe):
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
# 📘 第三层：Vision LLM 结构理解（v7 核心）
# =============================================================
# 📘 教学笔记：为什么 Vision LLM 是核心？
#   CV 只能看到画出来的线，看不到"隐藏的列分隔线"。
#   但人类看文档能理解"这里虽然没画线，但其实是两列"。
#   Vision LLM 也能做到这一点。
#
#   所以 v7 的策略是：
#   - CV 提供精确的可见线位置和图片区域
#   - OCR 提供精确的文字内容和位置
#   - Vision LLM 看图 + 参考 CV/OCR 数据 → 输出完整结构
#     （包括隐藏列、列宽比例、合并单元格、文字对齐、图片位置）
# =============================================================

HYBRID_STRUCTURE_PROMPT = """\
你是一个专业的文档结构识别助手。请仔细观察这张文档图片，结合下面提供的 CV 检测数据和 OCR 文字数据，精确识别完整的文档结构。

## CV 检测到的可见表格线
{cv_info}

## CV 检测到的图片区域
{image_info}

## OCR 识别到的文字（按位置排列）
{ocr_info}

## 你的任务
结合图片和上述数据，输出完整的文档结构 JSON。

**关键要求：**
1. **隐藏列**：很多表格有不画线的内部列分隔（比如"标签: 值"格式），你必须识别出这些隐藏的列。
2. **列宽比例**：col_widths 必须精确反映原文的视觉比例。标签列（如"父亲详情"）通常非常窄（8-15%），值列较宽。仔细测量原图中每列的实际像素宽度比例。
3. **每个单元格独立控制边框**：用 borders 字段指定四条边是否有线。只在原文画了线的地方标 true，隐藏的列分隔线标 false。
4. **合并单元格**：跨多列或多行的单元格必须用 colspan/rowspan。底部大段文字通常跨全部列。被合并覆盖的单元格不要输出。
5. **图片位置**：表格内的图片用 has_image + image_index 标注。用 image_position 指定图片相对于文字的位置："before"（图片在文字上方）、"after"（图片在文字下方）。
6. **每行独立对齐**：一个单元格内可能有多行文字，每行的对齐方式可能不同。用 lines 数组，每个元素是 {{"text": "...", "align": "left/center/right"}}。如果所有行对齐一致，也可以用简写 "text" + "align"。
7. **竖版文字**：如果原文中某个单元格的文字是竖排的（从上到下书写），标注 "vertical": true。
8. **文字内容**：优先使用 OCR 数据中的文字。

## 输出格式
```json
{{
  "page_type": "table",
  "elements": [
    {{
      "type": "table",
      "col_widths": [8, 20, 8, 20, 8, 18, 18],
      "rows": [
        {{
          "cells": [
            {{"text": "标签", "colspan": 1, "rowspan": 1, "bold": true, "align": "center", "borders": {{"top": true, "bottom": true, "left": true, "right": false}}}},
            {{"text": "值\\n第二行", "colspan": 2, "rowspan": 1, "bold": false, "align": "left", "borders": {{"top": true, "bottom": true, "left": false, "right": true}}, "has_image": true, "image_index": 0, "image_position": "after"}},
            {{"lines": [{{"text": "靠右", "align": "right"}}, {{"text": "居中", "align": "center"}}], "colspan": 1, "rowspan": 1, "bold": false, "borders": {{"top": true, "bottom": true, "left": true, "right": true}}}}
          ]
        }}
      ]
    }},
    {{"type": "paragraph", "text": "段落文字", "bold": false, "align": "center", "font_size": "normal"}},
    {{"type": "image_region", "image_index": 1, "description": "二维码"}}
  ]
}}
```

**注意：**
- col_widths 总和必须等于 100
- 只输出 JSON，不要其他文字
- 每个单元格必须有 borders 字段
- 不要给所有单元格都加框线，只在原文有线的地方加"""


def _call_vision_llm(vision_llm, image_b64: str, prompt: str) -> Optional[str]:
    """📘 调用 Vision LLM"""
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
    """
    📘 从 Vision LLM 响应中提取 JSON（增强容错版）

    📘 教学笔记：LLM 返回的 JSON 经常有各种小问题：
    1. 前后有多余文字（"好的，这是结构：{...}"）
    2. 被 markdown code block 包裹（```json ... ```）
    3. 有 trailing comma（最后一个元素后面多了逗号）
    4. 有注释（// 或 /* */）
    所以需要多种策略尝试提取。
    """
    text = response.strip()

    # 📘 策略1：去掉 markdown code block
    if "```" in text:
        # 找到第一个 ``` 和最后一个 ``` 之间的内容
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            # 去掉 ```json 的 "json" 标记
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break

    # 📘 策略2：找到第一个 { 和最后一个 } 之间的内容
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        text = text[first_brace:last_brace + 1]

    # 📘 策略3：直接尝试解析
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 📘 策略4：修复 trailing comma（JSON 标准不允许，但 LLM 经常生成）
    import re
    fixed = re.sub(r',\s*([}\]])', r'\1', text)
    try:
        result = json.loads(fixed)
        if isinstance(result, dict):
            logger.debug("JSON 通过 trailing comma 修复后解析成功")
            return result
    except json.JSONDecodeError as e:
        logger.warning(f"JSON 解析失败（已尝试修复）: {e}")
        logger.debug(f"JSON 原文前 800 字符: {text[:800]}")

    return None


def _build_cv_info(h_positions: List[int], v_positions: List[int], img_h: int, img_w: int) -> str:
    """📘 把 CV 检测结果格式化为文字描述，供 Vision LLM 参考"""
    if not h_positions and not v_positions:
        return "未检测到可见的表格线。"

    lines = []
    if h_positions:
        h_pcts = [round(y / img_h * 100, 1) for y in h_positions]
        lines.append(f"水平线 {len(h_positions)} 条，Y 位置（页面百分比）: {h_pcts}")
    if v_positions:
        v_pcts = [round(x / img_w * 100, 1) for x in v_positions]
        lines.append(f"垂直线 {len(v_positions)} 条，X 位置（页面百分比）: {v_pcts}")

    if len(h_positions) >= 2 and len(v_positions) >= 2:
        rows = len(h_positions) - 1
        cols = len(v_positions) - 1
        lines.append(f"可见网格: {rows} 行 × {cols} 列")
        # 📘 计算可见列宽比例
        total_w = v_positions[-1] - v_positions[0]
        if total_w > 0:
            widths = []
            for i in range(len(v_positions) - 1):
                w_pct = round((v_positions[i + 1] - v_positions[i]) / total_w * 100, 1)
                widths.append(w_pct)
            lines.append(f"可见列宽比例: {widths}")
        lines.append(
            "⚠️ 注意：这只是画出来的线。表格内部可能还有隐藏的列分隔（没画线但视觉上是分开的列）。"
            "请仔细观察图片，识别所有列（包括隐藏列）。"
        )

    return "\n".join(lines)


def _build_image_info(image_regions: List[dict], img_h: int, img_w: int) -> str:
    """📘 把图片区域信息格式化为文字描述"""
    if not image_regions:
        return "未检测到图片区域。"

    lines = []
    for i, region in enumerate(image_regions):
        bbox = region["bbox_px"]
        x_pct = round(bbox[0] / img_w * 100, 1)
        y_pct = round(bbox[1] / img_h * 100, 1)
        w_pct = round((bbox[2] - bbox[0]) / img_w * 100, 1)
        h_pct = round((bbox[3] - bbox[1]) / img_h * 100, 1)
        lines.append(
            f"图片 {i}: 位置 ({x_pct}%, {y_pct}%), "
            f"尺寸 ({w_pct}% × {h_pct}%), "
            f"描述: {region.get('description', '未知')}"
        )
    return "\n".join(lines)


def _build_ocr_info(ocr_results: List[dict], img_h: int, img_w: int) -> str:
    """📘 把 OCR 结果格式化为文字描述，按位置排列"""
    if not ocr_results:
        return "未识别到文字。"

    # 按 Y 坐标分组成行，再按 X 排序
    sorted_texts = sorted(ocr_results, key=lambda t: (t["bbox"][1], t["bbox"][0]))

    lines = []
    for t in sorted_texts:
        bbox = t["bbox"]
        x_pct = round(bbox[0] / img_w * 100, 1)
        y_pct = round(bbox[1] / img_h * 100, 1)
        lines.append(f"  ({x_pct}%, {y_pct}%) \"{t['text']}\"")

    return "\n".join(lines)


# =============================================================
# 📘 渲染辅助函数
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


# =============================================================
# 📘 v7 核心：混合处理流程
# =============================================================

def _process_page_hybrid(
    doc: fitz.Document,
    page_idx: int,
    vision_llm=None,
) -> Tuple[dict, List[dict]]:
    """
    📘 教学笔记：v7 混合处理单页

    流程：
    1. 渲染页面为图片
    2. OpenCV 检测可见表格线 + 图片区域
    3. RapidOCR 全页 OCR → 文字 + 位置
    4. Vision LLM 看图 + CV/OCR 数据 → 完整结构
    5. 裁剪图片区域，附加到结构中

    📘 关键改进（vs v6）：
    - Vision LLM 是主力，不再是 fallback
    - CV 数据作为辅助信息传给 LLM，帮助它更精确
    - 图片放在正确的位置（表格内/外），不再堆在末尾
    """
    # 1. 渲染
    cv_img = _render_page_to_cv(doc, page_idx)
    pil_img = _render_page_to_pil(doc, page_idx)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    img_h, img_w = cv_img.shape[:2]

    # 2. OpenCV 检测
    h_positions, v_positions, table_mask = _detect_table_lines(gray)
    has_visible_table = len(h_positions) >= 2 and len(v_positions) >= 2

    logger.info(
        f"第 {page_idx + 1} 页: "
        f"{'检测到可见表格线' if has_visible_table else '未检测到表格线'} "
        f"({len(h_positions)} 水平线, {len(v_positions)} 垂直线)"
    )

    # 3. 检测图片区域
    image_regions = _detect_image_regions(cv_img, gray, [])
    for region in image_regions:
        cropped = _crop_image_region(pil_img, region["bbox_pct"])
        if cropped:
            region["cropped_image"] = cropped

    logger.info(f"第 {page_idx + 1} 页: 检测到 {len(image_regions)} 个图片区域")

    # 4. RapidOCR 全页 OCR
    tmp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    cv2.imwrite(tmp_file.name, cv_img)
    tmp_file.close()

    ocr_results = _ocr_page_subprocess(tmp_file.name)
    os.unlink(tmp_file.name)

    logger.info(f"第 {page_idx + 1} 页: OCR 识别到 {len(ocr_results)} 个文字块")

    if not ocr_results and not image_regions:
        return {"page_type": "empty", "elements": []}, []

    # 5. Vision LLM 结构理解
    if vision_llm:
        return _process_with_hybrid_llm(
            doc, page_idx, pil_img,
            vision_llm, ocr_results, image_regions,
            h_positions, v_positions, img_h, img_w,
        )
    else:
        # 📘 没有 Vision LLM → 纯 OCR 段落模式
        return _process_ocr_only(ocr_results, page_idx, img_h, img_w)


def _process_with_hybrid_llm(
    doc: fitz.Document,
    page_idx: int,
    pil_img: Image.Image,
    vision_llm,
    ocr_results: List[dict],
    image_regions: List[dict],
    h_positions: List[int],
    v_positions: List[int],
    img_h: int,
    img_w: int,
) -> Tuple[dict, List[dict]]:
    """
    📘 v7 核心：CV + OCR + Vision LLM 混合处理

    把 CV 检测到的线、OCR 文字、图片区域信息都传给 Vision LLM，
    让它结合图片理解完整的文档结构。
    """
    # 构建辅助信息
    cv_info = _build_cv_info(h_positions, v_positions, img_h, img_w)
    image_info = _build_image_info(image_regions, img_h, img_w)
    ocr_info = _build_ocr_info(ocr_results, img_h, img_w)

    # 填充 prompt
    prompt = HYBRID_STRUCTURE_PROMPT.format(
        cv_info=cv_info,
        image_info=image_info,
        ocr_info=ocr_info,
    )

    # 调用 Vision LLM
    print(f"  [🧠 Vision LLM] 分析文档结构（含隐藏列、图片位置）...", flush=True)
    image_b64 = _render_page_to_base64(doc, page_idx)
    response = _call_vision_llm(vision_llm, image_b64, prompt)

    if not response:
        logger.warning(f"第 {page_idx + 1} 页: Vision LLM 无响应，回退到纯 OCR")
        return _process_ocr_only(ocr_results, page_idx, img_h, img_w)

    structure = _parse_structure_json(response)
    if not structure:
        logger.warning(f"第 {page_idx + 1} 页: Vision LLM JSON 解析失败，回退到纯 OCR")
        return _process_ocr_only(ocr_results, page_idx, img_h, img_w)

    # 📘 后处理：把裁剪好的图片附加到结构中
    elements = structure.get("elements", [])
    items = []

    for elem_idx, elem in enumerate(elements):
        elem_type = elem.get("type", "")

        if elem_type == "table":
            rows = elem.get("rows", [])
            for row_idx, row in enumerate(rows):
                cells = row.get("cells", row) if isinstance(row, dict) else row
                if isinstance(cells, dict):
                    cells = cells.get("cells", [])
                for col_idx, cell in enumerate(cells):
                    # 📘 附加图片到单元格
                    if cell.get("has_image") and cell.get("image_index") is not None:
                        img_idx = cell["image_index"]
                        if 0 <= img_idx < len(image_regions):
                            cell["cropped_image"] = image_regions[img_idx].get("cropped_image")

                    # 📘 v7.1: 支持 "lines" 数组格式（per-line alignment）
                    cell_lines = cell.get("lines")
                    if cell_lines and isinstance(cell_lines, list):
                        cell_text = "\n".join(
                            l.get("text", "") for l in cell_lines
                        ).strip()
                    else:
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
            # 📘 独立图片区域（表格外的）
            img_idx = elem.get("image_index")
            if img_idx is not None and 0 <= img_idx < len(image_regions):
                elem["cropped_image"] = image_regions[img_idx].get("cropped_image")
                elem["bbox_pct"] = image_regions[img_idx].get("bbox_pct")

    return structure, items


def _process_ocr_only(
    ocr_results: List[dict],
    page_idx: int,
    img_h: int,
    img_w: int,
) -> Tuple[dict, List[dict]]:
    """📘 纯 OCR 路径：没有 Vision LLM，所有文字作为段落"""
    elements = []
    items = []

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


# =============================================================
# 📘 公开接口
# =============================================================

def parse_scan_pdf(filepath: str, vision_llm=None) -> Dict[str, Any]:
    """
    📘 教学笔记：扫描件 PDF 解析主函数（v7 — CV + OCR + Vision LLM 混合方案）

    流程：
    1. 逐页渲染 PDF 为图片
    2. OpenCV 检测可见表格线 + 图片区域（像素级精确）
    3. RapidOCR（subprocess）全页 OCR → 精确文字 + 位置
    4. Vision LLM 看图 + CV/OCR 数据 → 完整结构
       （含隐藏列、列宽比例、合并单元格、图片位置）
    5. 裁剪图片区域，附加到结构中

    📘 v7 vs v6 的区别：
    - v6: CV 为主，LLM 只做 fallback → 隐藏列看不到
    - v7: LLM 为主（理解结构），CV/OCR 为辅（提供精确数据）
    - v7 的图片放在正确位置（表格内/外），不再堆在末尾
    """
    logger.info(f"开始扫描件解析 (v7 混合方案): {filepath}")
    print(f"[🔍 扫描件识别] CV + OCR + Vision LLM 混合方案...", flush=True)

    doc = fitz.open(filepath)
    num_pages = len(doc)

    all_items = []
    page_structures = []
    page_images = []

    for page_idx in range(num_pages):
        print(
            f"  [🔍 第 {page_idx + 1}/{num_pages} 页] "
            f"CV 检测 + OCR 识别 + Vision LLM 结构分析...",
            flush=True,
        )

        page_images.append(_render_page_to_jpeg_bytes(doc, page_idx))

        page_structure, page_items = _process_page_hybrid(
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
