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

DRAFT_SYSTEM_PROMPT = """你是一个专业的翻译专家，擅长中英文互译。

【你的任务】
你会收到一组待翻译的段落，格式为 JSON 数组。请逐段翻译，保持段落顺序和数量一致。

【翻译原则】
1. 忠实原文：准确传达原文含义，不遗漏、不添加信息
2. 通顺自然：译文要符合目标语言的表达习惯，不要翻译腔
3. 术语一致：同一个术语在全文中保持统一翻译
4. 保持格式：如果原文有编号、列表等结构，译文也要保持

【输出格式】
严格输出 JSON 数组，每个元素是对应段落的译文。不要输出任何其他内容。
示例输入：["你好世界", "这是测试"]
示例输出：["Hello World", "This is a test"]
"""

REVIEW_SYSTEM_PROMPT = """你是一个资深的翻译审校专家。

【你的任务】
你会收到"原文"和"初翻译文"的对照，请审校译文并输出修正后的最终版本。

【审校原则】
1. 准确性：检查是否有漏译、误译、添译
2. 流畅度：译文是否通顺自然，是否有翻译腔
3. 术语一致性：同一术语是否全文统一
4. 语法正确性：拼写、语法、标点是否正确

【输出格式】
严格输出 JSON 数组，每个元素是审校后的最终译文。不要输出任何其他内容。
如果初翻已经很好，直接原样输出即可，不要为了改而改。
"""


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
            # 去掉首尾的 ``` 行
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            logger.warning(f"JSON 解析失败，原始响应: {text[:200]}...")
        return None

    def translate_batch(
        self,
        texts: List[str],
        source_lang: str = "中文",
        target_lang: str = "英文",
    ) -> List[str]:
        """
        翻译一批文本（初翻 + 审校）。

        参数：
            texts: 待翻译的文本列表
            source_lang: 源语言
            target_lang: 目标语言

        返回：翻译后的文本列表（与输入一一对应）
        """
        if not texts:
            return []

        # ---- 第一步：初翻 ----
        draft_prompt = (
            f"请将以下{source_lang}段落翻译成{target_lang}。\n"
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

        # 构造审校输入：原文 + 初翻对照
        review_pairs = []
        for orig, draft in zip(texts, draft_results):
            review_pairs.append({"原文": orig, "初翻": draft})

        review_prompt = (
            f"请审校以下{source_lang}→{target_lang}的翻译。\n"
            f"输入：{json.dumps(review_pairs, ensure_ascii=False)}"
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
        source_lang: str = "中文",
        target_lang: str = "英文",
        on_progress=None,
    ) -> Dict[int, str]:
        """
        翻译整个文档。

        参数：
            parsed_data: docx_parser.parse_docx() 的返回值
            source_lang: 源语言
            target_lang: 目标语言
            on_progress: 进度回调 fn(completed, total)

        返回：{段落index: 翻译后文本} 字典
        """
        # 收集需要翻译的段落
        to_translate = []
        for para in parsed_data["paragraphs"]:
            if not para["is_empty"]:
                to_translate.append((para["index"], para["full_text"]))

        total = len(to_translate)
        logger.info(f"开始翻译文档: {total} 个段落，batch_size={self.batch_size}")

        translations = {}
        completed = 0

        # 分批翻译
        for i in range(0, total, self.batch_size):
            batch = to_translate[i:i + self.batch_size]
            batch_indices = [idx for idx, _ in batch]
            batch_texts = [text for _, text in batch]

            logger.info(f"翻译进度: {completed}/{total} "
                        f"(当前批次: 段落 {batch_indices[0]}~{batch_indices[-1]})")

            results = self.translate_batch(batch_texts, source_lang, target_lang)

            for idx, translated in zip(batch_indices, results):
                translations[idx] = translated

            completed += len(batch)
            if on_progress:
                on_progress(completed, total)

        logger.info(f"文档翻译完成: {total} 个段落")
        return translations
