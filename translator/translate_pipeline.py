# translator/translate_pipeline.py
import json
from typing import Dict, Any, List, Optional
from core.agent import BaseAgent
from core.llm_engine import ArkLLMEngine
from core.agent_config import AgentConfig
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：翻译流水线（Translation Pipeline）
# =============================================================
# 高质量翻译不是一步到位的，专业翻译公司的流程是：
#
#   初翻（Translation）→ 审校（Review）→ 定稿（Finalization）
#
# 我们用两个 Agent 模拟这个流程：
#   1. DraftAgent（初翻）：快速翻译，追求"信"和"达"
#   2. ReviewAgent（审校）：对照原文检查译文，追求"雅"
#
# 为什么不一步到位？
#   - 单次翻译容易出现术语不一致、漏译、语序生硬等问题
#   - 审校 Agent 看到的是"原文+译文"的对照，能发现初翻的盲点
#   - 两个 Agent 可以用不同的模型（初翻用快的，审校用好的）
#
# 分段翻译策略：
#   - 逐段翻译，保持段落对应关系
#   - 每次给 LLM 发送一批段落（batch），而不是一段一段发
#   - 批量翻译能让 LLM 理解上下文，翻译质量更高
#   - 同时减少 API 调用次数，降低成本
# =============================================================

logger = get_logger("translate_pipeline")

DRAFT_SYSTEM_PROMPT = """你是专业翻译专家。将收到的JSON数组逐段翻译，保持段落数量和顺序一致。

翻译原则：忠实原文、通顺自然、术语统一、保持编号和格式。

格式标记规则⚠️：部分段落含<r0>...</r0><r1>...</r1>标记，代表不同格式片段。
必须：保留所有标记，数量编号与原文一致，只翻译标记内文字。
例："<r0>关键：</r0><r1>说明文字</r1>" → "<r0>Key: </r0><r1>Description text</r1>"

输出：严格JSON数组，每个元素是对应译文字符串。不要输出其他内容。"""

REVIEW_SYSTEM_PROMPT = """你是翻译审校专家。收到编号对照的初翻译文，逐条审校并输出修正后的最终版本。

审校原则：检查漏译误译、提升流畅度、统一术语、修正语法。
格式标记⚠️：保留所有<rN>标记，数量编号不变，只改标记内译文。

输出：严格JSON数组，每个元素是审校后的译文字符串。初翻已好则原样输出。"""


