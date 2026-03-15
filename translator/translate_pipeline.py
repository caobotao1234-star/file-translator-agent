# translator/translate_pipeline.py
import json
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, Any, List, Optional, Tuple
from core.agent import BaseAgent
from core.llm_engine import ArkLLMEngine
from core.agent_config import AgentConfig
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：翻译流水线（Translation Pipeline v3 — 流水线并行）
# =============================================================
# 高质量翻译流程：初翻 → 审校
#
# v1~v2 是串行的：
#   batch1 初翻 → batch1 审校 → batch2 初翻 → batch2 审校 → ...
#   审校等待时间完全浪费。
#
# v3 改为流水线并行（Pipeline Parallelism）：
#   batch1 初翻 → [batch1 审校 + batch2 初翻] → [batch2 审校 + batch3 初翻] → ...
#
# 原理：初翻和审校用不同的 Agent（不同的 LLM 引擎实例），
# 它们之间没有共享状态，可以安全地在不同线程中并行执行。
# 审校 batch N 的同时，初翻 batch N+1 已经在跑了。
#
# 效果：审校的耗时几乎被完全隐藏，总时间 ≈ 只有初翻的时间。
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

REVIEW_SYSTEM_PROMPT = """你是翻译审校专家。你会收到译文列表（附原文摘要供参考），逐条审校并输出修正后的最终版本。

核心要求⚠️：
1. 输入N条，输出必须恰好N个元素的JSON数组，一一对应，不允许合并或拆分。
2. 逐条检查译文：语法是否正确、表达是否自然、术语是否统一。
3. 参考原文摘要判断：是否漏译、误译、语义偏差。
4. 所有译文必须是用户指定的目标语言。如果发现某条未翻译，你必须翻译它。

审校原则：纠正语法错误、提升流畅度、统一术语、修正漏译误译。
格式标记：保留所有<rN>标记，数量编号不变，只改标记内译文。

输出：严格JSON数组，每个元素是审校后的译文字符串。"""

MAX_BATCH_RETRIES = 2


