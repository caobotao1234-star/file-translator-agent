# translator/scan_writer.py
# =============================================================
# 📘 教学笔记：扫描件 PDF 写入器
# =============================================================
# 扫描件的写入和普通 PDF 完全不同：
#   普通 PDF: redaction 擦除文本层 → 写入新文本层
#   扫描件:   图像修复擦除文字 → 在修复后的图片上写入译文
#
# 核心挑战：如何擦除图片上的文字而不留白底？
#
# 策略（方案 2 + 方案 1 组合）：
#   1. 对每个文字 bbox，采样周围像素取主色调
#   2. 如果颜色方差小（纯色/浅渐变）→ 用采样色填充（快）
#   3. 如果颜色方差大（复杂背景）→ 用 cv2.inpaint()（效果好）
#   4. 在修复后的图片上用 PIL 写入译文
#   5. 图片组装回 PDF
#
# 📘 为什么不直接在 PDF 上写文本层？
# 扫描件的"文字"是图片的一部分，PDF 文本层是空的。
# 如果只加文本层，原文图片上的文字还在，会和译文重叠。
# 必须先在图片级别擦除原文，再写入译文。
# =============================================================

import cv2
import numpy as np
import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont
from typing import Dict, Any, List, Optional
from core.logger import get_logger
from translator.format_engine import FormatEngine

logger = get_logger("scan_writer")

# 📘 渲染 DPI 必须和 scan_parser 一致，这样像素坐标才能对上
RENDER_DPI = 150

# 📘 背景采样：在 bbox 外围采样的像素宽度
SAMPLE_MARGIN = 5

# 📘 颜色方差阈值：低于此值用纯色填充，高于此值用 inpainting
COLOR_VARIANCE_THRESHOLD = 300


def _sample_background_color(img: np.ndarray, bbox: List[float]) -> tuple:
    """
    📘 教学笔记：采样文字框周围的背景色

    在 bbox 的四条边外侧各取一条窄带（5px），
    计算这些像素的中位数颜色和方差。

    返回: (median_color, variance)
    - median_color: (B, G, R) 中位数颜色
    - variance: 颜色方差（越大说明背景越复杂）
    """
    h, w = img.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in bbox]

    # 限制在图片范围内
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(w, x1)
    y1 = min(h, y1)

    m = SAMPLE_MARGIN
    samples = []

    # 上边
    if y0 - m >= 0:
        strip = img[max(0, y0 - m):y0, x0:x1]
        if strip.size > 0:
            samples.append(strip.reshape(-1, 3))
    # 下边
    if y1 + m <= h:
        strip = img[y1:min(h, y1 + m), x0:x1]
        if strip.size > 0:
            samples.append(strip.reshape(-1, 3))
    # 左边
    if x0 - m >= 0:
        strip = img[y0:y1, max(0, x0 - m):x0]
        if strip.size > 0:
            samples.append(strip.reshape(-1, 3))
    # 右边
    if x1 + m <= w:
        strip = img[y0:y1, x1:min(w, x1 + m)]
        if strip.size > 0:
            samples.append(strip.reshape(-1, 3))

    if not samples:
        return (255, 255, 255), 0  # 兜底白色

    all_pixels = np.concatenate(samples, axis=0)
    median_color = tuple(int(v) for v in np.median(all_pixels, axis=0))
    variance = float(np.var(all_pixels))

    return median_color, variance


def _erase_text_region(img: np.ndarray, bbox: List[float], bg_color: tuple, variance: float) -> np.ndarray:
    """
    📘 教学笔记：擦除图片上的文字区域

    两种策略：
    1. 纯色/浅渐变背景（方差小）→ 直接用采样色填充，快且干净
    2. 复杂背景（方差大）→ 用 OpenCV inpainting 修复，自动补全纹理

    inpainting 原理：
    给定一个 mask（标记要修复的区域），算法会参考周围像素
    自动"画"出合理的填充内容。对照片、纹理背景效果很好。
    """
    x0, y0, x1, y1 = [int(round(v)) for v in bbox]
    h, w = img.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)

    if x1 <= x0 or y1 <= y0:
        return img

    if variance < COLOR_VARIANCE_THRESHOLD:
        # 📘 方案 2：纯色填充
        img[y0:y1, x0:x1] = bg_color
    else:
        # 📘 方案 1：inpainting
        # 创建 mask：文字区域为白色（255），其他为黑色（0）
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[y0:y1, x0:x1] = 255
        # 📘 inpaintRadius=5: 修复时参考周围 5 像素
        # INPAINT_TELEA: Telea 算法，速度快，效果好
        img = cv2.inpaint(img, mask, inpaintRadius=5, flags=cv2.INPAINT_TELEA)

    return img