class TranslatePipeline:
    """
    翻译流水线：初翻 → 审校，输出高质量译文。
    """

    def __init__(
        self,
        draft_llm: ArkLLMEngine,
        review_llm: ArkLLMEngine = None,
        batch_size: int = 10,
        debug: bool = False,
    ):
        """
        参数：
            draft_llm: 初翻用的 LLM 引擎
            review_llm: 审校用的 LLM 引擎（为 None 则跳过审校）
            batch_size: 每批翻译的段落数
            debug: 是否打印调试信息
        """
        self.batch_size = batch_size
        self.debug = debug

        # 初翻 Agent
        self.draft_agent = BaseAgent(
            llm_engine=draft_llm,
            tools=[],
            config=AgentConfig(max_loops=1, debug=debug, show_usage=False),
            system_prompt=DRAFT_SYSTEM_PROMPT,
            agent_name="draft_translator",
        )

        # 审校 Agent（可选）
        self.review_agent = None
        if review_llm:
            self.review_agent = BaseAgent(
                llm_engine=review_llm,
                tools=[],
                config=AgentConfig(max_loops=1, debug=debug, show_usage=False),
                system_prompt=REVIEW_SYSTEM_PROMPT,
                agent_name="reviewer",
            )

    def _parse_json_response(self, response: str) -> Optional[List[str]]:
        """从 LLM 响应中提取 JSON 数组"""
        # LLM 有时会在 JSON 前后加 markdown 代码块
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            result = json.loads(text)
            if isinstance(result, list):
                # 📘 教学笔记：LLM 返回格式防御
                # 有时 LLM 不听话，返回的不是纯字符串数组，而是对象数组，如：
                #   [{"原文": "你好", "译文": "Hello"}, ...]
                # 我们需要把这种情况也兜住，提取出译文字符串。
                cleaned = []
                for item in result:
                    if isinstance(item, str):
                        cleaned.append(item)
                    elif isinstance(item, dict):
                        # 尝试从常见的 key 中提取译文
                        for key in ("译文", "翻译", "translation", "translated",
                                    "审校", "result", "text", "初翻"):
                            if key in item:
                                cleaned.append(str(item[key]))
                                break
                        else:
                            # 实在找不到，取第一个值
                            vals = list(item.values())
                            cleaned.append(str(vals[-1]) if vals else "")
                    else:
                        cleaned.append(str(item))
                return cleaned
        except json.JSONDecodeError:
            logger.warning(f"JSON 解析失败，原始响应: {text[:200]}...")
        return None

    def translate_batch(
        self,
        texts: List[str],
        target_lang: str = "英文",
    ) -> List[str]:
        """
        翻译一批文本（初翻 + 审校）。

        参数：
            texts: 待翻译的文本列表
            target_lang: 目标语言

        返回：翻译后的文本列表（与输入一一对应）
        """
        if not texts:
            return []

        # ---- 第一步：初翻 ----
        draft_prompt = (
            f"请将以下段落翻译成{target_lang}。\n"
            f"输入：{json.dumps(texts, ensure_ascii=False)}"
        )

        print(f"  [📝 初翻中] {len(texts)} 个段落...", flush=True)
        logger.debug(f"初翻请求: {len(texts)} 段")
        draft_response = self.draft_agent.run(draft_prompt)
        draft_results = self._parse_json_response(draft_response)

        if not draft_results or len(draft_results) != len(texts):
            logger.warning(
                f"初翻结果数量不匹配: 期望 {len(texts)}，"
                f"得到 {len(draft_results) if draft_results else 0}，使用原文兜底"
            )
            # 兜底：如果解析失败，返回原文
            return texts if not draft_results else draft_results

        print(f"  [✅ 初翻完成]", flush=True)

        # ---- 第二步：审校（如果有审校 Agent）----
        if self.review_agent is None:
            logger.debug("跳过审校（未配置审校 Agent）")
            return draft_results

        # 📘 教学笔记：审校 prompt 优化
        # 旧方案：发送 [{"原文": "...", "初翻": "..."}] → 原文被重复发送，token 翻倍
        # 新方案：只发初翻结果，审校 Agent 只需要润色译文即可
        # 如果有格式标记的段落，附带原文供对照（因为标记需要校验）
        review_prompt = (
            f"审校以下→{target_lang}的初翻译文，输出修正后的JSON数组。\n"
            f"译文：{json.dumps(draft_results, ensure_ascii=False)}"
        )

        print(f"  [🔍 审校中] 对照原文检查译文质量...", flush=True)
        logger.debug(f"审校请求: {len(texts)} 段")
        review_response = self.review_agent.run(review_prompt)
        review_results = self._parse_json_response(review_response)

        if not review_results or len(review_results) != len(texts):
            logger.warning("审校结果解析失败，使用初翻结果")
            print(f"  [⚠️ 审校失败] 使用初翻结果", flush=True)
            return draft_results

        print(f"  [✅ 审校完成]", flush=True)
        return review_results

    def translate_document(
        self,
        parsed_data: Dict[str, Any],
        target_lang: str = "英文",
        on_progress=None,
    ) -> Dict[str, str]:
        """
        翻译整个文档（段落 + 表格单元格）。

        参数：
            parsed_data: 解析器返回值
            target_lang: 目标语言
            on_progress: 进度回调 fn(completed, total)

        返回：{key: 翻译后文本} 字典
        """
        # 收集需要翻译的项目
        to_translate = []
        for item in parsed_data["items"]:
            if item.get("is_empty"):
                continue
            if item.get("full_text"):
                to_translate.append((item["key"], item["full_text"]))

        total = len(to_translate)
        logger.info(f"开始翻译文档: {total} 个翻译单元，batch_size={self.batch_size}")

        translations = {}
        completed = 0

        # 分批翻译
        for i in range(0, total, self.batch_size):
            batch = to_translate[i:i + self.batch_size]
            batch_keys = [key for key, _ in batch]
            batch_texts = [text for _, text in batch]

            logger.info(f"翻译进度: {completed}/{total}")

            results = self.translate_batch(batch_texts, target_lang)

            for key, translated in zip(batch_keys, results):
                translations[key] = translated

            completed += len(batch)
            if on_progress:
                on_progress(completed, total)

        logger.info(f"文档翻译完成: {total} 个翻译单元")
        return translations
