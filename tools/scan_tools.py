# tools/scan_tools.py
# =============================================================
# 📘 教学笔记：扫描件 Agent 工具集（Scan Agent Tools）
# =============================================================
# 这些工具是 ScanAgent 的"手脚"——Agent 大脑（Gemini/Claude/GPT）
# 通过 tool_call 调用这些工具来完成具体操作。
#
# 📘 设计原则：
#   1. 每个工具继承 BaseTool，实现 execute(params) -> str
#   2. 页面图片通过 context（上下文）传递，不通过参数
#      （base64 图片太大，放在 JSON 参数里浪费 token）
#   3. 工具返回 JSON 字符串，LLM 能直接理解
#   4. 所有异常都捕获并返回结构化错误，不让异常冒泡
#
# 📘 工具清单：
#   - OCRTool: 子进程 RapidOCR 文字识别
#   - CVTool: OpenCV 表格线 + 图片区域检测
#   - TranslationTool: doubao 翻译（复用 TranslatePipeline）
#   - WordWriterTool: 生成 Word 文档（复用 scan_writer）
# =============================================================

import json
import os
import io
import tempfile
import numpy as np
import cv2
from typing import Any, Dict, Optional
from tools.base_tool import BaseTool
from core.logger import get_logger

logger = get_logger("scan_tools")


