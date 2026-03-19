# tools/scan_tools_ext.py
# =============================================================
# 📘 教学笔记：扫描件 Agent 扩展工具集
# =============================================================
# 这些工具补充 scan_tools.py 中的核心工具，覆盖翻译文件任务中
# 更多的实际场景需求：
#
# 📘 工具清单：
#   - GlossaryTool: 术语表管理（跨页术语一致性）
#   - ColorDetectTool: 文字/背景颜色检测
#   - TextDirectionTool: 文字方向检测（竖排/旋转）
#   - PageComparisonTool: 原文 vs 译文视觉对比
#   - ContextTranslationTool: 带上下文的翻译（跨页语境）
# =============================================================

import json
import io
import os
import numpy as np
import cv2
from typing import Any, Dict
from tools.base_tool import BaseTool
from core.logger import get_logger

logger = get_logger("scan_tools_ext")


class GlossaryTool(BaseTool):
    """
    📘 教学笔记：术语表管理工具

    翻译文件时最常见的质量问题之一：同一个专有名词在不同页面翻译不一致。
    比如公司名"东方建科"在第1页译为"Oriental Jianke"，第3页变成"Dongfang Construction"。

    📘 解决方案：
    Brain 在处理每页时，把识别到的专有名词（人名、公司名、地名、缩写等）
    注册到术语表。后续页面翻译时，先查术语表确保一致。

    📘 术语表存在 context["glossary"] 中，跨页共享。
    也可以持久化到 translator_config/glossary.json 供未来复用。
    """

    name = "manage_glossary"
    description = (
        "管理翻译术语表，确保专有名词跨页翻译一致。"
        "支持操作：add（添加术语）、lookup（查询术语）、list（列出所有术语）、save（持久化保存）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "lookup", "list", "save"],
                "description": "操作类型",
            },
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "原文术语"},
                        "target": {"type": "string", "description": "译文术语"},
                        "category": {"type": "string", "description": "类别：person/company/place/abbreviation/other"},
                    },
                },
                "description": "术语条目列表（add 操作时必填）",
            },
            "query": {
                "type": "string",
                "description": "查询关键词（lookup 操作时使用）",
            },
        },
        "required": ["action"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        action = params["action"]
        glossary = self.context.setdefault("glossary", {})

        if action == "add":
            entries = params.get("entries", [])
            added = 0
            for entry in entries:
                src = entry.get("source", "").strip()
                tgt = entry.get("target", "").strip()
                cat = entry.get("category", "other")
                if src and tgt:
                    glossary[src] = {"target": tgt, "category": cat}
                    added += 1
            return json.dumps({
                "added": added, "total": len(glossary),
            }, ensure_ascii=False)

        elif action == "lookup":
            query = params.get("query", "").strip()
            if not query:
                return json.dumps({"error": "lookup 需要 query 参数"}, ensure_ascii=False)
            # 📘 精确匹配 + 模糊匹配
            exact = glossary.get(query)
            if exact:
                return json.dumps({
                    "found": True, "source": query,
                    "target": exact["target"], "category": exact["category"],
                }, ensure_ascii=False)
            # 📘 模糊：查找包含 query 的术语
            partial = []
            for src, info in glossary.items():
                if query in src or query in info["target"]:
                    partial.append({"source": src, "target": info["target"], "category": info["category"]})
            return json.dumps({
                "found": bool(partial), "matches": partial[:10],
            }, ensure_ascii=False)

        elif action == "list":
            items = [
                {"source": src, "target": info["target"], "category": info["category"]}
                for src, info in glossary.items()
            ]
            return json.dumps({"total": len(items), "entries": items}, ensure_ascii=False)

        elif action == "save":
            path = "translator_config/glossary.json"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 📘 合并已有术语表
            existing = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    pass
            existing.update(glossary)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            return json.dumps({
                "saved": True, "total": len(existing), "path": path,
            }, ensure_ascii=False)

        return json.dumps({"error": f"未知操作: {action}"}, ensure_ascii=False)


class ColorDetectTool(BaseTool):
    """
    📘 教学笔记：颜色检测工具

    很多正式文档有彩色元素：红色公章、蓝色签名、彩色表头背景、
    灰色底纹等。如果翻译后全变成黑白，排版就不"一致"了。

    📘 工作原理：
    在指定区域内采样像素，统计主要颜色。
    Brain 可以用这些颜色信息来：
    1. 在 JSON 中标注文字颜色（font_color）
    2. 判断是否需要保留彩色背景
    3. 区分不同颜色的文字（如红色=重要、蓝色=链接）
    """

    name = "detect_colors"
    description = (
        "检测页面指定区域的主要颜色。用于识别彩色文字、背景色、"
        "红色公章、蓝色签名等需要在译文中保留的颜色信息。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码索引（从0开始）",
            },
            "bbox_pct": {
                "type": "array",
                "items": {"type": "number"},
                "description": "检测区域 [left%, top%, right%, bottom%]，每个值 0-100",
            },
        },
        "required": ["page_index", "bbox_pct"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        page_index = params["page_index"]
        bbox_pct = params["bbox_pct"]
        page_images = self.context.get("page_images", [])

        current_idx = self.context.get("current_page_index")
        if current_idx is not None and page_index != current_idx:
            page_index = current_idx

        if page_index < 0 or page_index >= len(page_images):
            return json.dumps({"error": f"page_index 超出范围"}, ensure_ascii=False)

        if not bbox_pct or len(bbox_pct) != 4:
            return json.dumps({"error": "bbox_pct 格式错误"}, ensure_ascii=False)

        try:
            img_bytes = page_images[page_index]
            nparr = np.frombuffer(img_bytes, np.uint8)
            cv_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            h, w = cv_img.shape[:2]

            left = max(0, int(w * bbox_pct[0] / 100))
            top = max(0, int(h * bbox_pct[1] / 100))
            right = min(w, int(w * bbox_pct[2] / 100))
            bottom = min(h, int(h * bbox_pct[3] / 100))

            if right - left < 5 or bottom - top < 5:
                return json.dumps({"error": "区域太小"}, ensure_ascii=False)

            roi = cv_img[top:bottom, left:right]

            # 📘 转 RGB（OpenCV 默认 BGR）
            roi_rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
            pixels = roi_rgb.reshape(-1, 3)

            # 📘 统计背景色（最常见的颜色）
            from collections import Counter
            # 量化到 32 级减少颜色数
            quantized = (pixels // 32 * 32 + 16).astype(np.uint8)
            color_tuples = [tuple(c) for c in quantized]
            counter = Counter(color_tuples)
            top_colors = counter.most_common(5)

            # 📘 判断背景色和前景色
            total_pixels = len(pixels)
            bg_color = top_colors[0][0] if top_colors else (255, 255, 255)
            bg_ratio = top_colors[0][1] / total_pixels if top_colors else 1.0

            # 📘 非背景色 = 前景色（文字/图案的颜色）
            fg_colors = []
            for color, count in top_colors[1:]:
                ratio = count / total_pixels
                if ratio > 0.02:  # 至少占 2%
                    fg_colors.append({
                        "rgb": list(color),
                        "hex": "#{:02x}{:02x}{:02x}".format(*color),
                        "ratio": round(ratio, 3),
                    })

            # 📘 判断是否有显著的非黑非白颜色
            has_color = False
            for fc in fg_colors:
                r, g, b = fc["rgb"]
                # 不是灰色（R≈G≈B）且不是接近黑/白
                is_gray = abs(r - g) < 40 and abs(g - b) < 40
                is_dark = r < 60 and g < 60 and b < 60
                is_light = r > 200 and g > 200 and b > 200
                if not is_gray and not is_dark and not is_light:
                    has_color = True
                    break

            result = {
                "background": {
                    "rgb": list(bg_color),
                    "hex": "#{:02x}{:02x}{:02x}".format(*bg_color),
                    "ratio": round(bg_ratio, 3),
                    "is_white": all(c > 230 for c in bg_color),
                },
                "foreground_colors": fg_colors,
                "has_significant_color": has_color,
            }

            logger.info(
                f"颜色检测: 第 {page_index} 页 bbox={bbox_pct}, "
                f"背景={'白' if result['background']['is_white'] else result['background']['hex']}, "
                f"{'有彩色' if has_color else '黑白'}"
            )
            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.error(f"颜色检测失败: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class TextDirectionTool(BaseTool):
    """
    📘 教学笔记：文字方向检测工具

    有些文档包含竖排文字（东亚传统排版）或旋转文字（如表格侧边标签）。
    OCR 可能识别出文字但不知道方向，Brain 看图能判断但不够精确。

    📘 工作原理：
    分析指定区域内 OCR 文字块的 bbox 分布模式：
    - 竖排：多个文字块 x 坐标接近，y 坐标递增
    - 横排：多个文字块 y 坐标接近，x 坐标递增
    - 旋转：bbox 宽高比异常（高 > 宽 的单字符块）
    """

    name = "detect_text_direction"
    description = (
        "检测指定区域内文字的排列方向（横排/竖排/旋转）。"
        "基于 OCR 结果中文字块的坐标分布模式判断。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码索引（从0开始）",
            },
            "bbox_pct": {
                "type": "array",
                "items": {"type": "number"},
                "description": "检测区域 [left%, top%, right%, bottom%]",
            },
        },
        "required": ["page_index", "bbox_pct"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        page_index = params["page_index"]
        bbox_pct = params["bbox_pct"]
        page_images = self.context.get("page_images", [])

        current_idx = self.context.get("current_page_index")
        if current_idx is not None and page_index != current_idx:
            page_index = current_idx

        if page_index < 0 or page_index >= len(page_images):
            return json.dumps({"error": "page_index 超出范围"}, ensure_ascii=False)

        try:
            import tempfile
            img_bytes = page_images[page_index]

            # 📘 获取页面尺寸
            nparr = np.frombuffer(img_bytes, np.uint8)
            cv_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            img_h, img_w = cv_img.shape[:2]

            # 📘 运行 OCR 获取文字块坐标
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(img_bytes)
            tmp.close()
            from translator.scan_parser import _ocr_page_subprocess
            ocr_results = _ocr_page_subprocess(tmp.name)
            os.unlink(tmp.name)

            # 📘 筛选区域内的文字块
            region_left = img_w * bbox_pct[0] / 100
            region_top = img_h * bbox_pct[1] / 100
            region_right = img_w * bbox_pct[2] / 100
            region_bottom = img_h * bbox_pct[3] / 100

            blocks_in_region = []
            for item in ocr_results:
                bbox = item.get("bbox", [0, 0, 0, 0])
                cx = (bbox[0] + bbox[2]) / 2
                cy = (bbox[1] + bbox[3]) / 2
                if region_left <= cx <= region_right and region_top <= cy <= region_bottom:
                    blocks_in_region.append({
                        "text": item["text"],
                        "bbox": bbox,
                        "width": bbox[2] - bbox[0],
                        "height": bbox[3] - bbox[1],
                        "cx": cx, "cy": cy,
                    })

            if len(blocks_in_region) < 2:
                direction = "unknown"
                confidence = 0.0
            else:
                # 📘 分析坐标分布
                xs = [b["cx"] for b in blocks_in_region]
                ys = [b["cy"] for b in blocks_in_region]
                x_spread = max(xs) - min(xs) if xs else 0
                y_spread = max(ys) - min(ys) if ys else 0

                # 📘 宽高比分析
                avg_aspect = sum(b["width"] / max(b["height"], 1) for b in blocks_in_region) / len(blocks_in_region)

                if x_spread < y_spread * 0.3 and y_spread > 20:
                    # x 变化小，y 变化大 → 竖排
                    direction = "vertical"
                    confidence = min(1.0, y_spread / max(x_spread + 1, 1) * 0.3)
                elif y_spread < x_spread * 0.3 and x_spread > 20:
                    # y 变化小，x 变化大 → 横排
                    direction = "horizontal"
                    confidence = min(1.0, x_spread / max(y_spread + 1, 1) * 0.3)
                elif avg_aspect < 0.5:
                    # 单字符块高>宽 → 可能旋转
                    direction = "rotated"
                    confidence = 0.6
                else:
                    direction = "horizontal"
                    confidence = 0.5

            result = {
                "direction": direction,
                "confidence": round(confidence, 2),
                "blocks_found": len(blocks_in_region),
                "blocks": [{"text": b["text"], "bbox": b["bbox"]} for b in blocks_in_region[:10]],
            }

            logger.info(
                f"文字方向检测: 第 {page_index} 页 bbox={bbox_pct}, "
                f"方向={direction}, 置信度={confidence:.2f}, {len(blocks_in_region)} 个文字块"
            )
            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.error(f"文字方向检测失败: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class ContextTranslationTool(BaseTool):
    """
    📘 教学笔记：上下文感知翻译工具

    普通的 translate_texts 是无状态的——每次调用都是独立的翻译请求。
    但真实文档有跨页上下文：
    - 合同第1页定义了缩写 "甲方"="ABC公司"，后面页面要一致
    - 医疗报告前面提到了病人信息，后面的诊断要用同样的术语
    - 学术论文的专有名词需要全文一致

    📘 解决方案：
    在翻译请求中附加上下文信息（术语表 + 前文摘要），
    让翻译模型在翻译时参考这些上下文，确保一致性和准确性。
    """

    name = "translate_with_context"
    description = (
        "带上下文的翻译。在翻译时附加术语表和前文摘要，"
        "确保专有名词一致、语境连贯。适用于跨页翻译场景。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "texts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "待翻译的文本列表",
            },
            "target_lang": {
                "type": "string",
                "description": "目标语言",
            },
            "context_summary": {
                "type": "string",
                "description": "前文摘要（如'这是一份医疗出院报告，患者张三，诊断为...'）",
            },
            "glossary_terms": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                    },
                },
                "description": "必须遵守的术语对照表",
            },
        },
        "required": ["texts", "target_lang"],
    }

    def __init__(self, translate_pipeline=None, context: Dict[str, Any] = None):
        self.translate_pipeline = translate_pipeline
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        texts = params["texts"]
        target_lang = params["target_lang"]
        context_summary = params.get("context_summary", "")
        glossary_terms = params.get("glossary_terms", [])

        if not texts:
            return json.dumps({"translations": {}}, ensure_ascii=False)

        if not self.translate_pipeline:
            return json.dumps({"error": "翻译流水线未初始化"}, ensure_ascii=False)

        # 📘 也从 context["glossary"] 中读取术语（GlossaryTool 维护的）
        ctx_glossary = self.context.get("glossary", {})
        all_terms = list(glossary_terms)
        for src, info in ctx_glossary.items():
            all_terms.append({"source": src, "target": info["target"]})

        try:
            # 📘 构建带上下文的翻译输入
            # 在每个文本前加上上下文提示
            enriched_texts = []
            for text in texts:
                prefix_parts = []
                if context_summary:
                    prefix_parts.append(f"[上下文: {context_summary}]")
                if all_terms:
                    terms_str = "; ".join(f"{t['source']}={t['target']}" for t in all_terms[:20])
                    prefix_parts.append(f"[术语: {terms_str}]")
                if prefix_parts:
                    enriched = " ".join(prefix_parts) + " " + text
                else:
                    enriched = text
                enriched_texts.append(enriched)

            # 📘 调用翻译流水线
            translated_list = self.translate_pipeline.translate_batch(
                enriched_texts, target_lang=target_lang
            )

            translations = {}
            for orig, trans in zip(texts, translated_list):
                translations[orig] = trans

            logger.info(
                f"上下文翻译完成: {len(translations)} 个文本, "
                f"{len(all_terms)} 个术语, 上下文={'有' if context_summary else '无'}"
            )
            return json.dumps({"translations": translations}, ensure_ascii=False)

        except Exception as e:
            logger.error(f"上下文翻译失败: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class PageComparisonTool(BaseTool):
    """
    📘 教学笔记：页面对比验证工具

    翻译完成后，最终的质量检查：原文页面 vs 译文页面的视觉对比。
    不是像素级对比（那没意义），而是结构级对比：
    - 元素数量是否一致（表格、段落、图片）
    - 文字区域的位置分布是否相似
    - 空白区域的比例是否接近

    📘 工作原理：
    1. 对原始页面图片做边缘检测，提取结构轮廓
    2. 对译文结构做模拟布局，计算元素位置
    3. 对比两者的结构相似度
    """

    name = "compare_page_layout"
    description = (
        "对比原文页面和译文结构的布局相似度。"
        "检查元素数量、位置分布、空白比例是否一致。"
        "用于翻译完成后的最终质量验证。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码索引（从0开始）",
            },
            "page_structure": {
                "type": "object",
                "description": "Brain 输出的页面结构 JSON",
            },
        },
        "required": ["page_index", "page_structure"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        page_index = params["page_index"]
        page_structure = params["page_structure"]
        page_images = self.context.get("page_images", [])

        current_idx = self.context.get("current_page_index")
        if current_idx is not None and page_index != current_idx:
            page_index = current_idx

        if page_index < 0 or page_index >= len(page_images):
            return json.dumps({"error": "page_index 超出范围"}, ensure_ascii=False)

        try:
            # 📘 分析原始页面的结构特征
            img_bytes = page_images[page_index]
            nparr = np.frombuffer(img_bytes, np.uint8)
            cv_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
            img_h, img_w = cv_img.shape[:2]

            # 📘 计算原始页面的文字密度分布（按行）
            # 二值化后统计每行的黑色像素比例
            _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
            row_density = np.mean(binary > 0, axis=1)  # 每行的"墨水"比例

            # 📘 把页面分成 10 个水平条带，统计每个条带的密度
            band_count = 10
            band_h = img_h // band_count
            original_bands = []
            for i in range(band_count):
                start_y = i * band_h
                end_y = min((i + 1) * band_h, img_h)
                band_density = float(np.mean(row_density[start_y:end_y]))
                original_bands.append(round(band_density, 3))

            # 📘 分析译文结构的特征
            elements = page_structure.get("elements", [])
            elem_count = len(elements)
            table_count = sum(1 for e in elements if e.get("type") == "table")
            para_count = sum(1 for e in elements if e.get("type") == "paragraph")
            image_count = sum(1 for e in elements if e.get("type") == "image_region")

            # 📘 计算总文字量
            total_cells = 0
            total_text_len = 0
            for elem in elements:
                if elem.get("type") == "table":
                    for row in elem.get("rows", []):
                        cells = row.get("cells", row) if isinstance(row, dict) else row
                        if isinstance(cells, dict):
                            cells = cells.get("cells", [])
                        for cell in cells:
                            total_cells += 1
                            total_text_len += len(cell.get("text", ""))
                elif elem.get("type") == "paragraph":
                    total_text_len += len(elem.get("text", ""))

            # 📘 简单的结构评分
            # 有内容的页面应该有元素
            has_content = any(d > 0.01 for d in original_bands)
            has_elements = elem_count > 0

            if has_content and not has_elements:
                score = 0.2
                issues = ["原始页面有内容但译文结构为空"]
            elif not has_content and has_elements:
                score = 0.8
                issues = ["原始页面可能是空白页但有译文结构"]
            elif not has_content and not has_elements:
                score = 1.0
                issues = []
            else:
                # 📘 基于密度分布的相似度（简化版）
                # 有内容的条带数量对比
                original_active = sum(1 for d in original_bands if d > 0.01)
                # 估算译文的活跃条带（基于元素数量）
                estimated_active = min(band_count, max(1, elem_count))
                band_similarity = 1.0 - abs(original_active - estimated_active) / band_count

                score = round(max(0.0, min(1.0, band_similarity)), 2)
                issues = []
                if score < 0.6:
                    issues.append(f"布局密度差异较大: 原文 {original_active}/{band_count} 条带有内容, 译文约 {estimated_active} 个元素")

            result = {
                "score": score,
                "original_analysis": {
                    "density_bands": original_bands,
                    "active_bands": sum(1 for d in original_bands if d > 0.01),
                },
                "structure_analysis": {
                    "total_elements": elem_count,
                    "tables": table_count,
                    "paragraphs": para_count,
                    "images": image_count,
                    "total_cells": total_cells,
                    "total_text_length": total_text_len,
                },
                "issues": issues,
            }

            logger.info(
                f"页面对比: 第 {page_index} 页, 相似度={score}, "
                f"元素={elem_count}(表{table_count}+段{para_count}+图{image_count})"
            )
            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.error(f"页面对比失败: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)
