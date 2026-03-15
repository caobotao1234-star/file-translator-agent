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
#
# 📘 v2 改进：
#   - 每批翻译用独立对话（清空历史），避免 context 膨胀
#   - 数量不匹配时智能重试漏翻段落，而不是直接放弃
#   - 兜底逻辑修复：部分成功的译文保留，缺失的用原文补齐
# =============================================================

logger = get_logger("translate_pipeline")

DRAFT_SYSTEM_PROMPT = """你是专业翻译专家。将收到的JSON数组逐段翻译，保持段落数量和顺序严格一致。

核心要求⚠️：
1. 输入N个元素，输出必须恰好N个元素，一一对应，不允许合并或拆分。
2. 每一段文字必须翻译成用户指定的目标语言。如果原文已经是目标语言，则保持原样。
3. 即使某段只有一个字（如"垃圾"）或纯数字（如"63"），也必须单独作为一个元素输出。

翻译原则：忠实原文、通顺自然、术语统一、保持编号和格式。

格式标记规则：部分段落含<r0>...</r0><r1>...</r1>标记，代表不同格式片段。
必须：保留所有标记，数量编号与原文一致，只翻译标记内文字。
例："<r0>关键：</r0><r1>说明文字</r1>" → "<r0>Key: </r0><r1>Description text</r1>"

输出：严格JSON数组，每个元素是对应译文字符串。不要输出其他内容。"""

REVIEW_SYSTEM_PROMPT = """你是翻译审校专家。你会收到原文和初翻译文的对照列表，逐条审校并输出修正后的最终版本。

核心要求⚠️：
1. 输入N对(原文,译文)，输出必须恰好N个元素的JSON数组，一一对应，不允许合并或拆分。
2. 逐条对照原文检查译文：是否漏译、误译、错位、语义偏差。
3. 所有译文必须是用户指定的目标语言。如果发现某条译文仍然是原文语言（未翻译），你必须将其翻译为目标语言。

审校原则：对照原文检查漏译误译、纠正错位、提升流畅度、统一术语、修正语法。
格式标记：保留所有<rN>标记，数量编号不变，只改标记内译文。

输出：严格JSON数组，每个元素是审校后的译文字符串（不要输出原文）。"""