class OCRTool(BaseTool):
    """
    📘 教学笔记：OCR 文字识别工具

    在子进程中运行 RapidOCR，避免 PyQt6 + onnxruntime DLL 冲突。
    通过 context["page_images"] 访问页面图片，不通过参数传 base64。

    📘 为什么用子进程？
    Python 3.14 上 PyQt6 和 onnxruntime 的 DLL 会冲突。
    subprocess 隔离 DLL 加载，两边互不干扰。
    """

    name = "ocr_extract_text"
    description = "对页面图片执行 OCR 文字识别，返回所有文字内容及其位置坐标。在子进程中运行以避免 DLL 冲突。"
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码索引（从0开始）",
            }
        },
        "required": ["page_index"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        """
        📘 context 包含 ScanAgent 共享的数据：
        - page_images: List[bytes]  每页的 JPEG bytes
        """
        self.context = context or {}

    def execute(self, params: dict) -> str:
        # 📘 参数校验
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        page_index = params["page_index"]
        page_images = self.context.get("page_images", [])

        # 📘 教学笔记：page_index 自动修正
        # Brain 经常传错 page_index（比如总是传 0），因为它不知道当前页码。
        # context["current_page_index"] 由 ScanAgent 在每页处理前设置，
        # 如果 Brain 传的值超出范围或与当前页不一致，自动修正。
        current_idx = self.context.get("current_page_index")
        if current_idx is not None and page_index != current_idx:
            logger.debug(
                f"OCR page_index 修正: Brain 传 {page_index} → 实际 {current_idx}"
            )
            page_index = current_idx

        if page_index < 0 or page_index >= len(page_images):
            return json.dumps(
                {"error": f"page_index {page_index} 超出范围 [0, {len(page_images) - 1}]"},
                ensure_ascii=False,
            )

        try:
            # 📘 把页面图片写入临时文件，供子进程 OCR 读取
            img_bytes = page_images[page_index]
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(img_bytes)
            tmp.close()

            # 📘 复用 scan_parser 的子进程 OCR 逻辑
            from translator.scan_parser import _ocr_page_subprocess

            results = _ocr_page_subprocess(tmp.name)
            os.unlink(tmp.name)

            logger.info(f"OCR 第 {page_index} 页: 识别到 {len(results)} 个文字块")
            return json.dumps(results, ensure_ascii=False)

        except Exception as e:
            logger.error(f"OCR 工具执行失败: {e}")
            return json.dumps(
                {"error": f"OCR 执行失败: {type(e).__name__}: {str(e)}"},
                ensure_ascii=False,
            )


class CVTool(BaseTool):
    """
    📘 教学笔记：CV 布局检测工具

    使用 OpenCV 检测页面中的表格线（水平线和垂直线）和图片区域。
    复用 scan_parser.py 中的 _detect_table_lines 和 _detect_image_regions。

    📘 为什么需要 CV？
    OCR 只能识别文字，看不到表格线和图片。
    CV 能精确检测到画出来的线（像素级），
    Agent 大脑结合 CV 数据 + 视觉理解，就能判断完整的表格结构。
    """

    name = "cv_detect_layout"
    description = "使用 OpenCV 检测页面中的表格线（水平线和垂直线）和图片区域，返回结构化的位置信息。"
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码索引（从0开始）",
            }
        },
        "required": ["page_index"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        page_index = params["page_index"]
        page_images = self.context.get("page_images", [])

        # 📘 page_index 自动修正（同 OCRTool）
        current_idx = self.context.get("current_page_index")
        if current_idx is not None and page_index != current_idx:
            logger.debug(
                f"CV page_index 修正: Brain 传 {page_index} → 实际 {current_idx}"
            )
            page_index = current_idx

        if page_index < 0 or page_index >= len(page_images):
            return json.dumps(
                {"error": f"page_index {page_index} 超出范围 [0, {len(page_images) - 1}]"},
                ensure_ascii=False,
            )

        try:
            # 📘 把 JPEG bytes 转成 OpenCV numpy array
            img_bytes = page_images[page_index]
            nparr = np.frombuffer(img_bytes, np.uint8)
            cv_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
            img_h, img_w = cv_img.shape[:2]

            # 📘 复用 scan_parser 的检测函数
            from translator.scan_parser import _detect_table_lines, _detect_image_regions

            h_positions, v_positions, table_mask = _detect_table_lines(gray)
            image_regions = _detect_image_regions(cv_img, gray, [])

            has_table = len(h_positions) >= 2 and len(v_positions) >= 2

            # 📘 转换为百分比坐标（相对于页面尺寸）
            h_pcts = [round(y / img_h * 100, 1) for y in h_positions]
            v_pcts = [round(x / img_w * 100, 1) for x in v_positions]

            result = {
                "has_table": has_table,
                "h_lines": h_pcts,
                "v_lines": v_pcts,
                "image_regions": [
                    {
                        "bbox_pct": r["bbox_pct"],
                        "description": r.get("description", "图片区域"),
                    }
                    for r in image_regions
                ],
                "page_size": {"width": img_w, "height": img_h},
            }

            logger.info(
                f"CV 第 {page_index} 页: "
                f"{'有表格' if has_table else '无表格'}, "
                f"{len(h_positions)} 水平线, {len(v_positions)} 垂直线, "
                f"{len(image_regions)} 图片区域"
            )
            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.error(f"CV 工具执行失败: {e}")
            return json.dumps(
                {"error": f"CV 执行失败: {type(e).__name__}: {str(e)}"},
                ensure_ascii=False,
            )


class TranslationTool(BaseTool):
    """
    📘 教学笔记：翻译工具

    内部调用 TranslatePipeline.translate_batch()，复用 doubao 模型。
    Agent 大脑（Gemini/Claude）负责理解和决策，翻译还是用 doubao（便宜且质量好）。

    📘 为什么翻译不用 Agent 大脑？
    翻译是"体力活"——需要处理大量文本，按 token 计费。
    doubao 翻译质量好且便宜，没必要用贵的外部模型来翻译。
    Agent 大脑只负责"看"和"想"，翻译交给 doubao。
    """

    name = "translate_texts"
    description = "将一批文本翻译为目标语言。使用 doubao 模型进行高质量翻译。"
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
                "description": "目标语言，如'英文'、'日文'",
            },
        },
        "required": ["texts", "target_lang"],
    }

    def __init__(self, translate_pipeline=None):
        """
        📘 translate_pipeline: TranslatePipeline 实例（由 ScanAgent 注入）
        """
        self.translate_pipeline = translate_pipeline

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        texts = params["texts"]
        target_lang = params["target_lang"]

        if not texts:
            return json.dumps({"translations": {}}, ensure_ascii=False)

        if not self.translate_pipeline:
            return json.dumps(
                {"error": "翻译流水线未初始化"},
                ensure_ascii=False,
            )

        try:
            # 📘 调用 TranslatePipeline.translate_batch（初翻 + 审校）
            translated_list = self.translate_pipeline.translate_batch(
                texts, target_lang=target_lang
            )

            # 📘 构建 {原文: 译文} 映射
            translations = {}
            for orig, trans in zip(texts, translated_list):
                translations[orig] = trans

            logger.info(f"翻译完成: {len(translations)} 个文本 → {target_lang}")
            return json.dumps({"translations": translations}, ensure_ascii=False)

        except Exception as e:
            logger.error(f"翻译工具执行失败: {e}")
            return json.dumps(
                {"error": f"翻译执行失败: {type(e).__name__}: {str(e)}"},
                ensure_ascii=False,
            )


