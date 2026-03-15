# translator/translate_pipeline.py
import json
import threading
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from typing import Dict, Any, List, Optional, Tuple
from core.agent import BaseAgent
from core.llm_engine import ArkLLMEngine
from core.agent_config import AgentConfig
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：翻译流水线（Translation Pipeline v4 — 多线程并行）
# =============================================================
# v1~v2: 串行 → B1初翻→B1审校→B2初翻→B2审校
# v3: 流水线 → [B1审校∥B2初翻]，2路并行
# v4: 全并行 → N个线程同时跑，分两阶段：
#   阶段1: 所有batch的初翻并行（N路同时发LLM请求）
#   阶段2: 所有batch的审校并行（N路同时发LLM请求）
#
# 📘 为什么能多线程并行调LLM？
# LLM调用本质是HTTP请求（I/O密集型），Python的GIL只限制CPU密集型并行。
# 5个线程同时发5个HTTP请求，服务端会并行处理，完全没问题。
#
# 📘 为什么每个线程需要独立的Agent？
# _call_llm_translate会操作agent.memory（清空消息），
# 多线程共享同一个Agent会互相覆盖memory。
# 所以每个worker线程用自己的Agent实例，但共享同一个LLM engine
# （HTTP client是线程安全的）。
#
# 📘 为什么分两阶段而不是初翻完一个立刻审校？
# 分阶段更简单、更可控：
#   - 阶段1全部完成后，所有初翻结果都在内存里
#   - 阶段2可以安全地读取初翻结果，不需要复杂的依赖管理
#   - 进度报告更清晰（初翻50%→100%→审校50%→100%）
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
    """
    翻译流水线：初翻 → 审校，多线程并行。

    📘 教学笔记：max_workers 控制并行度
    - max_workers=1: 串行（兼容模式，适合调试）
    - max_workers=2: 等同于 v3 流水线（初翻+审校各1线程）
    - max_workers=3~5: 推荐值，多batch并行，速度提升明显
    - max_workers>5: 可能触发API限流，视服务端配额而定
    """

    def __init__(
        self,
        draft_llm: ArkLLMEngine,
        review_llm: ArkLLMEngine = None,
        batch_size: int = 10,
        max_workers: int = 1,
        debug: bool = False,
    ):
        self.batch_size = batch_size
        self.max_workers = max(1, max_workers)
        self.debug = debug
        self.draft_llm = draft_llm
        self.review_llm = review_llm

        # 📘 教学笔记：优雅停止机制
        self._stop_event = threading.Event()

        # 📘 教学笔记：Agent 池
        # 每个worker线程需要独立的Agent实例（memory不是线程安全的）。
        # 但它们共享同一个LLM engine（HTTP client是线程安全的）。
        # draft_agent / review_agent 保留为"主Agent"，用于token统计汇总。
        self.draft_agent = self._make_draft_agent()
        self.review_agent = self._make_review_agent()

        # 额外的worker Agent（多线程时使用）
        self._draft_pool: List[BaseAgent] = [self.draft_agent]
        self._review_pool: List[BaseAgent] = [self.review_agent] if self.review_agent else []
        for _ in range(self.max_workers - 1):
            self._draft_pool.append(self._make_draft_agent())
            if self.review_llm:
                self._review_pool.append(self._make_review_agent())

        # 📘 线程安全的Agent分配：用锁保护的索引
        self._draft_lock = threading.Lock()
        self._draft_idx = 0
        self._review_lock = threading.Lock()
        self._review_idx = 0

    def _acquire_draft_agent(self) -> BaseAgent:
        """线程安全地获取一个draft Agent"""
        with self._draft_lock:
            agent = self._draft_pool[self._draft_idx % len(self._draft_pool)]
            self._draft_idx += 1
            return agent

    def _acquire_review_agent(self) -> Optional[BaseAgent]:
        """线程安全地获取一个review Agent"""
        if not self._review_pool:
            return None
        with self._review_lock:
            agent = self._review_pool[self._review_idx % len(self._review_pool)]
            self._review_idx += 1
            return agent

    def request_stop(self):
        """请求停止翻译（线程安全，可从任意线程调用）"""
        self._stop_event.set()
        logger.info("收到停止请求，将在当前批次完成后停止")

    def reset_stop(self):
        """重置停止标志（下次翻译前调用）"""
        self._stop_event.clear()

    @property
    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    @property
    def total_draft_tokens(self) -> int:
        """汇总所有draft Agent的token用量"""
        return sum(a.total_tokens for a in self._draft_pool)

    @property
    def total_review_tokens(self) -> int:
        """汇总所有review Agent的token用量"""
        return sum(a.total_tokens for a in self._review_pool)

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
        agent: BaseAgent = None,
    ) -> List[str]:
        """
        初翻一批文本，带智能重试。返回长度 == len(texts) 的结果列表。
        agent: 指定使用的Agent实例（多线程时每个线程用不同的Agent）
        """
        if not texts:
            return []

        if agent is None:
            agent = self._acquire_draft_agent()

        results = list(texts)  # 原文兜底
        pending_indices = list(range(len(texts)))

        for attempt in range(1 + MAX_BATCH_RETRIES):
            if self._stop_event.is_set():
                break

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

            draft_results = self._call_llm_translate(agent, draft_prompt)

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
        agent: BaseAgent = None,
    ) -> List[str]:
        """
        审校一批文本。返回审校后的结果，失败则返回初翻结果。

        📘 教学笔记：审校 token 优化
        原文只发前 50 字符作为"锚点"，足够审校 agent
        判断译文是否对齐、是否漏译，但 token 消耗大幅降低。
        """
        if agent is None:
            agent = self._acquire_review_agent()
        if agent is None:
            return draft_results

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
        review_results = self._call_llm_translate(agent, review_prompt)

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
        翻译整个文档（v4 多线程并行版）。

        📘 教学笔记：两阶段多线程并行

        阶段1 — 初翻（N路并行）：
          线程1: B1初翻    线程2: B2初翻    线程3: B3初翻 ...
          所有batch同时发LLM请求，服务端并行处理。

        阶段2 — 审校（N路并行）：
          线程1: B1审校    线程2: B2审校    线程3: B3审校 ...

        总时间 ≈ ceil(batch数/N) × (单batch初翻 + 单batch审校)
        对比v3: N × 单batch初翻 + 单batch审校
        当N=5、4个batch时: v4≈1轮初翻+1轮审校, v3≈4轮初翻+1轮审校
        """
        to_translate = []
        for item in parsed_data["items"]:
            if item.get("is_empty"):
                continue
            if item.get("full_text"):
                to_translate.append((item["key"], item["full_text"]))

        total = len(to_translate)
        workers = self.max_workers
        logger.info(f"开始翻译文档: {total} 个翻译单元, batch_size={self.batch_size}, workers={workers}")

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

        num_batches = len(batches)
        translations = {}
        draft_results_map: Dict[int, List[str]] = {}  # batch_idx -> draft results
        stopped_early = False

        # ============================================================
        # 阶段1：初翻（多线程并行）
        # ============================================================
        print(f"  [🚀 阶段1] 初翻 {num_batches} 个批次（{workers} 线程并行）", flush=True)
        completed_draft = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            # 提交所有初翻任务
            future_to_idx: Dict[Future, int] = {}
            for batch_idx, (batch_keys, batch_texts) in enumerate(batches):
                if self._stop_event.is_set():
                    stopped_early = True
                    break
                agent = self._acquire_draft_agent()
                future = executor.submit(
                    self._draft_batch, batch_texts, target_lang, lang_english, agent,
                )
                future_to_idx[future] = batch_idx

            # 收集初翻结果（按完成顺序）
            for future in as_completed(future_to_idx):
                if self._stop_event.is_set():
                    stopped_early = True
                    break
                batch_idx = future_to_idx[future]
                batch_keys, batch_texts = batches[batch_idx]
                try:
                    draft_results = future.result()
                    draft_results_map[batch_idx] = draft_results
                    # 先写入初翻结果作为兜底
                    for j, key in enumerate(batch_keys):
                        translations[key] = draft_results[j] if j < len(draft_results) else batch_texts[j]
                except Exception as e:
                    logger.error(f"初翻批次 {batch_idx} 异常: {e}")
                    # 异常时用原文兜底
                    for j, key in enumerate(batch_keys):
                        translations[key] = batch_texts[j]
                    draft_results_map[batch_idx] = list(batch_texts)

                completed_draft += len(batch_keys)
                if on_progress:
                    on_progress(completed_draft, total)

        if stopped_early and not translations:
            logger.info("用户在初翻阶段停止，无翻译结果")
            return translations

        # ============================================================
        # 阶段2：审校（多线程并行）
        # ============================================================
        if self.review_agent is not None and not self._stop_event.is_set():
            completed_batches = sorted(draft_results_map.keys())
            review_count = len(completed_batches)
            print(f"  [🚀 阶段2] 审校 {review_count} 个批次（{workers} 线程并行）", flush=True)

            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_idx: Dict[Future, int] = {}
                for batch_idx in completed_batches:
                    if self._stop_event.is_set():
                        stopped_early = True
                        break
                    batch_keys, batch_texts = batches[batch_idx]
                    draft_results = draft_results_map[batch_idx]
                    agent = self._acquire_review_agent()
                    future = executor.submit(
                        self._review_batch,
                        batch_texts, draft_results, target_lang, lang_english, agent,
                    )
                    future_to_idx[future] = batch_idx

                for future in as_completed(future_to_idx):
                    if self._stop_event.is_set():
                        stopped_early = True
                        break
                    batch_idx = future_to_idx[future]
                    batch_keys, batch_texts = batches[batch_idx]
                    try:
                        reviewed = future.result()
                        for j, key in enumerate(batch_keys):
                            translations[key] = reviewed[j] if j < len(reviewed) else translations.get(key, batch_texts[j])
                    except Exception as e:
                        logger.error(f"审校批次 {batch_idx} 异常: {e}")
                        # 审校失败，保留初翻结果（已在translations里）

        if stopped_early:
            logger.info(f"文档翻译被中断: 已完成 {len(translations)}/{total} 个翻译单元")
        else:
            logger.info(f"文档翻译完成: {total} 个翻译单元")
        return translations