# 📘 教学笔记：最大重试次数
# 模型返回数量不匹配时，只重试漏翻的段落。
# 重试 2 次足够了，再多也不会有质的改善。
MAX_BATCH_RETRIES = 2


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
        self.batch_size = batch_size
        self.debug = debug
        self.draft_llm = draft_llm
        self.review_llm = review_llm

        # 📘 教学笔记：为什么不在 __init__ 里创建 Agent？
        # 旧版在这里创建 draft_agent/review_agent，它们的 ConversationMemory
        # 会在整个文档翻译过程中累积所有批次的对话历史。
        # 到第 5 批时，messages 已经有几千 token，模型注意力被稀释。
        # 新版改为每批创建新 Agent（无状态），保持 context 干净。
        # 但我们仍然保留 agent 引用用于 token 统计。
        self.draft_agent = self._make_draft_agent()
        self.review_agent = self._make_review_agent()

    def _make_draft_agent(self) -> BaseAgent:
        return BaseAgent(
            llm_engine=self.draft_llm,
            tools=[],
            config=AgentConfig(max_loops=1, debug=self.debug, show_usage=False),
            system_prompt=DRAFT_SYSTEM_PROMPT,
            agent_name="draft_translator",
        )

    def _make_review_agent(self) -> Optional[BaseAgent]:
        if self.review_llm is None:
            return None
        return BaseAgent(
            llm_engine=self.review_llm,
            tools=[],
            config=AgentConfig(max_loops=1, debug=self.debug, show_usage=False),
            system_prompt=REVIEW_SYSTEM_PROMPT,
            agent_name="reviewer",
        )

    def _parse_json_response(self, response: str) -> Optional[List[str]]:
        """从 LLM 响应中提取 JSON 数组"""
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            result = json.loads(text)
            if isinstance(result, list):
                cleaned = []
                for item in result:
                    if isinstance(item, str):
                        cleaned.append(item)
                    elif isinstance(item, dict):
                        for key in ("译文", "翻译", "translation", "translated",
                                    "审校", "result", "text", "初翻"):
                            if key in item:
                                cleaned.append(str(item[key]))
                                break
                        else:
                            vals = list(item.values())
                            cleaned.append(str(vals[-1]) if vals else "")
                    else:
                        cleaned.append(str(item))
                return cleaned
        except json.JSONDecodeError:
            logger.warning(f"JSON 解析失败，原始响应: {text[:200]}...")
        return None

    def _call_llm_translate(self, agent: BaseAgent, prompt: str) -> Optional[List[str]]:
        """
        📘 教学笔记：无状态 LLM 调用
        每次调用前清空 agent 的对话历史，确保每批翻译都是独立的。
        这样避免了 context 膨胀问题，也让每批的 token 消耗稳定可控。
        """
        # 清空历史，保持无状态
        agent.memory.messages.clear()
        agent.memory.memory_summary = ""

        response = agent.run(prompt)
        return self._parse_json_response(response)

    def translate_batch(
        self,
        texts: List[str],
        target_lang: str = "英文",
    ) -> List[str]:
        """
        翻译一批文本（初翻 + 审校），带智能重试。

        📘 教学笔记：重试策略
        模型返回数量不匹配时（合并/拆分了段落），不是整批重试，
        而是找出"漏翻"的段落，单独再发一次。这样：
          - 已翻译好的段落不会被浪费
          - 重试的 batch 更小，模型更容易处理
          - 最多重试 MAX_BATCH_RETRIES 次，避免死循环
        """
        if not texts:
            return []

        lang_hint = {
            "英文": "English", "中文": "Chinese", "日文": "Japanese",
            "韩文": "Korean", "法文": "French", "德文": "German",
            "西班牙文": "Spanish", "俄文": "Russian",
        }
        lang_english = lang_hint.get(target_lang, target_lang)

        # ---- 第一步：初翻 ----
        results = list(texts)  # 初始化为原文（兜底）
        pending_indices = list(range(len(texts)))  # 待翻译的索引

        for attempt in range(1 + MAX_BATCH_RETRIES):
            pending_texts = [texts[i] for i in pending_indices]
            count = len(pending_texts)

            if attempt == 0:
                print(f"  [📝 初翻中] {count} 个段落...", flush=True)
            else:
                print(f"  [🔄 重试初翻] 第{attempt}次，{count} 个漏翻段落...", flush=True)

            logger.debug(f"初翻请求: {count} 段, 目标语言={target_lang}({lang_english}), attempt={attempt}")
            logger.debug(f"初翻输入摘要: {[t[:30] for t in pending_texts[:3]]}{'...' if count > 3 else ''}")

            draft_prompt = (
                f"将以下{count}个段落翻译成{target_lang}({lang_english})。"
                f"必须输出恰好{count}个元素的JSON数组，一一对应。"
                f"每一段都必须输出{target_lang}，不允许保留原文语言。\n"
                f"输入：{json.dumps(pending_texts, ensure_ascii=False)}"
            )

            draft_results = self._call_llm_translate(self.draft_agent, draft_prompt)

            if draft_results and len(draft_results) == count:
                # 完美匹配，填入结果
                for idx, translated in zip(pending_indices, draft_results):
                    results[idx] = translated
                logger.debug(f"初翻输出摘要: {[t[:30] for t in draft_results[:3]]}{'...' if count > 3 else ''}")
                print(f"  [✅ 初翻完成]", flush=True)
                pending_indices = []  # 全部完成
                break

            # 数量不匹配 — 区分"多了"和"少了"两种情况
            if draft_results:
                got = len(draft_results)
                logger.warning(f"初翻结果数量不匹配: 期望 {count}，得到 {got}")

                if got > count:
                    # 📘 教学笔记：返回多了（模型拆分了段落）
                    # 无法判断哪些是对的，因为对应关系已经错位。
                    # 策略：如果是最后一次重试机会，截断取前 N 个（总比空着好）；
                    # 否则整批重试，给模型再一次机会。
                    if attempt < MAX_BATCH_RETRIES:
                        logger.info(f"返回多了 {got}>{count}，整批重试")
                        # pending_indices 不变，整批重试
                    else:
                        logger.warning(f"最后一次重试仍多了，截断取前 {count} 个")
                        for j in range(count):
                            results[pending_indices[j]] = draft_results[j]
                        pending_indices = []
                else:
                    # 📘 教学笔记：返回少了（模型合并了段落）
                    # 前 N 个大概率是按顺序翻译的，先填上，剩下的留给重试。
                    new_pending = []
                    for j in range(count):
                        if j < got:
                            results[pending_indices[j]] = draft_results[j]
                        else:
                            new_pending.append(pending_indices[j])
                    pending_indices = new_pending
                    logger.info(f"部分匹配 {got}/{count}，剩余 {len(pending_indices)} 个待重试")
            else:
                logger.warning(f"初翻完全失败（JSON 解析错误），{count} 个段落将重试")

            if not pending_indices:
                print(f"  [✅ 初翻完成（部分匹配）]", flush=True)
                break

        if pending_indices:
            logger.warning(f"初翻最终仍有 {len(pending_indices)} 个段落未翻译，使用原文兜底")
            print(f"  [⚠️ {len(pending_indices)} 个段落使用原文兜底]", flush=True)

        # ---- 第二步：审校 ----
        if self.review_agent is None:
            logger.debug("跳过审校（未配置审校 Agent）")
            return results

        # 📘 教学笔记：原文+译文对照审校
        # 旧方案只发译文，审校 Agent 只能做润色，发现不了错位和漏译。
        # 新方案发送 [{"原文": "...", "译文": "..."}] 的配对格式，
        # 让审校 Agent 能真正对照原文检查每一条译文是否准确。
        # token 消耗会增加，但审校质量大幅提升，值得。
        pairs = [
            {"原文": src, "译文": tgt}
            for src, tgt in zip(texts, results)
        ]
        n = len(pairs)
        review_prompt = (
            f"以下是{n}对(原文→{target_lang}({lang_english}))的翻译对照。"
            f"逐条对照原文审校译文，输出恰好{n}个元素的JSON数组（只含修正后的译文）。\n"
            f"对照表：{json.dumps(pairs, ensure_ascii=False)}"
        )

        print(f"  [🔍 审校中] 对照原文检查译文质量...", flush=True)
        logger.debug(f"审校请求: {n} 段（原文+译文对照）")
        review_results = self._call_llm_translate(self.review_agent, review_prompt)

        if review_results and len(review_results) == n:
            logger.debug(f"审校输出摘要: {[t[:30] for t in review_results[:3]]}{'...' if n > 3 else ''}")
            print(f"  [✅ 审校完成]", flush=True)
            return review_results
        else:
            logger.warning("审校结果解析失败或数量不匹配，使用初翻结果")
            print(f"  [⚠️ 审校失败] 使用初翻结果", flush=True)
            return results

    def translate_document(
        self,
        parsed_data: Dict[str, Any],
        target_lang: str = "英文",
        on_progress=None,
    ) -> Dict[str, str]:
        """
        翻译整个文档（段落 + 表格单元格）。
        """
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

        for i in range(0, total, self.batch_size):
            batch = to_translate[i:i + self.batch_size]
            batch_keys = [key for key, _ in batch]
            batch_texts = [text for _, text in batch]

            logger.info(f"翻译进度: {completed}/{total}")

            results = self.translate_batch(batch_texts, target_lang)

            # 📘 教学笔记：安全的 key-result 映射
            # 旧版用 zip(batch_keys, results)，如果 results 比 batch_keys 短，
            # 多出来的 key 就没有译文，PDF writer 找不到就留空。
            # 新版 results 长度始终等于 batch_texts（translate_batch 保证了这一点），
            # 但为了防御性编程，还是加个保护。
            for j, key in enumerate(batch_keys):
                if j < len(results):
                    translations[key] = results[j]
                else:
                    translations[key] = batch_texts[j]  # 原文兜底

            completed += len(batch)
            if on_progress:
                on_progress(completed, total)

        logger.info(f"文档翻译完成: {total} 个翻译单元")
        return translations
