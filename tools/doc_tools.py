# tools/doc_tools.py
# =============================================================
# 📘 教学笔记：文档工具（Agent 的手脚）
# =============================================================
# 这些工具把现有的 parser/writer 包装成 Agent 可调用的接口。
# Agent 不知道内部实现，只知道"调 parse_document 可以解析文档"。
# =============================================================

import json
import os
from typing import Any, Dict, List, Optional

from core.agent_loop import BaseTool
from core.logger import get_logger

logger = get_logger("doc_tools")


class ParseDocumentTool(BaseTool):
    """
    📘 解析文档工具

    Agent 调用后获得文档的结构概览：类型、页数、每页段落数。
    内部自动识别文件类型，调用对应的 parser。
    """

    name = "parse_document"
    description = (
        "解析文档文件，返回文档结构概览（类型、页数、每页段落数）。"
        "支持 .pptx / .docx / .pdf 文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": "文档文件路径",
            },
        },
        "required": ["filepath"],
    }

    def __init__(self, format_engine=None):
        self.format_engine = format_engine
        self._parsed_cache: Dict[str, dict] = {}

    def execute(self, params: dict) -> str:
        filepath = params["filepath"]
        ext = os.path.splitext(filepath)[1].lower()

        try:
            if ext == ".pptx":
                from translator.pptx_parser import parse_pptx
                parsed = parse_pptx(filepath)
                doc_type = "PPT"
            elif ext == ".docx":
                from translator.docx_parser import parse_docx
                parsed = parse_docx(filepath)
                doc_type = "Word"
            elif ext == ".pdf":
                parsed = self._parse_pdf(filepath)
                doc_type = parsed.pop("_doc_type", "PDF")
            else:
                return json.dumps(
                    {"error": f"不支持的文件类型: {ext}"},
                    ensure_ascii=False,
                )

            # 缓存解析结果供后续工具使用
            self._parsed_cache[filepath] = parsed
            self._parsed_cache["_last"] = parsed
            self._parsed_cache["_last_path"] = filepath
            self._parsed_cache["_last_type"] = doc_type
            self._parsed_cache["_last_ext"] = ext

            # 构建概览
            items = parsed.get("items", [])
            total = len(items)
            non_empty = sum(1 for i in items if not i.get("is_empty"))

            # 按页分组统计
            page_stats = {}
            for item in items:
                key = item.get("key", "")
                prefix = key.split("_")[0] if "_" in key else "p0"
                page_stats[prefix] = page_stats.get(prefix, 0) + 1

            overview = {
                "doc_type": doc_type,
                "filepath": filepath,
                "total_items": total,
                "non_empty_items": non_empty,
                "pages": len(page_stats),
                "items_per_page": dict(
                    sorted(page_stats.items())
                ),
            }
            return json.dumps(overview, ensure_ascii=False)

        except Exception as e:
            logger.error(f"解析文档失败: {e}")
            return json.dumps(
                {"error": f"解析失败: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )

    def _parse_pdf(self, filepath: str) -> dict:
        """PDF 解析：先检测是否扫描件"""
        from translator.scan_parser import detect_scan_pdf
        if detect_scan_pdf(filepath):
            return {"items": [], "_doc_type": "scanned_PDF"}
        from translator.pdf_parser import parse_pdf
        parsed = parse_pdf(filepath)
        parsed["_doc_type"] = "PDF"
        return parsed


class GetPageContentTool(BaseTool):
    """
    📘 获取指定页（或多页）的文本内容

    支持单页和批量获取，减少 Agent turn 数。
    """

    name = "get_page_content"
    description = (
        "获取指定页/幻灯片的所有文本段落。支持批量获取多页。"
        "返回每个段落的 key、原文、类型。需要先调用 parse_document。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "单页页码（0-based）。与 page_range 二选一。",
            },
            "page_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "多页页码列表，如 [0,1,2,3,4]。一次获取多页内容。",
            },
        },
    }

    def __init__(self, parse_tool: ParseDocumentTool):
        self._parse_tool = parse_tool

    def _get_page_prefix(self, page_idx: int, doc_type: str) -> str:
        if doc_type == "PPT":
            return f"s{page_idx}_"
        elif doc_type in ("PDF", "scanned_PDF"):
            return f"pg{page_idx}_"
        else:
            return f"p{page_idx}"

    def execute(self, params: dict) -> str:
        parsed = self._parse_tool._parsed_cache.get("_last")
        if not parsed:
            return json.dumps({"error": "请先调用 parse_document"}, ensure_ascii=False)

        doc_type = self._parse_tool._parsed_cache.get("_last_type", "")

        # 支持单页或多页
        page_indices = params.get("page_range")
        if page_indices is None:
            page_idx = params.get("page_index", 0)
            page_indices = [page_idx]

        all_results = {}
        total_items = 0
        for pidx in page_indices:
            prefix = self._get_page_prefix(pidx, doc_type)
            page_items = []
            for item in parsed.get("items", []):
                key = item.get("key", "")
                if key.startswith(prefix):
                    page_items.append({
                        "key": key,
                        "text": item.get("full_text", ""),
                        "type": item.get("type", ""),
                    })
            all_results[str(pidx)] = page_items
            total_items += len(page_items)

        return json.dumps({
            "pages_requested": len(page_indices),
            "total_items": total_items,
            "pages": all_results,
        }, ensure_ascii=False)


class WriteDocumentTool(BaseTool):
    """
    📘 写入翻译结果到输出文件

    Agent 翻译完成后调用，把翻译结果写入新文件。
    内部根据文件类型调用对应的 writer。
    """

    name = "write_document"
    description = (
        "将翻译结果写入输出文件。"
        "需要提供 translations（key->译文 的映射）和输出路径。"
        "需要先调用 parse_document。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "translations": {
                "type": "object",
                "description": "翻译结果映射: {key: 译文}",
            },
            "output_path": {
                "type": "string",
                "description": "输出文件路径",
            },
        },
        "required": ["translations", "output_path"],
    }

    def __init__(self, parse_tool: ParseDocumentTool, format_engine=None):
        self._parse_tool = parse_tool
        self.format_engine = format_engine

    def execute(self, params: dict) -> str:
        translations = params["translations"]
        output_path = params["output_path"]

        parsed = self._parse_tool._parsed_cache.get("_last")
        source_path = self._parse_tool._parsed_cache.get("_last_path")
        ext = self._parse_tool._parsed_cache.get("_last_ext", "")

        if not parsed or not source_path:
            return json.dumps({"error": "请先调用 parse_document"}, ensure_ascii=False)

        try:
            if ext == ".pptx":
                from translator.pptx_writer import write_pptx
                write_pptx(parsed, translations, output_path,
                           self.format_engine, source_path)
            elif ext == ".docx":
                from translator.docx_writer import write_docx
                write_docx(parsed, translations, output_path,
                           self.format_engine, source_path)
            elif ext == ".pdf":
                from translator.pdf_writer import write_pdf
                write_pdf(parsed, translations, output_path,
                          self.format_engine, source_path)
            else:
                return json.dumps({"error": f"不支持写入: {ext}"}, ensure_ascii=False)

            return json.dumps({
                "success": True,
                "output_path": output_path,
                "items_written": len(translations),
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"写入文档失败: {e}")
            return json.dumps(
                {"error": f"写入失败: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