class TranslatePipeline:
    """翻译流水线：初翻 → 审校，流水线并行。"""

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
        """无状态 LLM 调用：清空历史 → 调用 → 解析"""
        agent.memory.messages.clear()
        agent.memory.memory_summary = ""
        response = agent.run(prompt)
        return self._parse_json_response(response)

    def _draft_batch(
        self,
        texts: List[str],
        target_lang: str,
        lang_english: str,
    ) -> List[str]:
        """
        初翻一批文本，带智能重试。返回长度 == len(texts) 的结果列表。
        """
        if not texts:
            return []

        results = list(texts)  # 原文兜底
        pending_indices = list(range(len(texts)))

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
                for idx, translated in zip(pending_indices, draft_results):
                    results[idx] = translated
                logger.debug(f"初翻输出摘要: {[t[:30] for t in draft_results[:3]]}{'...' if count > 3 else ''}")
                print(f"  [✅ 初翻完成]", flush=True)
                pending_indices = []
                break

            if draft_results:
                got = len(draft_results)
                logger.warning(f"初翻结果数量不匹配: 期望 {count}，得到 {got}")

                if got > count:
                    if attempt < MAX_BATCH_RETRIES:
                        logger.info(f"返回多了 {got}>{count}，整批重试")
                    else:
                        logger.warning(f"最后一次重试仍多了，截断取前 {count} 个")
                        for j in range(count):
                            results[pending_indices[j]] = draft_results[j]
                        pending_indices = []
                else:
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

        return results

    def _review_batch(
        self,
        texts: List[str],
        draft_results: List[str],
        target_lang: str,
        lang_english: str,
    ) -> List[str]:
        """
        审校一批文本。返回审校后的结果，失败则返回初翻结果。

        📘 教学笔记：审校 token 优化
        v2 发完整原文+译文对照，token 消耗是初翻的 2~4 倍。
        v3 优化：原文只发前 50 字符作为"锚点"，足够审校 agent
        判断译文是否对齐、是否漏译，但 token 消耗大幅降低。
        
        为什么 50 字符够用？
        - 审校的核心任务是：润色、纠错、统一术语
        - 判断"是否漏译"只需要看原文开头就知道主题
        - 判断"是否错位"只需要对比原文和译文的主题是否匹配
        - 真正需要完整原文的场景（如数字/专有名词校验）很少
        """
        if self.review_agent is None:
            return draft_results

        # 📘 压缩原文：只保留前 50 字符作为参考锚点
        SRC_ANCHOR_LEN = 50
        pairs = []
        for src, tgt in zip(texts, draft_results):
            anchor = src[:SRC_ANCHOR_LEN]
            if len(src) > SRC_ANCHOR_LEN:
                anchor += "…"
            pairs.append({"原文摘要": anchor, "译文": tgt})

        n = len(pairs)
        review_prompt = (
            f"以下是{n}条翻译结果（含原文摘要供参考）。"
            f"逐条审校译文，确保是准确的{target_lang}({lang_english})，"
            f"修正漏译、误译、语法错误、术语不一致。"
            f"输出恰好{n}个元素的JSON数组（只含修正后的译文）。\n"
            f"对照表：{json.dumps(pairs, ensure_ascii=False)}"
        )

        print(f"  [🔍 审校中] 对照原文检查译文质量...", flush=True)
        logger.debug(f"审校请求: {n} 段（原文摘要+译文对照）")
        review_results = self._call_llm_translate(self.review_agent, review_prompt)

        if review_results and len(review_results) == n:
            logger.debug(f"审校输出摘要: {[t[:30] for t in review_results[:3]]}{'...' if n > 3 else ''}")
            print(f"  [✅ 审校完成]", flush=True)
            return review_results
        else:
            logger.warning("审校结果解析失败或数量不匹配，使用初翻结果")
            print(f"  [⚠️ 审校失败] 使用初翻结果", flush=True)
            return draft_results

    def translate_batch(
        self,
        texts: List[str],
        target_lang: str = "英文",
    ) -> List[str]:
        """
        翻译一批文本（初翻 + 审校）。
        📘 保留此方法供 COM 增强等外部调用（非流水线模式）。
        """
        if not texts:
            return []

        lang_hint = {
            "英文": "English", "中文": "Chinese", "日文": "Japanese",
            "韩文": "Korean", "法文": "French", "德文": "German",
            "西班牙文": "Spanish", "俄文": "Russian",
        }
        lang_english = lang_hint.get(target_lang, target_lang)

        draft_results = self._draft_batch(texts, target_lang, lang_english)
        return self._review_batch(texts, draft_results, target_lang, lang_english)

    def translate_document(
        self,
        parsed_data: Dict[str, Any],
        target_lang: str = "英文",
        on_progress=None,
    ) -> Dict[str, str]:
        """
        翻译整个文档（流水线并行版）。

        📘 教学笔记：流水线并行（Pipeline Parallelism）
        
        串行流程（v2）：
          B1初翻 → B1审校 → B2初翻 → B2审校 → B3初翻 → B3审校
          总时间 = N × (初翻时间 + 审校时间)

        流水线并行（v3）：
          B1初翻 → [B1审校 ∥ B2初翻] → [B2审校 ∥ B3初翻] → B3审校
          总时间 ≈ N × 初翻时间 + 1 × 审校时间
          （审校时间几乎被完全隐藏，只有最后一批需要单独等审校）

        实现：用 ThreadPoolExecutor 在后台线程跑审校，
        主线程继续跑下一批初翻。审校结果通过 Future 收集。
        """
        to_translate = []
        for item in parsed_data["items"]:
            if item.get("is_empty"):
                continue
            if item.get("full_text"):
                to_translate.append((item["key"], item["full_text"]))

        total = len(to_translate)
        logger.info(f"开始翻译文档: {total} 个翻译单元，batch_size={self.batch_size}")

        lang_hint = {
            "英文": "English", "中文": "Chinese", "日文": "Japanese",
            "韩文": "Korean", "法文": "French", "德文": "German",
            "西班牙文": "Spanish", "俄文": "Russian",
        }
        lang_english = lang_hint.get(target_lang, target_lang)

        # 切分所有 batch
        batches: List[Tuple[List[str], List[str]]] = []  # (keys, texts)
        for i in range(0, total, self.batch_size):
            batch = to_translate[i:i + self.batch_size]
            batch_keys = [key for key, _ in batch]
            batch_texts = [text for _, text in batch]
            batches.append((batch_keys, batch_texts))

        translations = {}
        completed = 0

        # 📘 流水线核心：用线程池跑审校，主线程跑初翻
        # max_workers=1 因为审校只有一个 agent，不能并发多个审校
        executor = ThreadPoolExecutor(max_workers=1)
        pending_review: Optional[Tuple[Future, List[str], List[str]]] = None
        # pending_review = (future, batch_keys, batch_texts)

        def _collect_review(future: Future, keys: List[str], texts: List[str]):
            """收集审校结果并写入 translations"""
            try:
                reviewed = future.result()  # 阻塞等待审校完成
                for j, key in enumerate(keys):
                    translations[key] = reviewed[j] if j < len(reviewed) else texts[j]
            except Exception as e:
                logger.error(f"审校线程异常: {e}")
                # 审校失败，用初翻结果（已经在 translations 里了）

        for batch_idx, (batch_keys, batch_texts) in enumerate(batches):
            logger.info(f"翻译进度: {completed}/{total}")

            # ---- 初翻当前 batch ----
            draft_results = self._draft_batch(batch_texts, target_lang, lang_english)

            # 先把初翻结果写入（作为兜底，审校完成后会覆盖）
            for j, key in enumerate(batch_keys):
                translations[key] = draft_results[j] if j < len(draft_results) else batch_texts[j]

            # ---- 收集上一批的审校结果（如果有）----
            if pending_review is not None:
                prev_future, prev_keys, prev_texts = pending_review
                _collect_review(prev_future, prev_keys, prev_texts)
                pending_review = None

            # ---- 提交当前 batch 的审校到后台线程 ----
            if self.review_agent is not None:
                future = executor.submit(
                    self._review_batch,
                    batch_texts, draft_results, target_lang, lang_english,
                )
                pending_review = (future, batch_keys, batch_texts)

            completed += len(batch_texts)
            if on_progress:
                on_progress(completed, total)

        # ---- 收集最后一批的审校结果 ----
        if pending_review is not None:
            prev_future, prev_keys, prev_texts = pending_review
            _collect_review(prev_future, prev_keys, prev_texts)

        executor.shutdown(wait=False)

        logger.info(f"文档翻译完成: {total} 个翻译单元")
        return translations