class WordWriterTool(BaseTool):
    """
    📘 教学笔记：Word 文档生成工具

    内部调用 scan_writer.write_scan_pdf()，复用已有的 Word 生成能力。
    Agent 只需要产出兼容的结构化数据（page_structures + translations），
    Word 生成完全由 python-docx 完成——不花钱。

    📘 为什么不让 Agent 大脑直接生成 Word？
    1. python-docx 生成 Word 是确定性操作，不需要 LLM
    2. scan_writer.py 已经有完善的布局还原能力（per-cell 边框、图片嵌入等）
    3. 省钱——Word 生成不消耗任何 API token
    """

    name = "generate_word_document"
    description = "根据结构化数据和翻译结果生成 Word 文档。"
    parameters = {
        "type": "object",
        "properties": {
            "page_structures": {
                "type": "array",
                "description": "每页的结构化数据",
            },
            "translations": {
                "type": "object",
                "description": "翻译映射 {key: 译文}",
            },
            "output_path": {
                "type": "string",
                "description": "输出文件路径",
            },
        },
        "required": ["page_structures", "translations", "output_path"],
    }

    def __init__(self, format_engine=None, page_images: list = None):
        """
        📘 format_engine: FormatEngine 实例
        📘 page_images: 每页的 JPEG bytes（嵌入原始图片参考）
        """
        self.format_engine = format_engine
        self.page_images = page_images or []

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        page_structures = params["page_structures"]
        translations = params["translations"]
        output_path = params["output_path"]

        try:
            from translator.scan_writer import write_scan_pdf

            # 📘 构建与 parse_scan_pdf 兼容的 parsed_data
            parsed_data = {
                "page_structures": page_structures,
                "page_images": self.page_images,
            }

            result_path = write_scan_pdf(
                parsed_data=parsed_data,
                translations=translations,
                output_path=output_path,
                format_engine=self.format_engine,
            )

            logger.info(f"Word 文档生成完成: {result_path}")
            return json.dumps(
                {"output_path": result_path, "success": True},
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"Word 生成工具执行失败: {e}")
            return json.dumps(
                {"error": f"Word 生成失败: {type(e).__name__}: {str(e)}"},
                ensure_ascii=False,
            )


