# tools/translate_tools.py
# =============================================================
# 📘 教学笔记：翻译工具
# =============================================================
# Agent 可以选择调用这个工具来翻译文本（内部用便宜模型），
# 也可以选择自己直接翻译（看到图片时更准确）。
# 我们不规定，Agent 自己决定。
# =============================================================

import json
from typing import Dict, List

from core.agent_loop import BaseTool
from core.logger import get_logger

logger = get_logger("translate_tools")


class TranslatePageTool(BaseTool):
    """
    📘 翻译一组文本

    支持批量翻译（多页的文本一次性发过来）。
    内部调用 TranslatePipeline（便宜的翻译模型）。
    """

    name = "translate_page"
    description = (
        "翻译一组文本段落。输入 texts 数组和目标语言，"
        "返回对应的翻译结果数组。内部使用专业翻译模型，高效且便宜。"
        "可以一次传入多页的文本一起翻译，提高效率。"
        "如果你能看到图片且需要上下文理解，"
        "也可以选择自己直接翻译而不调用此工具。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "texts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "待翻译的文本列表（可以包含多页的文本）",
            },
            "target_lang": {
                "type": "string",
                "description": "目标语言，如 '英文'、'日文'",
            },
            "context_hint": {
                "type": "string",
                "description": "可选的上下文提示（如文档主题、前文摘要）",
            },
        },
        "required": ["texts", "target_lang"],
    }

    def __init__(self, translate_pipeline=None):
        self.translate_pipeline = translate_pipeline

    def execute(self, params: dict) -> str:
        texts = params["texts"]
        target_lang = params["target_lang"]
        context_hint = params.get("context_hint", "")

        if not texts:
            return json.dumps({"translations": []}, ensure_ascii=False)

        if not self.translate_pipeline:
            return json.dumps(
                {"error": "翻译模型未初始化"},
                ensure_ascii=False,
            )

        try:
            # 如果有上下文提示，附加到每段文本前面
            if context_hint:
                enriched = [f"[上下文: {context_hint}] {t}" for t in texts]
                results = self.translate_pipeline.translate_batch(
                    enriched, target_lang=target_lang
                )
            else:
                results = self.translate_pipeline.translate_batch(
                    texts, target_lang=target_lang
                )

            return json.dumps({
                "translations": results,
                "count": len(results),
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"翻译失败: {e}")
            return json.dumps(
                {"error": f"翻译失败: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
