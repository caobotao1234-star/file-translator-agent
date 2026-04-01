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

    Agent 传 keys 列表，工具自己从 parsed_data 读取原文（含格式标记），
    翻译后返回 {key: 译文} 映射。工具内部处理格式标记保留。
    """

    name = "translate_page"
    description = (
        "翻译指定的段落。传入 keys 数组（从 get_page_content 获取）和目标语言。"
        "也可以直接传 texts 数组。工具内部自动处理格式标记保留。"
        "返回 {key: 译文} 或 translations 数组。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要翻译的段落 key 列表（从 get_page_content 获取）",
            },
            "texts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "直接传入文本列表（备选，不传 keys 时用）",
            },
            "target_lang": {
                "type": "string",
                "description": "目标语言，如 '英文'、'日文'",
            },
            "context_hint": {
                "type": "string",
                "description": "可选的上下文提示",
            },
        },
        "required": ["target_lang"],
    }

    def __init__(self, translate_pipeline=None, parse_tool=None):
        self.translate_pipeline = translate_pipeline
        self._parse_tool = parse_tool

    def execute(self, params: dict) -> str:
        target_lang = params["target_lang"]
        context_hint = params.get("context_hint", "")
        keys = params.get("keys", [])
        texts = params.get("texts", [])

        if not self.translate_pipeline:
            return json.dumps({"error": "翻译模型未初始化"}, ensure_ascii=False)

        try:
            lang_hint = {
                "英文": "English", "中文": "Chinese", "日文": "Japanese",
                "韩文": "Korean", "法文": "French", "德文": "German",
                "西班牙文": "Spanish", "俄文": "Russian",
            }
            lang_english = lang_hint.get(target_lang, target_lang)
            import re
            TAG_RE = re.compile(r'<r(\d+)>(.*?)</r\1>', re.DOTALL)

            # 📘 如果传了 keys，从 parsed_data 读取原文（含格式标记）
            if keys and self._parse_tool:
                parsed = self._parse_tool._parsed_cache.get("_last", {})
                item_map = {item["key"]: item for item in parsed.get("items", [])}
                texts = []
                key_list = []
                for k in keys:
                    item = item_map.get(k)
                    if item and item.get("full_text"):
                        texts.append(item["full_text"])
                        key_list.append(k)
            else:
                key_list = [f"t_{i}" for i in range(len(texts))]

            if not texts:
                return json.dumps({"translations": {}, "count": 0}, ensure_ascii=False)

            # 📘 提取纯文本用于翻译（去掉标记）
            plain_texts = []
            tagged_flags = []
            for text in texts:
                matches = TAG_RE.findall(text)
                if matches:
                    pure = "".join(content for _, content in matches)
                    plain_texts.append(pure)
                    tagged_flags.append(True)
                else:
                    plain_texts.append(text)
                    tagged_flags.append(False)

            # 翻译
            results = self.translate_pipeline._translate_batch(
                plain_texts, target_lang, lang_english,
                cross_page_hint=context_hint or "",
            )

            # 📘 构建 key -> 译文 映射
            translations = {}
            for i, (key, trans) in enumerate(zip(key_list, results)):
                translations[key] = trans

            return json.dumps({
                "translations": translations,
                "count": len(translations),
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"翻译失败: {e}")
            return json.dumps(
                {"error": f"翻译失败: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