class ImageGenTool(BaseTool):
    """
    📘 教学笔记：图片生成工具（Image Generation Tool）

    Agent Brain 调用此工具，让图片生成 LLM 在原始页面图片上直接重绘译文。
    这是「保留背景」模式的核心工具——图片生成 LLM 能理解背景纹理、
    匹配字体风格、处理渐变和阴影，远比 Pillow 画矩形+写字效果好。

    📘 工作流程：
    1. Brain 分析页面，提取所有文字区域和译文
    2. Brain 构造详细的 image_prompt，描述每个文字区域的位置和译文
    3. 本工具把原始页面图片 + prompt 发给图片生成 LLM
    4. 图片生成 LLM 返回重绘后的图片（背景完全保留，文字替换为译文）
    5. 图片存入 context["overlay_images"]，供最终 PDF 合成

    📘 也可用于非保留背景模式：Brain 认为某些复杂排版用图片生成更好时自主调用。
    """

    name = "generate_translated_image"
    description = (
        "用图片生成模型将页面中的文字替换为目标语言译文，保留原始背景和视觉效果。"
        "图片生成 LLM 会在原图上直接重绘——保留所有背景纹理、花纹、水印、颜色，"
        "只把文字内容替换为译文。生成的图片自动保存，供最终 PDF 合成。\n"
        "适用场景：有花纹背景、水印、彩色底图的文档，或排版极复杂的页面。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码索引（从0开始）",
            },
            "translated_text": {
                "type": "string",
                "description": "准确的译文内容（所有文字区域的译文，按位置标注）",
            },
            "image_prompt": {
                "type": "string",
                "description": (
                    "给图片生成模型的详细指令，必须包含：\n"
                    "1. 每个文字区域在图片中的大致位置描述\n"
                    "2. 对应的译文内容\n"
                    "3. 字体大小、粗细、颜色等视觉要求\n"
                    "4. 明确指出哪些区域不要修改（签名、盖章、logo 等）\n"
                    "5. 强调：保留所有背景元素，只替换文字"
                ),
            },
            "target_lang": {
                "type": "string",
                "description": "目标语言，如'中文'、'英文'、'日文'",
            },
        },
        "required": ["page_index", "translated_text", "image_prompt", "target_lang"],
    }

    def __init__(self, image_gen_engine=None, context: Dict[str, Any] = None):
        self.image_gen_engine = image_gen_engine
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        if not self.image_gen_engine:
            return json.dumps(
                {"error": "图片生成模型未配置"},
                ensure_ascii=False,
            )

        page_index = params["page_index"]
        translated_text = params["translated_text"]
        image_prompt = params["image_prompt"]
        target_lang = params["target_lang"]

        page_images = self.context.get("page_images", [])
        # 📘 page_index 自动修正
        current_idx = self.context.get("current_page_index")
        if current_idx is not None and page_index != current_idx:
            page_index = current_idx

        if page_index < 0 or page_index >= len(page_images):
            return json.dumps(
                {"error": f"page_index {page_index} 超出范围 [0, {len(page_images) - 1}]"},
                ensure_ascii=False,
            )

        try:
            import base64

            # 📘 构建图片生成请求
            img_b64 = base64.b64encode(page_images[page_index]).decode("utf-8")

            full_prompt = (
                f"You are a professional document translator. Your task is to modify this document image "
                f"by replacing ALL original text with the {target_lang} translation below.\n\n"
                f"## CRITICAL: Complete text replacement\n"
                f"- REMOVE every single character of the original language text from the image\n"
                f"- Replace each text region with ONLY the translated text provided below\n"
                f"- There must be ZERO original language characters remaining in the output image\n"
                f"- If the original has both Chinese and English (e.g. a bilingual heading), "
                f"replace the entire region with ONLY the translated version\n\n"
                f"## Translated text to place on the image\n{translated_text}\n\n"
                f"## Layout and style instructions\n{image_prompt}\n\n"
                f"## Visual preservation rules\n"
                f"- The background must be IDENTICAL to the original - no white patches, no color changes\n"
                f"- Text position, size ratio, weight, and color must match the original layout\n"
                f"- Do NOT modify any non-text elements (signatures, stamps, logos, photos, barcodes)\n"
                f"- If space is tight, slightly reduce font size but keep readability\n"
                f"- Output a complete document image with the same dimensions as the input"
            )

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img_b64}",
                            },
                        },
                        {"type": "text", "text": full_prompt},
                    ],
                },
            ]

            # 📘 使用非流式调用（图片数据不适合流式传输）
            if hasattr(self.image_gen_engine, 'generate_image'):
                gen_result = self.image_gen_engine.generate_image(messages)
            else:
                # 📘 fallback: 用流式接口（可能丢失图片数据）
                response_text = ""
                for chunk in self.image_gen_engine.stream_chat(messages):
                    if chunk["type"] == "text":
                        response_text += chunk["content"]
                gen_result = {"text": response_text, "images": [], "usage": None}

            # 📘 统计 token
            if gen_result.get("usage"):
                usage = gen_result["usage"]
                # 📘 token 统计由 scan_agent 在外层处理
                self.context.setdefault("_last_image_gen_usage", {})
                self.context["_last_image_gen_usage"] = usage

            # 📘 保存生成的图片到 context
            images = gen_result.get("images", [])
            if images:
                if "overlay_images" not in self.context:
                    self.context["overlay_images"] = {}

                # 📘 教学笔记：强制 resize 到原图尺寸
                # 图片生成 LLM（如 Gemini）返回的图片尺寸可能与原图不同，
                # 导致最终 PDF 页面忽大忽小。必须 resize 到原图的精确尺寸。
                from PIL import Image as PILImage
                original_img = PILImage.open(io.BytesIO(page_images[page_index]))
                orig_w, orig_h = original_img.size

                gen_img = PILImage.open(io.BytesIO(images[0]))
                gen_w, gen_h = gen_img.size

                if (gen_w, gen_h) != (orig_w, orig_h):
                    logger.info(
                        f"图片生成尺寸不匹配: 生成 {gen_w}x{gen_h} vs 原图 {orig_w}x{orig_h}，"
                        f"resize 到原图尺寸"
                    )
                    gen_img = gen_img.resize((orig_w, orig_h), PILImage.LANCZOS)
                    buf = io.BytesIO()
                    gen_img.save(buf, format="JPEG", quality=92)
                    final_bytes = buf.getvalue()
                else:
                    final_bytes = images[0]

                self.context["overlay_images"][page_index] = final_bytes

                logger.info(
                    f"图片生成成功: 第 {page_index} 页, "
                    f"图片大小 {len(final_bytes) / 1024:.1f}KB"
                )

                return json.dumps({
                    "success": True,
                    "page_index": page_index,
                    "image_generated": True,
                    "image_size_kb": round(len(final_bytes) / 1024, 1),
                    "image_count": len(images),
                    "resized": (gen_w, gen_h) != (orig_w, orig_h),
                    "text_response": gen_result.get("text", "")[:200],
                }, ensure_ascii=False)
            else:
                # 📘 图片生成 LLM 没有返回图片（可能只返回了文本）
                logger.warning(
                    f"图片生成未返回图片: 第 {page_index} 页, "
                    f"文本响应: {gen_result.get('text', '')[:200]}"
                )
                return json.dumps({
                    "success": False,
                    "page_index": page_index,
                    "image_generated": False,
                    "text_response": gen_result.get("text", "")[:500],
                    "hint": "图片生成模型未返回图片。可尝试调整 prompt 或使用 overlay_translated_text 工具作为备选。",
                }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"图片生成工具执行失败: {e}")
            return json.dumps(
                {"error": f"图片生成失败: {type(e).__name__}: {str(e)}"},
                ensure_ascii=False,
            )



