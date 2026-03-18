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