def _has_cjk(text: str) -> bool:
    """检测文本是否包含 CJK 字符"""
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3040' <= ch <= '\u30ff' or '\uac00' <= ch <= '\ud7af':
            return True
    return False


def _find_system_font(bold: bool = False, cjk: bool = False) -> str:
    """
    📘 查找系统字体文件路径

    Windows 字体目录: C:/Windows/Fonts/
    优先级：
    - CJK 文本: 微软雅黑 > 宋体
    - 英文文本: Arial > Times New Roman > Calibri
    """
    import os
    font_dir = "C:/Windows/Fonts"

    if cjk:
        candidates = [
            "msyhbd.ttc" if bold else "msyh.ttc",  # 微软雅黑
            "simhei.ttf",   # 黑体
            "simsun.ttc",   # 宋体
        ]
    else:
        candidates = [
            "arialbd.ttf" if bold else "arial.ttf",  # Arial
            "timesbd.ttf" if bold else "times.ttf",   # Times New Roman
            "calibrib.ttf" if bold else "calibri.ttf", # Calibri
        ]

    for name in candidates:
        path = os.path.join(font_dir, name)
        if os.path.exists(path):
            return path

    # 兜底
    fallback = os.path.join(font_dir, "msyh.ttc")
    if os.path.exists(fallback):
        return fallback
    return os.path.join(font_dir, "arial.ttf")


def _draw_text_on_image(
    img: np.ndarray,
    text: str,
    bbox: List[float],
    font_size: float,
    font_color: tuple = (0, 0, 0),
    bold: bool = False,
    alignment: str = "left",
) -> np.ndarray:
    """
    📘 教学笔记：在图片上绘制文字

    用 PIL（Pillow）绘制文字，因为 OpenCV 的 putText 不支持：
    - 中文字符
    - 自动换行
    - 字体样式

    PIL 绘制流程：
    1. numpy → PIL Image
    2. 创建 ImageDraw
    3. 加载 TrueType 字体
    4. 自动换行：按可用宽度拆分文本
    5. 绘制文字
    6. PIL Image → numpy
    """
    x0, y0, x1, y1 = [int(round(v)) for v in bbox]
    box_width = x1 - x0
    box_height = y1 - y0

    if box_width <= 0 or box_height <= 0:
        return img

    # numpy (BGR) → PIL (RGB)
    pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_img)

    # 加载字体
    cjk = _has_cjk(text)
    font_path = _find_system_font(bold=bold, cjk=cjk)
    px_size = int(round(font_size * RENDER_DPI / 72.0))  # pt → px
    px_size = max(8, px_size)

    try:
        font = ImageFont.truetype(font_path, px_size)
    except Exception:
        font = ImageFont.load_default()

    # 📘 自动换行：按可用宽度拆分
    lines = _wrap_text(draw, text, font, box_width)

    # 📘 自动缩小：如果文字放不下，逐步缩小字号
    line_height = px_size * 1.3
    total_height = line_height * len(lines)
    while total_height > box_height and px_size > 6:
        px_size = int(px_size * 0.85)
        try:
            font = ImageFont.truetype(font_path, px_size)
        except Exception:
            break
        lines = _wrap_text(draw, text, font, box_width)
        line_height = px_size * 1.3
        total_height = line_height * len(lines)

    # 绘制每行文字
    # font_color 是 BGR，PIL 需要 RGB
    pil_color = (font_color[2], font_color[1], font_color[0])
    current_y = y0

    for line in lines:
        if current_y + line_height > y1:
            break  # 超出区域就停

        if alignment == "center":
            line_w = draw.textlength(line, font=font)
            line_x = x0 + (box_width - line_w) / 2
        elif alignment == "right":
            line_w = draw.textlength(line, font=font)
            line_x = x1 - line_w
        else:  # left / justify
            line_x = x0

        draw.text((line_x, current_y), line, fill=pil_color, font=font)
        current_y += line_height

    # PIL (RGB) → numpy (BGR)
    result = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    return result