class CropImageTool(BaseTool):
    """
    📘 教学笔记：图片区域裁剪工具

    从页面图片中裁剪指定区域，用于保留签名、盖章、logo、照片等
    不可翻译的视觉元素。Brain 看到这些元素后，用 bbox_pct 标记位置，
    本工具从原始高分辨率页面图片中裁剪出来。

    📘 为什么需要这个工具？
    签名、盖章、logo 等是非文字视觉元素：
    - OCR 完全看不到它们（只能识别文字）
    - CV 可能检测到图片区域，但不知道是什么
    - Brain（多模态 LLM）能看到并理解它们
    排版要求"完全一致"，这些元素必须保留在译文中的原始位置。
    """

    name = "crop_image_region"
    description = (
        "从页面图片中裁剪指定区域。用于保留签名、盖章、logo、照片等不可翻译的视觉元素。"
        "提供百分比坐标 bbox_pct = [left%, top%, right%, bottom%]，返回裁剪结果。"
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
                "description": "裁剪区域的百分比坐标 [left%, top%, right%, bottom%]，每个值 0-100",
            },
            "description": {
                "type": "string",
                "description": "区域描述（如'红色公章'、'手写签名'、'公司logo'）",
            },
        },
        "required": ["page_index", "bbox_pct", "description"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        page_index = params["page_index"]
        bbox_pct = params["bbox_pct"]
        description = params.get("description", "图片区域")

        page_images = self.context.get("page_images", [])

        # 📘 page_index 自动修正
        current_idx = self.context.get("current_page_index")
        if current_idx is not None and page_index != current_idx:
            page_index = current_idx

        if page_index < 0 or page_index >= len(page_images):
            return json.dumps(
                {"error": f"page_index {page_index} 超出范围"},
                ensure_ascii=False,
            )

        if not bbox_pct or len(bbox_pct) != 4:
            return json.dumps(
                {"error": "bbox_pct 必须是 [left%, top%, right%, bottom%] 格式"},
                ensure_ascii=False,
            )

        try:
            from PIL import Image as PILImage

            img_bytes = page_images[page_index]
            pil_img = PILImage.open(io.BytesIO(img_bytes))
            w, h = pil_img.size

            left = int(w * bbox_pct[0] / 100)
            top = int(h * bbox_pct[1] / 100)
            right = int(w * bbox_pct[2] / 100)
            bottom = int(h * bbox_pct[3] / 100)

            # 📘 边界保护
            left = max(0, min(left, w - 1))
            top = max(0, min(top, h - 1))
            right = max(left + 1, min(right, w))
            bottom = max(top + 1, min(bottom, h))

            if right - left < 10 or bottom - top < 10:
                return json.dumps(
                    {"error": "裁剪区域太小（< 10px）"},
                    ensure_ascii=False,
                )

            cropped = pil_img.crop((left, top, right, bottom))
            buf = io.BytesIO()
            cropped.save(buf, format="JPEG", quality=92)
            cropped_bytes = buf.getvalue()

            # 📘 存入 context 供 writer 使用
            # key: "cropped_{page_index}_{bbox}" → bytes
            crop_key = f"cropped_{page_index}_{int(bbox_pct[0])}_{int(bbox_pct[1])}_{int(bbox_pct[2])}_{int(bbox_pct[3])}"
            if "cropped_images" not in self.context:
                self.context["cropped_images"] = {}
            self.context["cropped_images"][crop_key] = cropped_bytes

            logger.info(
                f"裁剪成功: 第 {page_index} 页 [{description}] "
                f"bbox={bbox_pct} → {cropped.size[0]}x{cropped.size[1]}px, "
                f"{len(cropped_bytes) / 1024:.1f}KB"
            )

            return json.dumps({
                "success": True,
                "crop_key": crop_key,
                "size_px": list(cropped.size),
                "size_kb": round(len(cropped_bytes) / 1024, 1),
                "description": description,
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"图片裁剪失败: {e}")
            return json.dumps(
                {"error": f"裁剪失败: {type(e).__name__}: {str(e)}"},
                ensure_ascii=False,
            )


class OverlayTextTool(BaseTool):
    """
    📘 教学笔记：文字覆盖工具（保留背景模式）

    在原始页面图片上：
    1. 用背景色矩形覆盖原文字区域（擦除原文）
    2. 在同位置绘制译文（匹配字号、颜色、对齐）

    📘 为什么需要这个工具？
    客户选择"保留背景"时，不能重建 Word 文档（会丢失背景）。
    必须在原图上直接操作：擦掉原文 → 写上译文 → 合成 PDF。
    Brain 决定每个文字区域的 bbox、译文、字号、颜色、对齐方式。

    📘 技术方案：
    - Pillow 的 ImageDraw 绘制矩形（擦除）和文字（写入）
    - 支持中英文混排（需要合适的字体文件）
    - 自动缩小字号以适应区域宽度
    """

    name = "overlay_translated_text"
    description = (
        "在原始页面图片上覆盖译文（Pillow 绘制）。先用背景色擦除原文区域，再在同位置绘制译文。"
        "⚠️ 仅适用于纯白色或纯色背景的简单文档。"
        "有花纹、水印、渐变、彩色底图的文档不要用这个工具（会留下明显的色块补丁），"
        "请改用 generate_translated_image（图片生成 LLM）。"
        "可一次处理多个文字区域（regions 数组）。"
        "处理完成后，修改后的图片自动存入 context，供最终 PDF 合成使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码索引（从0开始）",
            },
            "regions": {
                "type": "array",
                "description": "要覆盖的文字区域列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "bbox_pct": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "区域百分比坐标 [left%, top%, right%, bottom%]",
                        },
                        "translated_text": {
                            "type": "string",
                            "description": "要写入的译文",
                        },
                        "font_size": {
                            "type": "number",
                            "description": "字号（pt），不填则自动计算",
                        },
                        "font_color": {
                            "type": "string",
                            "description": "字体颜色，如 '#000000'、'#FF0000'，默认黑色",
                        },
                        "bg_color": {
                            "type": "string",
                            "description": "背景覆盖色，如 '#FFFFFF'，默认白色。设为 'auto' 则自动检测区域周边颜色",
                        },
                        "align": {
                            "type": "string",
                            "enum": ["left", "center", "right"],
                            "description": "对齐方式，默认 left",
                        },
                        "bold": {
                            "type": "boolean",
                            "description": "是否加粗，默认 false",
                        },
                    },
                    "required": ["bbox_pct", "translated_text"],
                },
            },
        },
        "required": ["page_index", "regions"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def _detect_bg_color(self, pil_img, left, top, right, bottom):
        """📘 自动检测区域周边的背景色（取边缘像素的中位数）"""
        import numpy as np
        w, h = pil_img.size
        # 📘 采样区域边缘外扩 2px 的像素
        margin = 3
        pixels = []
        for x in range(max(0, left - margin), min(w, right + margin)):
            for y_off in [max(0, top - margin), min(h - 1, bottom + margin)]:
                pixels.append(pil_img.getpixel((x, y_off))[:3])
        for y in range(max(0, top - margin), min(h, bottom + margin)):
            for x_off in [max(0, left - margin), min(w - 1, right + margin)]:
                pixels.append(pil_img.getpixel((x_off, y))[:3])
        if not pixels:
            return (255, 255, 255)
        arr = np.array(pixels)
        median = tuple(int(v) for v in np.median(arr, axis=0))
        return median

    def _find_font(self, bold=False):
        """📘 查找可用的中文字体文件"""
        import platform
        candidates = []
        if platform.system() == "Windows":
            font_dir = "C:/Windows/Fonts"
            if bold:
                candidates = [
                    f"{font_dir}/msyhbd.ttc",  # 微软雅黑粗体
                    f"{font_dir}/simhei.ttf",  # 黑体
                    f"{font_dir}/msyh.ttc",    # 微软雅黑
                ]
            else:
                candidates = [
                    f"{font_dir}/msyh.ttc",    # 微软雅黑
                    f"{font_dir}/simsun.ttc",  # 宋体
                    f"{font_dir}/simhei.ttf",  # 黑体
                ]
        else:
            candidates = [
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        page_index = params["page_index"]
        regions = params.get("regions", [])

        page_images = self.context.get("page_images", [])
        current_idx = self.context.get("current_page_index")
        if current_idx is not None and page_index != current_idx:
            page_index = current_idx

        if page_index < 0 or page_index >= len(page_images):
            return json.dumps(
                {"error": f"page_index {page_index} 超出范围"},
                ensure_ascii=False,
            )

        if not regions:
            return json.dumps({"error": "regions 不能为空"}, ensure_ascii=False)

        try:
            from PIL import Image as PILImage, ImageDraw, ImageFont

            img_bytes = page_images[page_index]
            pil_img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
            draw = ImageDraw.Draw(pil_img)
            w, h = pil_img.size

            # 📘 预加载字体
            font_path_normal = self._find_font(bold=False)
            font_path_bold = self._find_font(bold=True)

            success_count = 0
            errors = []

            for i, region in enumerate(regions):
                try:
                    bbox_pct = region["bbox_pct"]
                    text = region["translated_text"]
                    if not text or not bbox_pct or len(bbox_pct) != 4:
                        continue

                    # 📘 百分比 → 像素坐标
                    left = int(w * bbox_pct[0] / 100)
                    top = int(h * bbox_pct[1] / 100)
                    right = int(w * bbox_pct[2] / 100)
                    bottom = int(h * bbox_pct[3] / 100)

                    left = max(0, min(left, w - 1))
                    top = max(0, min(top, h - 1))
                    right = max(left + 1, min(right, w))
                    bottom = max(top + 1, min(bottom, h))

                    region_w = right - left
                    region_h = bottom - top
                    if region_w < 5 or region_h < 5:
                        continue

                    # 📘 背景色
                    bg_color_str = region.get("bg_color", "#FFFFFF")
                    if bg_color_str == "auto":
                        bg_color = self._detect_bg_color(pil_img, left, top, right, bottom)
                    else:
                        bg_color = tuple(int(bg_color_str.lstrip("#")[j:j+2], 16) for j in (0, 2, 4))

                    # 📘 字体颜色
                    font_color_str = region.get("font_color", "#000000")
                    font_color = tuple(int(font_color_str.lstrip("#")[j:j+2], 16) for j in (0, 2, 4))

                    # 📘 字号：指定 or 自动计算
                    is_bold = region.get("bold", False)
                    font_path = font_path_bold if is_bold else font_path_normal
                    target_size = region.get("font_size")

                    if target_size:
                        # 📘 pt → px（按 200 DPI 换算）
                        px_size = int(target_size * 200 / 72)
                    else:
                        # 📘 自动：根据区域高度和文字行数估算
                        line_count = text.count("\n") + 1
                        px_size = max(12, int(region_h / line_count * 0.85))

                    # ── 📘 自动换行（Word Wrap）──
                    # 📘 教学笔记：为什么需要自动换行？
                    # Brain 传来的译文可能是一整段没有 \n 的长文本。
                    # 如果不换行，字号会被缩到极小以适应区域宽度，导致文字几乎看不见。
                    # 解决方案：先加载字体，然后按区域宽度自动折行，
                    # 再根据折行后的行数调整字号使文字填满区域。
                    def _wrap_text(text_to_wrap, font_obj, max_width):
                        """按像素宽度自动折行，支持中英文混排"""
                        wrapped_lines = []
                        for paragraph in text_to_wrap.split("\n"):
                            if not paragraph.strip():
                                wrapped_lines.append("")
                                continue
                            current_line = ""
                            for char in paragraph:
                                test_line = current_line + char
                                bbox = font_obj.getbbox(test_line)
                                line_w = (bbox[2] - bbox[0]) if bbox else 0
                                if line_w > max_width - 4 and current_line:
                                    wrapped_lines.append(current_line)
                                    current_line = char
                                else:
                                    current_line = test_line
                            if current_line:
                                wrapped_lines.append(current_line)
                        return wrapped_lines

                    # 📘 加载字体 → 自动换行 → 如果行数太多则缩小字号重试
                    font = None
                    wrapped_lines = []
                    usable_w = region_w - 4  # 📘 左右各留 2px 边距

                    if font_path:
                        # 📘 从估算字号开始，逐步缩小直到文字能装进区域
                        for attempt_size in range(px_size, 8, -2):
                            try:
                                font = ImageFont.truetype(font_path, attempt_size)
                            except Exception:
                                font = ImageFont.load_default()
                                break
                            wrapped_lines = _wrap_text(text, font, usable_w)
                            # 📘 计算折行后的总高度
                            total_h = 0
                            for wl in wrapped_lines:
                                bbox = font.getbbox(wl) if wl else font.getbbox("A")
                                total_h += (bbox[3] - bbox[1]) if bbox else attempt_size
                            total_h += max(0, (len(wrapped_lines) - 1) * 2)
                            if total_h <= region_h - 4:
                                break
                        else:
                            try:
                                font = ImageFont.truetype(font_path, 10)
                                wrapped_lines = _wrap_text(text, font, usable_w)
                            except Exception:
                                font = ImageFont.load_default()
                                wrapped_lines = text.split("\n")
                    else:
                        font = ImageFont.load_default()
                        wrapped_lines = text.split("\n")

                    # 📘 Step 1: 用背景色覆盖原文区域
                    draw.rectangle([left, top, right, bottom], fill=bg_color)

                    # 📘 Step 2: 绘制译文（使用自动换行后的 wrapped_lines）
                    align = region.get("align", "left")
                    lines = wrapped_lines
                    # 📘 计算总文字高度
                    line_heights = []
                    for line in lines:
                        bbox = font.getbbox(line) if line else font.getbbox("A")
                        lh = (bbox[3] - bbox[1]) if bbox else px_size
                        line_heights.append(lh)
                    total_text_h = sum(line_heights) + max(0, (len(lines) - 1) * 2)

                    # 📘 垂直居中
                    y_start = top + max(0, (region_h - total_text_h) // 2)
                    y_cursor = y_start

                    for line_idx, line in enumerate(lines):
                        if not line.strip():
                            y_cursor += line_heights[line_idx] + 2
                            continue
                        bbox = font.getbbox(line)
                        line_w = (bbox[2] - bbox[0]) if bbox else 0

                        if align == "center":
                            x = left + (region_w - line_w) // 2
                        elif align == "right":
                            x = right - line_w - 2
                        else:
                            x = left + 2

                        draw.text((x, y_cursor), line, fill=font_color, font=font)
                        y_cursor += line_heights[line_idx] + 2

                    success_count += 1

                except Exception as e:
                    errors.append(f"区域 {i}: {str(e)}")

            # 📘 保存修改后的图片到 context
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=92)
            overlay_bytes = buf.getvalue()

            if "overlay_images" not in self.context:
                self.context["overlay_images"] = {}
            self.context["overlay_images"][page_index] = overlay_bytes

            logger.info(
                f"文字覆盖完成: 第 {page_index} 页, "
                f"{success_count}/{len(regions)} 个区域成功"
            )

            result = {
                "success": True,
                "page_index": page_index,
                "regions_processed": success_count,
                "regions_total": len(regions),
                "image_size_kb": round(len(overlay_bytes) / 1024, 1),
            }
            if errors:
                result["errors"] = errors

            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.error(f"文字覆盖失败: {e}")
            return json.dumps(
                {"error": f"覆盖失败: {type(e).__name__}: {str(e)}"},
                ensure_ascii=False,
            )
