# translator/translate_pipeline.py
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from typing import Dict, Any, List, Optional, Tuple
from core.agent import BaseAgent
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

排版感知规则：
- 每段文字可能附带 <<ROLE:角色>> 和 <<LIMIT:N>> 提示，这些是给你的参考信息。
- <<ROLE:标题>> 类文字：用简洁有力的表达，但不要过度缩写，要保持可读性。
- <<ROLE:图注>> 或 <<ROLE:标签>> 类文字：适当精简，但必须保持完整可读的单词，不要用缩写。
- <<LIMIT:N>>：译文长度尽量不超过N个字符（含空格），但可读性优先。
  如果直译超长，请适当精简表达，但绝不要用不常见的缩写。
- 没有标注的段落按正常翻译处理。

⚠️ 关键：输出的译文中绝对不要包含 <<ROLE:...>>、<<LIMIT:...>> 等标记。
这些标记只是给你的提示，不是译文的一部分。也不要输出%%%等分隔符。
也不要输出 [Body]、[Label]、[Subtitle] 等任何标签。

输出：严格JSON数组，每个元素是纯译文字符串（不含任何标签或标记）。"""

REVIEW_SYSTEM_PROMPT = """你是翻译审校专家。你会收到译文列表（附原文摘要供参考），逐条审校并输出修正后的最终版本。

核心要求⚠️：
1. 输入N条，输出必须恰好N个元素的JSON数组，一一对应，不允许合并或拆分。
2. 逐条检查译文：语法是否正确、表达是否自然、术语是否统一。
3. 参考原文摘要判断：是否漏译、误译、语义偏差。
4. 所有译文必须是用户指定的目标语言。如果发现某条未翻译，你必须翻译它。

审校原则：纠正语法错误、提升流畅度、统一术语、修正漏译误译。
格式标记：保留所有<rN>标记，数量编号不变，只改标记内译文。

排版审校：
- 如果某条译文附带了字符限制提示但明显超长，请适当精简，但保持可读性。
- 标题类译文应简洁有力，但不要过度缩写成不可读的形式。
- 保持全文术语一致性（同一个专有名词在不同段落应翻译一致）。
- 不要使用不常见的缩写（如 Co. Hons, Ind Stds, ServFlow 等），要用完整可读的表达。

⚠️ 关键：输出的译文中绝对不要包含任何标签或标记（如 <<ROLE:...>>、[Body]、[Label] 等）。
也不要输出%%%等分隔符。只输出纯译文。

