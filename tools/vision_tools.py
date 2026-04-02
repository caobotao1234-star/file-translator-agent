# tools/vision_tools.py
# =============================================================
# 📘 教学笔记：视觉工具（扫描件 PDF 专用）
# =============================================================
# 这些工具让 Agent 能"看到"文档页面图片，处理扫描件。
# Agent 自己判断是否需要用这些工具（看到 scanned_PDF 时）。
#
# 旧架构的 OCRTool/CVTool/ImageGenTool/OverlayTextTool/CropImageTool
# 已经实现得很好，这里直接复用，只加一个 get_page_image 工具。
# =============================================================

import json
import os
import io
import base64
from typing import Any, Dict, List

from core.agent_loop import BaseTool
from core.logger import get_logger

logger = get_logger("vision_tools")


class GetPageImageTool(BaseTool):
    """
    📘 获取文档页面的图片

    把 PDF/PPT 的指定页渲染为图片（base64），让 Agent 能看到页面内容。
    扫描件 PDF 必须用这个工具才能看到内容。
    """

    name = "get_page_image"
    description = (
        "获取文档指定页的图片（base64 JPEG）。"
        "用于查看扫描件 PDF 的页面内容，或 PPT 的视觉效果。"
        "返回图片的 base64 编码，你可以直接查看。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码（0-based）",
            },
        },
        "required": ["page_index"],
    }

    def __init__(self):
        self._page_images_b64: List[str] = []
        self._page_images_bytes: List[bytes] = []
        self._filepath: str = ""

    def load_pdf(self, filepath: str):
        """预渲染 PDF 所有页面为图片"""
        import fitz
        self._filepath = filepath
        self._page_images_b64 = []
        self._page_images_bytes = []

        doc = fitz.open(filepath)
        for i in range(len(doc)):
            page = doc[i]
            # 高分辨率给 OCR/CV
            zoom_hi = 200 / 72.0
            mat_hi = fitz.Matrix(zoom_hi, zoom_hi)
            pix_hi = page.get_pixmap(matrix=mat_hi)
            jpeg_hi = pix_hi.tobytes("jpeg", jpg_quality=88)
            self._page_images_bytes.append(jpeg_hi)

            # 低分辨率给 Agent 看（省 tokens）
            zoom_lo = 150 / 72.0
            mat_lo = fitz.Matrix(zoom_lo, zoom_lo)
            pix_lo = page.get_pixmap(matrix=mat_lo)
            jpeg_lo = pix_lo.tobytes("jpeg", jpg_quality=75)
            self._page_images_b64.append(base64.b64encode(jpeg_lo).decode("utf-8"))
        doc.close()
        logger.info(f"PDF 渲染完成: {len(self._page_images_b64)} 页")

    def execute(self, params: dict) -> str:
        page_idx = params["page_index"]

        if not self._page_images_b64:
            return json.dumps({"error": "请先通过 parse_document 加载 PDF"}, ensure_ascii=False)

        if page_idx < 0 or page_idx >= len(self._page_images_b64):
            return json.dumps({
                "error": f"页码越界: {page_idx}, 总页数: {len(self._page_images_b64)}"
            }, ensure_ascii=False)

        return json.dumps({
            "page_index": page_idx,
            "image_base64": self._page_images_b64[page_idx],
            "total_pages": len(self._page_images_b64),
        }, ensure_ascii=False)


def create_scan_tools(
    page_image_tool: GetPageImageTool,
    image_gen_engine=None,
):
    """
    📘 创建扫描件专用工具集

    复用旧架构的 OCRTool/CVTool/ImageGenTool/OverlayTextTool/CropImageTool，
    它们已经实现得很好，只需要传入正确的 context。

    返回工具列表，Agent 自己决定用哪些。
    """
    from tools.scan_tools import OCRTool, CVTool, ImageGenTool, CropImageTool, OverlayTextTool

    # 📘 共享 context：page_images 和 current_page_index
    # 旧工具通过 context 获取页面图片数据
    context = {
        "page_images": page_image_tool._page_images_bytes,
        "current_page_index": 0,
    }

    tools = [
        OCRTool(context=context),
        CVTool(context=context),
        CropImageTool(context=context),
        OverlayTextTool(context=context),
    ]

    if image_gen_engine:
        tools.append(ImageGenTool(
            image_gen_engine=image_gen_engine,
            context=context,
        ))

    return tools, context


class SaveScanPDFTool(BaseTool):
    """
    📘 保存扫描件翻译结果为 PDF

    把 generate_translated_image / overlay_translated_text 生成的
    页面图片合成为 PDF 文件。
    """

    name = "save_scan_pdf"
    description = (
        "把扫描件翻译后的页面图片保存为 PDF 文件。"
        "在调用 generate_translated_image 或 overlay_translated_text 之后使用。"
        "会自动收集所有已生成的页面图片，合成为多页 PDF。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "output_path": {
                "type": "string",
                "description": "输出 PDF 文件路径",
            },
        },
        "required": ["output_path"],
    }

    def __init__(self, page_image_tool: GetPageImageTool, scan_context: dict = None):
        self._page_image_tool = page_image_tool
        self._scan_context = scan_context or {}

    def execute(self, params: dict) -> str:
        output_path = params["output_path"]

        try:
            from translator.scan_writer import write_overlay_pdf

            overlay_images = self._scan_context.get("overlay_images", {})
            page_images = self._page_image_tool._page_images_bytes
            num_pages = len(page_images)

            if num_pages == 0:
                return json.dumps({"error": "没有页面图片，请先调用 parse_document"}, ensure_ascii=False)

            result_path = write_overlay_pdf(
                overlay_images=overlay_images,
                page_images=page_images,
                output_path=output_path,
                num_pages=num_pages,
            )

            return json.dumps({
                "success": True,
                "output_path": result_path,
                "pages": num_pages,
                "overlay_pages": len(overlay_images),
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"保存扫描件 PDF 失败: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)
