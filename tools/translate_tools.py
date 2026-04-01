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
            lang_hint = {
                "英文": "English", "中文": "Chinese", "日文": "Japanese",
                "韩文": "Korean", "法文": "French", "德文": "German",
                "西班牙文": "Spanish", "俄文": "Russian",
            }
            lang_english = lang_hint.get(target_lang, target_lang)
            hint = context_hint or ""

            # 📘 教学笔记：格式标记的可靠保留策略
            # 翻译模型经常丢失 <r0>...</r0> 标记，不管怎么提醒都不可靠。
            # 可靠方案：把带标记的文本拆开，提取纯文本翻译，再重新组装标记。
            import re
            TAG_RE = re.compile(r'<r(\d+)>(.*?)</r\1>', re.DOTALL)

            # 分离：带标记的文本 vs 普通文本
            tagged_indices = []  # 带标记的文本索引
            plain_texts = []  # 要发给翻译模型的纯文本
            tag_maps = {}  # {index: [(tag_idx, text), ...]}

            for i, text in enumerate(texts):
                matches = TAG_RE.findall(text)
                if matches:
                    # 带标记：提取每个 Run 的纯文本，拼成一段发给翻译模型
                    tagged_indices.append(i)
                    tag_maps[i] = [(int(idx), content) for idx, content in matches]
                    # 把所有 Run 的文本拼成一段，用 ||| 分隔
                    run_texts = [content for _, content in matches]
                    plain_texts.append(" ||| ".join(run_texts))
                else:
                    plain_texts.append(text)

            # 翻译所有纯文本
            results = self.translate_pipeline._translate_batch(
                plain_texts, target_lang, lang_english,
                cross_page_hint=hint,
            )

            # 重新组装带标记的译文
            final_results = []
            for i, (orig, trans) in enumerate(zip(texts, results)):
                if i in tag_maps:
                    # 把翻译结果按 ||| 拆回各个 Run
                    parts = [p.strip() for p in trans.split("|||")]
                    tag_info = tag_maps[i]
                    if len(parts) >= len(tag_info):
                        # 重新包装标记
                        tagged = "".join(
                            f"<r{idx}>{parts[j]}</r{idx}>"
                            for j, (idx, _) in enumerate(tag_info)
                        )
                        final_results.append(tagged)
                    else:
                        # 拆分数量不匹配，用整段译文包在 r0 里
                        logger.warning(
                            f"标记重组失败: 期望 {len(tag_info)} 段, "
                            f"得到 {len(parts)} 段, 降级处理"
                        )
                        final_results.append(f"<r0>{trans}</r0>")
                else:
                    final_results.append(trans)

            return json.dumps({
                "translations": final_results,
                "count": len(final_results),
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"翻译失败: {e}")
            return json.dumps(
                {"error": f"翻译失败: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
