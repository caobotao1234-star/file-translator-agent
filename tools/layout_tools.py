# tools/layout_tools.py
# =============================================================
# 📘 教学笔记：PDF 排版工具集（Layout Tools）
# =============================================================
# 这些工具是 PDF Layout Agent 的"手脚"——
# 规划者（Agent Brain）通过 tool_call 调用这些工具来检测和修正排版问题。
#
# 📘 设计原则：
#   1. 每个工具继承 BaseTool，实现 execute(params) -> str
#   2. 工具操作的是内存中的 PDF 文档（fitz.Document），不直接写文件
#   3. 所有修改都是可逆的——通过 translations 和 overrides 字典追踪
#   4. 工具返回 JSON 字符串，LLM 能直接理解
#
# 📘 工具清单：
#   - MeasureOverflowTool: 检测所有文本块的溢出情况
#   - ResizeFontTool: 调整指定文本块的字号
#   - RetranslateShorterTool: 让翻译模型重新翻译（更短版本）
#   - RenderPageTool: 渲染指定页面为图片供审查
#   - SaveLayoutRuleTool: 把有效的修正策略沉淀为持久化规则
# =============================================================

import json
import base64
import fitz  # PyMuPDF
from typing import Any, Dict, List, Optional
from tools.base_tool import BaseTool
from core.logger import get_logger

logger = get_logger("layout_tools")