输出：严格JSON数组，每个元素是审校后的纯译文字符串。"""

MAX_BATCH_RETRIES = 2

# =============================================================
# 📘 教学笔记：上下文感知翻译（Context-Aware Translation）
# =============================================================
# 之前的翻译是"盲翻"——LLM 只看到一批纯文本，不知道：
#   - 这段文字是标题还是正文？
#   - 它在页面上占多大空间？译文能放得下吗？
#   - 整个文档是什么类型？（宣传册 vs 技术文档 vs 合同）
#
# 上下文感知的核心改进：
#   1. 文本角色标注：parser 给每个 item 标记 text_role
#      (title/subtitle/caption/label/body)
#   2. 长度约束：根据原文占据的空间估算译文最大字符数
#      中→英通常膨胀 1.5~2 倍，但空间不变，所以需要控制长度
#   3. 页面上下文：告诉 LLM 这批文字来自哪一页、文档类型
#
# 这些信息通过 prompt 传递给 LLM，不需要新 Agent，
# 只是让现有的初翻/审校 Agent 做出更好的决策。
# =============================================================


def _classify_text_role(item: dict) -> str:
    """
    📘 教学笔记：自动分类文本角色

    根据 item 的元数据推断它在文档中的角色：
    - title: 大字号、粗体、短文本 → 标题/口号
    - subtitle: 中等字号、较短 → 副标题
    - caption: 在图片附近的短文本 → 图注/标签
    - label: 极短文本（<10字符）→ 标签/编号
    - body: 其他 → 正文

    分类结果用于 prompt 中的角色提示，让 LLM 调整翻译风格。
    """
    text = item.get("full_text", "")
    clean_text = text.replace("\n", "").strip()
    char_count = len(clean_text)

    # 获取格式信息
    fmt = item.get("dominant_format", {})
    font_size = fmt.get("font_size", 12)
    bold = fmt.get("bold", False)

    # PPT/Word 的样式名也是线索
    style_name = ""
    if "style" in item:
        style_name = (item["style"].get("style_name") or "").lower()

    item_type = item.get("type", "")

    # 规则 1：样式名包含 title/heading → 标题
    if any(kw in style_name for kw in ("title", "heading", "标题")):
        return "title"

    # 规则 2：极短文本 → 标签（仅限纯数字、页码等）
    # 📘 v2: 阈值从 8 降到 4，避免把"公司简介"(4字)这种正常短文本标为 label
    # label 应该只用于页码、编号等极短的非语义文本
    if char_count <= 4 and not any('\u4e00' <= c <= '\u9fff' for c in clean_text):
        return "label"

    # 规则 3：PDF 大字号 + 粗体 + 短文本 → 标题
    if item_type == "pdf_block":
        if font_size >= 16 and char_count < 30:
            return "title"
        if font_size >= 14 and bold and char_count < 50:
            return "title"
        if font_size >= 12 and char_count < 20:
            return "caption"

    # 规则 4：PPT 短文本通常是标题/标签
    if item_type == "slide_text":
        if char_count < 20:
            return "caption"
        if char_count < 40 and bold:
            return "title"

    # 规则 5：短文本（10~30字符）→ 副标题/图注
    if char_count <= 30:
        return "subtitle"

    return "body"


def _estimate_max_chars(item: dict, target_lang: str) -> Optional[int]:
    """
    📘 教学笔记：估算译文最大字符数

    根据原文在页面上占据的空间，估算目标语言译文的最大字符数。
    核心逻辑：
    - PDF: 有 text_bbox，可以精确计算可用宽度 / 目标字号
    - PPT/Word: 没有精确 bbox，用原文字符数 × 膨胀系数估算

    📘 v2 修正：中→英膨胀系数大幅上调
    之前用 1.3（标题）/ 1.8（正文），导致 LLM 被迫极度缩写。
    实际上中文→英文的膨胀比约 2.5~3.5 倍：
      "公司简介" (4字) → "Company Introduction" (22字符)
      "装配式建筑" (5字) → "Prefabricated Buildings" (23字符)
    现在用 3.0（标题）/ 2.5（正文），给 LLM 足够空间写出正常译文。

    返回 None 表示不限制（正文段落通常不需要限制）。
    """
    text = item.get("full_text", "")
    clean_text = text.replace("\n", "").strip()
    char_count = len(clean_text)
    item_type = item.get("type", "")

    # 📘 正文段落（>50字符）通常不需要长度限制
    # 因为正文有足够的空间换行，不会溢出
    if char_count > 50:
        return None

    # PDF: 用 text_bbox 精确估算
    if item_type == "pdf_block":
        text_bbox = item.get("text_bbox", item.get("bbox"))
        if text_bbox:
            width = text_bbox[2] - text_bbox[0]
            height = text_bbox[3] - text_bbox[1]
            font_size = item.get("dominant_format", {}).get("font_size", 12)
            is_multiline = item.get("is_multiline", False)

            if is_multiline:
                # 多行文本：可用面积 / 每字符面积
                # 📘 英文字符宽度 ≈ 0.55 × 字号（比 0.5 宽松一点）
                char_width = font_size * 0.55
                line_height = font_size * 1.3
                if char_width > 0 and line_height > 0:
                    chars_per_line = int(width / char_width)
                    num_lines = max(1, int(height / line_height))
                    return chars_per_line * num_lines
            else:
                # 单行文本：可用宽度 / 字符宽度
                char_width = font_size * 0.55
                if char_width > 0:
                    return int(width / char_width)

    # PPT/Word: 用膨胀系数估算
    # 📘 v2: 大幅上调膨胀系数，避免过度缩写
    role = item.get("text_role", "body")
    if role in ("title", "subtitle"):
        factor = 3.0  # 标题：中文4字 → 英文12字符，合理
    elif role in ("caption", "label"):
        factor = 2.5  # 标签：稍紧凑但不至于缩写
    else:
        factor = 2.5

    # 只对短文本做限制
    if target_lang in ("英文", "法文", "德文", "西班牙文"):
        return int(char_count * factor)

    return None



def _build_context_hint(items: List[dict], doc_type: str) -> str:
    """
    📘 教学笔记：构建页面上下文提示

    给 LLM 一段简短的上下文描述，帮助它理解这批文字的来源：
    - 文档类型（PDF宣传册/PPT演示/Word文档）
    - 当前页码/幻灯片号
    - 这批文字的角色分布（几个标题、几个正文等）

    这段提示加在翻译 prompt 的开头，不影响 JSON 输出格式。
    """
    doc_type_names = {
        "pdf_block": "PDF文档（可能是宣传册/手册）",
        "slide_text": "PPT演示文稿",
        "paragraph": "Word文档",
        "table_cell": "表格",
    }

    # 统计角色分布
    role_counts = {}
    pages = set()
    for item in items:
        role = item.get("text_role", "body")
        role_counts[role] = role_counts.get(role, 0) + 1
        key = item.get("key", "")
        if key.startswith("pg"):
            # PDF: pg0_b1 → page 0
            pages.add(key.split("_")[0])
        elif key.startswith("s"):
            # PPT: s0_sh1_p0 → slide 0
            pages.add(key.split("_")[0])

    type_name = doc_type_names.get(doc_type, "文档")
    role_desc = "、".join(f"{v}个{k}" for k, v in role_counts.items())
    page_desc = f"（来自 {', '.join(sorted(pages))}）" if pages else ""

    return f"[文档类型: {type_name}] [本批内容{page_desc}: {role_desc}]"


def _enrich_text_for_prompt(text: str, item: dict) -> str:
    """
    📘 教学笔记：给翻译文本附加角色和长度提示

    在原文前面加上角色标签和长度限制，例如：
      "<<ROLE:标题>> <<LIMIT:30>> 装配式建筑全生态产业链服务商"
      "<<ROLE:正文>> 东方建科代表案例：河南省直青年人才公寓..."

    📘 v2: 改用 <<ROLE:...>> 格式代替 [角色] 格式
    之前用 [标题/口号] 这种方括号格式，LLM 容易把它当成内容的一部分
    翻译成 [Body]、[Label] 等混入输出。
    改用 <<...>> 格式更明确是元数据，LLM 不太会保留在输出中。
    """
    role = item.get("text_role", "body")
    max_chars = item.get("max_chars")

    role_labels = {
        "title": "标题",
        "subtitle": "副标题",
        "caption": "图注",
        "label": "标签",
        "body": "正文",
    }
    label = role_labels.get(role, "正文")

    prefix = f"<<ROLE:{label}>>"
    if max_chars and role != "body":
        prefix += f" <<LIMIT:{max_chars}>>"

    return f"{prefix} {text}"


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
        draft_llm,
        review_llm=None,
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

    # 📘 教学笔记：清理 LLM 译文中泄漏的元数据标签
    # prompt 中给 LLM 的角色提示（如 <<ROLE:标题>>）和长度限制（<<LIMIT:30>>）
    # 有时会被 LLM 保留在输出中。旧格式 [Body]、[Label] 也要兼容清理。
    _ROLE_TAG_RE = re.compile(
        r'<<(?:ROLE|LIMIT):[^>]*>>\s*|'
        r'\[(?:Body|Label|Subtitle|Caption|Title|'
        r'标题/?口号?|副标题|图注/?标签?|标签|正文|页码|'
        r'限\d+字符)\]\s*',
        re.IGNORECASE,
    )
    _SEPARATOR_RE = re.compile(r'%%%+')

    def _clean_translation(self, text: str) -> str:
        """清理译文中泄漏的角色标签和分隔符"""
        text = self._ROLE_TAG_RE.sub("", text)
        text = self._SEPARATOR_RE.sub("", text)
        return text.strip()

    def _parse_json_response(self, response: str) -> Optional[List[str]]:
        """从 LLM 响应中提取 JSON 数组，并清理泄漏的标签"""
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
                        cleaned.append(self._clean_translation(item))
                    elif isinstance(item, dict):
                        for key in ("译文", "翻译", "translation", "translated",
                                    "审校", "result", "text", "初翻"):
                            if key in item:
                                cleaned.append(self._clean_translation(str(item[key])))
                                break
                        else:
                            vals = list(item.values())
                            cleaned.append(self._clean_translation(str(vals[-1])) if vals else "")
                    else:
                        cleaned.append(self._clean_translation(str(item)))
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
        items: List[dict] = None,
    ) -> List[str]:
        """
        初翻一批文本，带智能重试。返回长度 == len(texts) 的结果列表。
        agent: 指定使用的Agent实例（多线程时每个线程用不同的Agent）
        items: 对应的 parsed item 列表（用于上下文感知翻译）
        """
        if not texts:
            return []

        if agent is None:
            agent = self._acquire_draft_agent()

        results = list(texts)  # 原文兜底
        pending_indices = list(range(len(texts)))

        # 📘 教学笔记：构建上下文感知的翻译输入
        # 如果有 items 元数据，给每段文本附加角色和长度提示。
        # LLM 看到的输入从 "装配式建筑" 变成 "[标题][限30字符] 装配式建筑"，
        # 这样它就知道要用简洁的翻译风格，并控制长度。
        has_context = items is not None and len(items) == len(texts)
        if has_context:
            enriched_texts = [_enrich_text_for_prompt(t, it) for t, it in zip(texts, items)]
            # 构建页面上下文提示
            doc_type = items[0].get("type", "paragraph") if items else "paragraph"
            context_hint = _build_context_hint(items, doc_type)
        else:
            enriched_texts = texts
            context_hint = ""

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
            )
            if context_hint:
                draft_prompt += f"{context_hint}\n"
            # 📘 用 enriched_texts（带角色/长度提示）而不是原始 texts
            prompt_texts = [enriched_texts[i] for i in pending_indices] if has_context else pending_texts
            draft_prompt += f"输入：{json.dumps(prompt_texts, ensure_ascii=False)}"

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
        items: List[dict] = None,
    ) -> List[str]:
        """
        审校一批文本。返回审校后的结果，失败则返回初翻结果。

        📘 教学笔记：审校 token 优化 + 上下文感知
        原文只发前 50 字符作为"锚点"，足够审校 agent
        判断译文是否对齐、是否漏译，但 token 消耗大幅降低。
        新增：附带角色和长度提示，让审校也能控制译文长度。
        """
        if agent is None:
            agent = self._acquire_review_agent()
        if agent is None:
            return draft_results

        SRC_ANCHOR_LEN = 50
        has_context = items is not None and len(items) == len(texts)
        pairs = []
        for idx, (src, tgt) in enumerate(zip(texts, draft_results)):
            anchor = src[:SRC_ANCHOR_LEN]
            if len(src) > SRC_ANCHOR_LEN:
                anchor += "…"
            pair = {"原文摘要": anchor, "译文": tgt}
            # 📘 附加角色和长度提示给审校
            if has_context:
                item = items[idx]
                role = item.get("text_role", "body")
                max_chars = item.get("max_chars")
                if role != "body":
                    pair["角色"] = role
                if max_chars and role != "body":
                    pair["限字符"] = max_chars
            pairs.append(pair)

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
        # 📘 教学笔记：上下文感知预处理
        # 在翻译前，给每个 item 标注文本角色和长度约束。
        # 这些元数据会被 _enrich_text_for_prompt 用来构建增强 prompt。
        for item in parsed_data["items"]:
            if item.get("is_empty"):
                continue
            if not item.get("text_role"):
                item["text_role"] = _classify_text_role(item)
            if item.get("max_chars") is None:
                item["max_chars"] = _estimate_max_chars(item, target_lang)

        # 📘 TRACE 日志：显示角色分类结果
        role_stats = {}
        for item in parsed_data["items"]:
            if item.get("is_empty"):
                continue
            role = item.get("text_role", "body")
            role_stats[role] = role_stats.get(role, 0) + 1
        if role_stats:
            logger.log(5, f"文本角色分类: {role_stats}")  # TRACE level = 5

        to_translate = []
        # 📘 保存 item 引用，后续构建 prompt 时需要元数据
        item_map: Dict[str, dict] = {}
        for item in parsed_data["items"]:
            if item.get("is_empty"):
                continue
            if item.get("full_text"):
                to_translate.append((item["key"], item["full_text"]))
                item_map[item["key"]] = item

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
        batches: List[Tuple[List[str], List[str], List[dict]]] = []  # (keys, texts, items)
        for i in range(0, total, self.batch_size):
            batch = to_translate[i:i + self.batch_size]
            batch_keys = [key for key, _ in batch]
            batch_texts = [text for _, text in batch]
            batch_items = [item_map[key] for key in batch_keys]
            batches.append((batch_keys, batch_texts, batch_items))

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
            for batch_idx, (batch_keys, batch_texts, batch_items) in enumerate(batches):
                if self._stop_event.is_set():
                    stopped_early = True
                    break
                agent = self._acquire_draft_agent()
                future = executor.submit(
                    self._draft_batch, batch_texts, target_lang, lang_english, agent,
                    batch_items,
                )
                future_to_idx[future] = batch_idx

            # 收集初翻结果（按完成顺序）
            for future in as_completed(future_to_idx):
                if self._stop_event.is_set():
                    stopped_early = True
                    break
                batch_idx = future_to_idx[future]
                batch_keys, batch_texts, batch_items = batches[batch_idx]
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
                    batch_keys, batch_texts, batch_items = batches[batch_idx]
                    draft_results = draft_results_map[batch_idx]
                    agent = self._acquire_review_agent()
                    future = executor.submit(
                        self._review_batch,
                        batch_texts, draft_results, target_lang, lang_english, agent,
                        batch_items,
                    )
                    future_to_idx[future] = batch_idx

                for future in as_completed(future_to_idx):
                    if self._stop_event.is_set():
                        stopped_early = True
                        break
                    batch_idx = future_to_idx[future]
                    batch_keys, batch_texts, batch_items = batches[batch_idx]
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