def _wrap_text(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """
    📘 自动换行：按可用宽度拆分文本

    英文按空格拆分单词，中文按字符拆分。
    逐词/逐字累加宽度，超过 max_width 就换行。
    """
    # 先按原有换行符拆分
    paragraphs = text.split("\n")
    all_lines = []

    for para in paragraphs:
        if not para.strip():
            all_lines.append("")
            continue

        # 📘 判断是否主要是 CJK 文本
        cjk_count = sum(1 for c in para if '\u4e00' <= c <= '\u9fff')
        if cjk_count > len(para) * 0.3:
            # CJK 文本：按字符拆分
            current_line = ""
            for char in para:
                test = current_line + char
                w = draw.textlength(test, font=font)
                if w > max_width and current_line:
                    all_lines.append(current_line)
                    current_line = char
                else:
                    current_line = test
            if current_line:
                all_lines.append(current_line)
        else:
            # 英文文本：按空格拆分单词
            words = para.split()
            current_line = ""
            for word in words:
                test = f"{current_line} {word}".strip()
                w = draw.textlength(test, font=font)
                if w > max_width and current_line:
                    all_lines.append(current_line)
                    current_line = word
                else:
                    current_line = test
            if current_line:
                all_lines.append(current_line)

    return all_lines if all_lines else [""]


def _compute_aligned_layout(items: List[dict], translations: Dict[str, str],
                            zoom: float, page_height_px: float) -> tuple:
    """
    📘 教学笔记：原文对齐模式 — 同字号的块用同一个字号 + 扩展框（不重叠）

    策略：
    1. 把原文字号四舍五入到整数，相同整数字号的块归为一组
    2. 每组内：根据译文长度，找到能让所有块都放下的最大字号
       （从原始字号开始，逐步缩小，直到每个块都能放下）
    3. 向下扩展 bbox，但不能和下一个块重叠
    4. 返回 (size_map: {key: fontsize_pt}, expanded_bboxes_px: {key: bbox})

    📘 和"统一字号"的区别：
    统一字号 = 全页一个字号（标题和正文一样大，不合理）
    原文对齐 = 保持原文的字号层级（标题大、正文小），同级别内统一
    """
    if not items:
        return {}, {}

    # 1. 按四舍五入字号分组
    from collections import defaultdict
    groups: Dict[int, List[dict]] = defaultdict(list)
    for it in items:
        fs = it["dominant_format"].get("font_size", 10)
        rounded = round(fs)
        groups[rounded].append(it)

    # 2. 每组计算统一字号
    size_map = {}  # key -> fontsize_pt
    for rounded_fs, group_items in groups.items():
        # 组内统一用四舍五入后的字号
        target_fs = float(rounded_fs)
        for it in group_items:
            size_map[it["key"]] = target_fs

    # 3. 按 y 坐标排序，计算扩展空间
    sorted_items = sorted(items, key=lambda it: it["bbox"][1])
    expanded = {}

    for i, item in enumerate(sorted_items):
        key = item["key"]
        if key not in translations or key not in size_map:
            continue

        fs = size_map[key]
        px_size = fs * RENDER_DPI / 72.0
        line_h = px_size * 1.3

        bbox_px = [
            item["bbox"][0] * zoom, item["bbox"][1] * zoom,
            item["bbox"][2] * zoom, item["bbox"][3] * zoom,
        ]
        box_w = bbox_px[2] - bbox_px[0]
        text = translations[key]

        # 估算需要的行数
        avg_char_w = px_size * 0.6
        chars_per_line = max(1, int(box_w / avg_char_w))
        needed_lines = max(1, -(-len(text) // chars_per_line))
        needed_h = needed_lines * line_h + 4

        current_h = bbox_px[3] - bbox_px[1]
        if needed_h > current_h:
            max_bottom = page_height_px - 5
            if i + 1 < len(sorted_items):
                next_top_px = sorted_items[i + 1]["bbox"][1] * zoom
                max_bottom = next_top_px - 3
            new_bottom = min(bbox_px[1] + needed_h, max_bottom)
            bbox_px[3] = new_bottom

        expanded[key] = bbox_px

    return size_map, expanded


def _compute_fixed_layout(items: List[dict], translations: Dict[str, str],
                          zoom: float, page_height_px: float,
                          fixed_size: float) -> tuple:
    """
    📘 教学笔记：指定字号模式 — 用户指定一个字号，全部文本框统一使用

    和原文对齐模式共享扩展框逻辑，只是字号来源不同。
    返回 (fixed_size, expanded_bboxes_px)
    """
    if not items:
        return fixed_size, {}

    sorted_items = sorted(items, key=lambda it: it["bbox"][1])
    expanded = {}
    px_size = fixed_size * RENDER_DPI / 72.0
    line_h = px_size * 1.3

    for i, item in enumerate(sorted_items):
        key = item["key"]
        if key not in translations:
            continue

        bbox_px = [
            item["bbox"][0] * zoom, item["bbox"][1] * zoom,
            item["bbox"][2] * zoom, item["bbox"][3] * zoom,
        ]
        box_w = bbox_px[2] - bbox_px[0]
        text = translations[key]

        avg_char_w = px_size * 0.6
        chars_per_line = max(1, int(box_w / avg_char_w))
        needed_lines = max(1, -(-len(text) // chars_per_line))
        needed_h = needed_lines * line_h + 4

        current_h = bbox_px[3] - bbox_px[1]
        if needed_h > current_h:
            max_bottom = page_height_px - 5
            if i + 1 < len(sorted_items):
                next_top_px = sorted_items[i + 1]["bbox"][1] * zoom
                max_bottom = next_top_px - 3
            new_bottom = min(bbox_px[1] + needed_h, max_bottom)
            bbox_px[3] = new_bottom

        expanded[key] = bbox_px

    return fixed_size, expanded


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
    📘 教学笔记：扫描件 PDF 写入主函数

    scan_mode 三种模式：
    - "adaptive"（自适应字号）: 每个块用 OCR 估算的字号，放不下就缩小
    - "aligned"（原文对齐）: 原文同字号的块，译文也用同一个字号，保持层级
    - "fixed"（指定字号）: 用户指定一个字号，全部文本框统一使用

    📘 可编辑 PDF：
    在图片层之上叠加透明文本层（PyMuPDF insert_text），
    这样 PDF 阅读器可以选中、复制、搜索文字。
    """
    if not source_path:
        raise ValueError("source_path is required")
    if layout_overrides is None:
        layout_overrides = {}

    mode_names = {"adaptive": "自适应字号", "aligned": "原文对齐", "fixed": f"指定字号({fixed_fontsize}pt)"}
    mode_name = mode_names.get(scan_mode, scan_mode)
    logger.info(f"开始生成扫描件 PDF: {output_path} (模式: {mode_name})")
    print(f"[📝 扫描件写入] 擦除原文 + 写入译文 ({mode_name})...", flush=True)

    doc = fitz.open(source_path)
    zoom = RENDER_DPI / 72.0

    # 按页分组 items
    page_items: Dict[int, List[dict]] = {}
    for item in parsed_data["items"]:
        key = item["key"]
        if key not in translations:
            continue
        page_idx = int(key.split("_")[0][2:])
        if page_idx not in page_items:
            page_items[page_idx] = []
        page_items[page_idx].append(item)

    out_doc = fitz.open()
    replaced_count = 0

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_width = page.rect.width
        page_height = page.rect.height

        # 1. 渲染页面为图片
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        items = page_items.get(page_idx, [])

        # 📘 非自适应模式：预计算字号和扩展框
        aligned_size_map = {}   # key -> fontsize_pt（原文对齐模式）
        fixed_size_val = None   # float（指定字号模式）
        expanded_bboxes = {}    # key -> bbox_px

        if scan_mode == "aligned" and items:
            page_h_px = page_height * zoom
            aligned_size_map, expanded_bboxes = _compute_aligned_layout(
                items, translations, zoom, page_h_px
            )
            logger.debug(f"第 {page_idx+1} 页: 原文对齐模式，{len(set(aligned_size_map.values()))} 种字号")
        elif scan_mode == "fixed" and items:
            page_h_px = page_height * zoom
            fixed_size_val, expanded_bboxes = _compute_fixed_layout(
                items, translations, zoom, page_h_px, fixed_fontsize
            )
            logger.debug(f"第 {page_idx+1} 页: 指定字号 {fixed_size_val}pt")

        # 📘 收集文本层数据（用于可编辑 PDF）
        text_layer_items = []

        if items:
            for item in items:
                key = item["key"]
                translated = translations[key]

                # 擦除原文
                sub_bboxes_px = item.get("sub_bboxes_px", [])
                if not sub_bboxes_px:
                    bbox_pt = item["bbox"]
                    sub_bboxes_px = [[
                        bbox_pt[0] * zoom, bbox_pt[1] * zoom,
                        bbox_pt[2] * zoom, bbox_pt[3] * zoom,
                    ]]

                for sub_bbox_px in sub_bboxes_px:
                    bg_color, variance = _sample_background_color(img, sub_bbox_px)
                    img = _erase_text_region(img, sub_bbox_px, bg_color, variance)

                # 📘 根据模式决定字号和 bbox
                if scan_mode in ("aligned", "fixed") and key in expanded_bboxes:
                    bbox_px = expanded_bboxes[key]
                    # 扩展区域也需要擦除背景
                    orig_px = sub_bboxes_px[0] if sub_bboxes_px else bbox_px
                    if bbox_px[3] > orig_px[3]:
                        extra = [bbox_px[0], orig_px[3], bbox_px[2], bbox_px[3]]
                        bg_c, var = _sample_background_color(img, extra)
                        img = _erase_text_region(img, extra, bg_c, var)
                    # 📘 原文对齐：从 size_map 取；指定字号：用固定值
                    if scan_mode == "aligned":
                        font_size = aligned_size_map.get(key, 10.0)
                    else:
                        font_size = fixed_size_val
                else:
                    bbox_pt = item["bbox"]
                    bbox_px = [
                        bbox_pt[0] * zoom, bbox_pt[1] * zoom,
                        bbox_pt[2] * zoom, bbox_pt[3] * zoom,
                    ]
                    fmt = item["dominant_format"]
                    override = layout_overrides.get(key, {})
                    font_size = override.get("fontsize", fmt.get("font_size", 12))

                fmt = item["dominant_format"]
                bold = fmt.get("bold", False)
                alignment = item.get("alignment", "left")

                color_hex = fmt.get("font_color", "#000000").lstrip("#")
                if len(color_hex) == 6:
                    b = int(color_hex[4:6], 16)
                    g = int(color_hex[2:4], 16)
                    r = int(color_hex[0:2], 16)
                    font_color = (b, g, r)
                else:
                    font_color = (0, 0, 0)

                img = _draw_text_on_image(
                    img, translated, bbox_px,
                    font_size=font_size,
                    font_color=font_color,
                    bold=bold,
                    alignment=alignment,
                )
                replaced_count += 1

                # 📘 收集文本层数据（pt 坐标）
                bbox_pt_for_text = [v / zoom for v in bbox_px]
                text_layer_items.append({
                    "text": translated,
                    "bbox_pt": bbox_pt_for_text,
                    "font_size": font_size,
                })

        # 3. 图片 → PDF 页面
        rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb_img)

        new_page = out_doc.new_page(width=page_width, height=page_height)

        import io
        img_buffer = io.BytesIO()
        pil_img.save(img_buffer, format="JPEG", quality=92)
        img_buffer.seek(0)
        img_rect = fitz.Rect(0, 0, page_width, page_height)
        new_page.insert_image(img_rect, stream=img_buffer.read())

        # 4. 📘 叠加透明文本层 — 让 PDF 可编辑/可搜索/可复制
        # 在图片上方插入不可见文字（字体颜色透明），
        # PDF 阅读器能选中和复制，但视觉上看不到。
        for tl_item in text_layer_items:
            rect = fitz.Rect(tl_item["bbox_pt"])
            fs = tl_item["font_size"]
            txt = tl_item["text"].replace("\n", " ")
            try:
                # 📘 fontsize 不能太小，否则 insert_textbox 会失败
                actual_fs = max(6, min(fs, 72))
                new_page.insert_textbox(
                    rect, txt,
                    fontsize=actual_fs,
                    fontname="helv",  # PDF 内置字体
                    color=(0, 0, 0),  # 黑色但会被图片遮住
                    overlay=True,     # 在图片上方
                    render_mode=3,    # 📘 render_mode=3 = 不可见文字（invisible）
                )
            except Exception:
                pass  # 个别块写入失败不影响整体

    out_doc.save(output_path, garbage=4, deflate=True)
    out_doc.close()
    doc.close()

    logger.info(f"扫描件 PDF 生成完成: {output_path} (替换 {replaced_count} 个文字块)")
    print(f"[✅ 扫描件写入完成] 替换了 {replaced_count} 个文字块 (可编辑)", flush=True)