class MeasureOverflowTool(BaseTool):
    """
    📘 教学笔记：溢出检测工具

    精确测量每个文本块的译文是否超出可用空间。
    使用 PyMuPDF 的 get_text_length / insert_htmlbox 模拟渲染，
    计算译文实际需要的像素宽高 vs 可用空间。

    返回每个文本块的溢出状态：
    - overflow_ratio: 溢出比例（1.0 = 刚好，1.5 = 超出 50%）
    - status: "ok" / "tight" / "overflow"
    - suggested_action: 建议的修正方式
    """

    name = "measure_overflow"
    description = (
        "检测翻译后的 PDF 文本块溢出情况。"
        "返回每个文本块的溢出比例和建议修正方式。"
        "可以指定检测单页或全部页面。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码索引（从0开始），-1 表示检测所有页面",
            },
        },
        "required": ["page_index"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        """
        📘 context 包含：
        - parsed_data: 解析后的文档数据
        - translations: {key: 译文} 映射
        - overrides: {key: {fontsize: N}} 字号覆盖
        """
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        page_index = params["page_index"]
        parsed_data = self.context.get("parsed_data", {})
        translations = self.context.get("translations", {})
        overrides = self.context.get("overrides", {})

        items = parsed_data.get("items", [])
        results = []

        for item in items:
            key = item["key"]
            if key not in translations:
                continue
            if item.get("type") != "pdf_block":
                continue

            # 📘 过滤页面
            item_page = int(key.split("_")[0][2:])
            if page_index >= 0 and item_page != page_index:
                continue

            translated = translations[key]
            bbox = item.get("text_bbox", item["bbox"])
            fmt = item["dominant_format"]
            font_size = overrides.get(key, {}).get("fontsize", fmt["font_size"])

            # 📘 计算可用空间
            avail_w = bbox[2] - bbox[0]
            avail_h = bbox[3] - bbox[1]

            if avail_w <= 0 or avail_h <= 0:
                continue

            # 📘 估算译文渲染尺寸
            # 英文字符宽度 ≈ 0.55 × 字号，CJK ≈ 1.0 × 字号
            has_cjk = any('\u4e00' <= c <= '\u9fff' for c in translated)
            char_width = font_size * (1.0 if has_cjk else 0.55)
            line_height = font_size * 1.3

            if char_width <= 0:
                continue

            chars_per_line = max(1, int(avail_w / char_width))
            text_len = len(translated)
            needed_lines = max(1, -(-text_len // chars_per_line))  # ceil division
            needed_h = needed_lines * line_height

            overflow_ratio = round(needed_h / avail_h, 2) if avail_h > 0 else 99.0

            # 📘 判断状态
            if overflow_ratio <= 1.0:
                status = "ok"
                action = None
            elif overflow_ratio <= 1.3:
                status = "tight"
                action = "resize_font"
            else:
                status = "overflow"
                # 📘 严重溢出时建议重新翻译更短版本
                action = "retranslate_shorter" if overflow_ratio > 2.0 else "resize_font"

            results.append({
                "key": key,
                "page": item_page,
                "original_text": item["full_text"][:30],
                "translated_text": translated[:30],
                "font_size": font_size,
                "avail_size": f"{avail_w:.0f}x{avail_h:.0f}",
                "overflow_ratio": overflow_ratio,
                "status": status,
                "suggested_action": action,
            })

        # 📘 统计摘要
        overflow_count = sum(1 for r in results if r["status"] == "overflow")
        tight_count = sum(1 for r in results if r["status"] == "tight")
        ok_count = sum(1 for r in results if r["status"] == "ok")

        return json.dumps({
            "summary": {
                "total": len(results),
                "ok": ok_count,
                "tight": tight_count,
                "overflow": overflow_count,
            },
            "items": results,
        }, ensure_ascii=False)


class ResizeFontTool(BaseTool):
    """
    📘 教学笔记：字号调整工具

    修改指定文本块的字号。修改记录在 context["overrides"] 中，
    最终由 pdf_writer 在写入时应用。

    📘 为什么不直接改 PDF？
    因为 PDF 写入是一次性的（redaction + insert），
    不能反复修改。所以工具只修改 overrides 字典，
    最终由 writer 统一应用。
    """

    name = "resize_font"
    description = (
        "调整指定文本块的字号。可以指定绝对字号或相对缩放。"
        "修改会在最终写入 PDF 时生效。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要调整的文本块 key 列表",
            },
            "fontsize": {
                "type": "number",
                "description": "目标字号（绝对值，如 10.0）。与 scale 二选一。",
            },
            "scale": {
                "type": "number",
                "description": "缩放比例（如 0.8 表示缩小到 80%）。与 fontsize 二选一。",
            },
            "min_fontsize": {
                "type": "number",
                "description": "最小字号下限（默认 5.0），防止缩得太小不可读",
            },
        },
        "required": ["keys"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        keys = params["keys"]
        target_fontsize = params.get("fontsize")
        scale = params.get("scale")
        min_fontsize = params.get("min_fontsize", 5.0)

        overrides = self.context.get("overrides", {})
        parsed_data = self.context.get("parsed_data", {})

        # 📘 构建 key → item 映射
        item_map = {item["key"]: item for item in parsed_data.get("items", [])}

        results = []
        for key in keys:
            item = item_map.get(key)
            if not item:
                results.append({"key": key, "error": "key not found"})
                continue

            current_size = overrides.get(key, {}).get(
                "fontsize", item["dominant_format"]["font_size"]
            )

            if target_fontsize is not None:
                new_size = target_fontsize
            elif scale is not None:
                new_size = current_size * scale
            else:
                results.append({"key": key, "error": "need fontsize or scale"})
                continue

            new_size = round(max(min_fontsize, new_size), 1)

            if key not in overrides:
                overrides[key] = {}
            overrides[key]["fontsize"] = new_size

            results.append({
                "key": key,
                "old_fontsize": current_size,
                "new_fontsize": new_size,
            })

        # 📘 写回 context
        self.context["overrides"] = overrides

        return json.dumps({
            "adjusted": len([r for r in results if "error" not in r]),
            "results": results,
        }, ensure_ascii=False)


class RetranslateShorterTool(BaseTool):
    """
    📘 教学笔记：重新翻译工具（更短版本）

    让翻译模型重新翻译指定文本块，给更严格的长度限制。
    翻译结果直接更新到 context["translations"] 中。

    📘 为什么需要这个工具？
    有时候缩小字号还是放不下（比如字号已经很小了），
    这时候需要让翻译模型用更精简的表达重新翻译。
    规划者决定什么时候用缩字、什么时候用重新翻译。
    """

    name = "retranslate_shorter"
    description = (
        "让翻译模型重新翻译指定文本块，要求更短的译文。"
        "可以指定最大字符数限制。翻译结果会直接更新。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "文本块 key"},
                        "max_chars": {"type": "integer", "description": "最大字符数限制"},
                    },
                    "required": ["key", "max_chars"],
                },
                "description": "要重新翻译的文本块列表",
            },
            "target_lang": {
                "type": "string",
                "description": "目标语言，如'英文'",
            },
        },
        "required": ["items", "target_lang"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        retranslate_items = params["items"]
        target_lang = params["target_lang"]

        pipeline = self.context.get("translate_pipeline")
        parsed_data = self.context.get("parsed_data", {})
        translations = self.context.get("translations", {})

        if not pipeline:
            return json.dumps({"error": "translate_pipeline not available"}, ensure_ascii=False)

        item_map = {item["key"]: item for item in parsed_data.get("items", [])}

        results = []
        # 📘 逐个重新翻译（带严格长度限制）
        for rt_item in retranslate_items:
            key = rt_item["key"]
            max_chars = rt_item["max_chars"]

            item = item_map.get(key)
            if not item:
                results.append({"key": key, "error": "key not found"})
                continue

            original_text = item["full_text"]
            old_translation = translations.get(key, "")

            # 📘 构建带严格长度限制的翻译请求
            texts = [f"<<LIMIT:{max_chars}>> {original_text}"]
            try:
                new_translations = pipeline.translate_batch(texts, target_lang=target_lang)
                new_text = new_translations[0] if new_translations else old_translation
            except Exception as e:
                logger.error(f"重新翻译失败 key={key}: {e}")
                results.append({"key": key, "error": str(e)})
                continue

            # 📘 更新 translations
            translations[key] = new_text

            results.append({
                "key": key,
                "old_text": old_translation[:40],
                "new_text": new_text[:40],
                "old_len": len(old_translation),
                "new_len": len(new_text),
                "max_chars": max_chars,
            })

        return json.dumps({
            "retranslated": len([r for r in results if "error" not in r]),
            "results": results,
        }, ensure_ascii=False)


class RenderPageTool(BaseTool):
    """
    📘 教学笔记：页面渲染工具

    将当前状态的 PDF 页面渲染为图片，供规划者视觉审查。
    规划者可以对比翻译前后的页面截图，判断排版是否合理。

    📘 实现方式：
    1. 在内存中创建临时 PDF（应用当前 translations + overrides）
    2. 渲染指定页面为 JPEG
    3. 返回 base64 编码的图片

    📘 为什么不直接用原 PDF？
    因为我们需要看到"应用了当前修正后"的效果，
    而不是原始 PDF 或上一次写入的结果。
    """

    name = "render_page_preview"
    description = (
        "渲染 PDF 页面的当前状态为图片（应用了所有字号调整和重新翻译）。"
        "返回 base64 编码的 JPEG 图片，供视觉审查。"
        "也可以渲染原始页面（翻译前）用于对比。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "页码索引（从0开始）",
            },
            "render_original": {
                "type": "boolean",
                "description": "是否渲染原始页面（翻译前），默认 false",
            },
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
        render_original = params.get("render_original", False)
        source_path = self.context.get("source_path", "")

        if not source_path:
            return json.dumps({"error": "source_path not available"}, ensure_ascii=False)

        try:
            if render_original:
                # 📘 渲染原始页面
                doc = fitz.open(source_path)
                if page_index >= len(doc):
                    doc.close()
                    return json.dumps({"error": f"page_index {page_index} out of range"}, ensure_ascii=False)
                page = doc[page_index]
                zoom = 150 / 72.0  # 150 DPI（够看清文字，不会太大）
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat)
                img_bytes = pix.tobytes("jpeg", jpg_quality=80)
                doc.close()
            else:
                # 📘 渲染当前状态：先写入临时 PDF，再渲染
                import tempfile
                import os
                from translator.pdf_writer import write_pdf

                parsed_data = self.context.get("parsed_data", {})
                translations = self.context.get("translations", {})
                overrides = self.context.get("overrides", {})
                format_engine = self.context.get("format_engine")

                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                tmp.close()
                try:
                    write_pdf(
                        parsed_data, translations, tmp.name,
                        format_engine, source_path=source_path,
                        layout_overrides=overrides,
                    )
                    doc = fitz.open(tmp.name)
                    if page_index >= len(doc):
                        doc.close()
                        return json.dumps({"error": f"page_index {page_index} out of range"}, ensure_ascii=False)
                    page = doc[page_index]
                    zoom = 150 / 72.0
                    mat = fitz.Matrix(zoom, zoom)
                    pix = page.get_pixmap(matrix=mat)
                    img_bytes = pix.tobytes("jpeg", jpg_quality=80)
                    doc.close()
                finally:
                    os.unlink(tmp.name)

            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            logger.info(f"渲染页面 {page_index} ({'原始' if render_original else '当前状态'})")

            return json.dumps({
                "page_index": page_index,
                "type": "original" if render_original else "current",
                "image_base64": img_b64,
                "image_size_kb": round(len(img_bytes) / 1024, 1),
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"渲染页面失败: {e}")
            return json.dumps({"error": f"渲染失败: {str(e)}"}, ensure_ascii=False)


class SaveLayoutRuleTool(BaseTool):
    """
    📘 教学笔记：规则沉淀工具

    当规划者发现某个排版修正策略有效且可复用时，
    调用此工具将策略沉淀为持久化规则。

    📘 规则类型：
    - font_scale: 特定场景下的字号缩放规则
      例："中→英标题溢出时缩小到原字号的 80%"
    - max_chars_ratio: 特定角色的最大字符数比例
      例："标题类文本，译文长度不超过原文的 2.5 倍"

    规则保存在 translator_config/layout_rules.json 中，
    下次翻译时自动加载并应用。
    """

    name = "save_layout_rule"
    description = (
        "将有效的排版修正策略沉淀为持久化规则。"
        "规则会保存到配置文件，下次翻译时自动应用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "rule_name": {
                "type": "string",
                "description": "规则名称（简短描述）",
            },
            "rule_type": {
                "type": "string",
                "enum": ["font_scale", "max_chars_ratio", "custom"],
                "description": "规则类型",
            },
            "condition": {
                "type": "object",
                "description": "触发条件（如 text_role、overflow_ratio 等）",
            },
            "action": {
                "type": "object",
                "description": "修正动作（如 scale: 0.8、max_chars_factor: 2.5）",
            },
        },
        "required": ["rule_name", "rule_type", "condition", "action"],
    }

    def __init__(self, context: Dict[str, Any] = None):
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        import os

        rule = {
            "name": params["rule_name"],
            "type": params["rule_type"],
            "condition": params["condition"],
            "action": params["action"],
        }

        rules_path = "translator_config/layout_rules.json"
        os.makedirs(os.path.dirname(rules_path), exist_ok=True)

        # 📘 加载已有规则
        existing_rules = []
        if os.path.exists(rules_path):
            try:
                with open(rules_path, "r", encoding="utf-8") as f:
                    existing_rules = json.load(f)
            except Exception:
                existing_rules = []

        # 📘 检查是否已存在同名规则（更新而非重复添加）
        updated = False
        for i, r in enumerate(existing_rules):
            if r.get("name") == rule["name"]:
                existing_rules[i] = rule
                updated = True
                break
        if not updated:
            existing_rules.append(rule)

        # 📘 保存
        with open(rules_path, "w", encoding="utf-8") as f:
            json.dump(existing_rules, f, ensure_ascii=False, indent=2)

        logger.info(f"排版规则已{'更新' if updated else '保存'}: {rule['name']}")

        return json.dumps({
            "saved": True,
            "rule_name": rule["name"],
            "total_rules": len(existing_rules),
            "action": "updated" if updated else "created",
        }, ensure_ascii=False)
