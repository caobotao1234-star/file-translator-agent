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
# 📘 教学笔记：翻译流水线（Translation Pipeline v7 — 整页翻译 + 跨页上下文）
# =============================================================
# v7 核心理念：一页一次调用，LLM 注意力机制自然覆盖页内上下文。
#
# 📘 v5/v6 的问题：
#   v5: 所有 batch 全并行，页内页间都没有上下文
#   v6: 页间串行但页内并行，页内上下文仍然丢失
#
# 📘 v7 的解决方案：
#   1. 整页翻译：一页所有段落一次性发给 LLM，一次性收回
#      - LLM 注意力机制自然关联标题、正文、表格
#      - 减少 API 调用次数，更快
#   2. 跨页上下文：前一页翻完 → 收集术语缓存 → 注入下一页 prompt
#   3. 翻译缓存：相同原文只翻译一次，后续页面直接复用
#   4. 极端情况兜底：单页段落数超过 batch_size 时串行分批
# =============================================================

logger = get_logger("translate_pipeline")

TRANSLATE_SYSTEM_PROMPT = """你是专业翻译专家。将收到的JSON数组逐段翻译，保持段落数量和顺序严格一致。

核心要求⚠️：
1. 输入N个元素，输出必须恰好N个元素，一一对应，不允许合并或拆分。
2. 每一段文字必须翻译成用户指定的目标语言。如果原文已经是目标语言，则保持原样。
3. 即使某段只有一个字（如"垃圾"）或纯数字（如"63"），也必须单独作为一个元素输出。

翻译原则：
- 忠实原文、通顺自然、术语统一、保持编号和格式。
- ⚠️ 翻译必须地道自然，读起来像母语者写的，绝不能是逐字直译的 Chinglish。
- 中文的长定语链（如"铝合金导体交联聚乙烯绝缘聚氯乙烯护套电力电缆"）翻译时必须重组句式，
  用英文自然的表达方式（如 "XLPE-insulated, PVC-sheathed power cables with aluminum alloy conductors"），
  不要按中文语序堆砌名词。
- 工业/技术文档中的术语要用行业标准英文（如 XLPE = cross-linked polyethylene, PVC = polyvinyl chloride），
  首次出现可用全称，后续用缩写。
- 宣传性语句（如企业简介、产品优势）要用商务英文的表达习惯，避免生硬的直译。
  例："大大降低了施工难度和费用" → "significantly simplifying installation and reducing costs"
  而不是 "greatly reduced the construction difficulty and expenses"

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
# 只是让翻译 Agent 做出更好的决策。
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

    返回 None 表示不限制（正文段落通常不需要限制）。
    """
    text = item.get("full_text", "")
    clean_text = text.replace("\n", "").strip()
    char_count = len(clean_text)
    item_type = item.get("type", "")

    # 📘 正文段落（>50字符）通常不需要长度限制
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
                char_width = font_size * 0.55
                line_height = font_size * 1.3
                if char_width > 0 and line_height > 0:
                    chars_per_line = int(width / char_width)
                    num_lines = max(1, int(height / line_height))
                    return chars_per_line * num_lines
            else:
                char_width = font_size * 0.55
                if char_width > 0:
                    return int(width / char_width)

    # PPT/Word: 用膨胀系数估算
    role = item.get("text_role", "body")
    if role in ("title", "subtitle"):
        factor = 3.0
    elif role in ("caption", "label"):
        factor = 2.5
    else:
        factor = 2.5

    # 只对短文本做限制
    if target_lang in ("英文", "法文", "德文", "西班牙文"):
        return int(char_count * factor)

    return None


def _build_context_hint(items: List[dict], doc_type: str) -> str:
    """
    📘 教学笔记：构建页面上下文提示

    给 LLM 一段简短的上下文描述，帮助它理解这批文字的来源。
    """
    doc_type_names = {
        "pdf_block": "PDF文档（可能是宣传册/手册）",
        "slide_text": "PPT演示文稿",
        "paragraph": "Word文档",
        "table_cell": "表格",
    }

    role_counts = {}
    pages = set()
    for item in items:
        role = item.get("text_role", "body")
        role_counts[role] = role_counts.get(role, 0) + 1
        key = item.get("key", "")
        if key.startswith("pg"):
            pages.add(key.split("_")[0])
        elif key.startswith("s"):
            pages.add(key.split("_")[0])

    type_name = doc_type_names.get(doc_type, "文档")
    role_desc = "、".join(f"{v}个{k}" for k, v in role_counts.items())
    page_desc = f"（来自 {', '.join(sorted(pages))}）" if pages else ""

    return f"[文档类型: {type_name}] [本批内容{page_desc}: {role_desc}]"


def _enrich_text_for_prompt(text: str, item: dict) -> str:
    """
    📘 教学笔记：给翻译文本附加角色和长度提示
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
    📘 翻译流水线 v7：整页一次性翻译 + 跨页上下文。
    LLM 注意力机制自然覆盖页内上下文，跨页缓存确保术语一致性。

    📘 教学笔记：max_workers 控制并行度（用于极端情况的页内分批）
    - 正常情况下每页一次调用，不需要并行
    - 仅当单页段落数超过 batch_size 时才分批串行
    """

    def __init__(
        self,
        translate_llm,
        batch_size: int = 10,
        max_workers: int = 1,
        debug: bool = False,
    ):
        self.batch_size = batch_size
        self.max_workers = max(1, max_workers)
        self.debug = debug
        self.translate_llm = translate_llm

        # 📘 教学笔记：优雅停止机制
        self._stop_event = threading.Event()

        # 📘 教学笔记：Agent 池
        # 每个worker线程需要独立的Agent实例（memory不是线程安全的）。
        # 但它们共享同一个LLM engine（HTTP client是线程安全的）。
        self.translate_agent = self._make_translate_agent()

        # 额外的worker Agent（多线程时使用）
        self._translate_pool: List[BaseAgent] = [self.translate_agent]
        for _ in range(self.max_workers - 1):
            self._translate_pool.append(self._make_translate_agent())

        # 📘 线程安全的Agent分配：用锁保护的索引
        self._translate_lock = threading.Lock()
        self._translate_idx = 0

    def _acquire_translate_agent(self) -> BaseAgent:
        """线程安全地获取一个翻译 Agent"""
        with self._translate_lock:
            agent = self._translate_pool[self._translate_idx % len(self._translate_pool)]
            self._translate_idx += 1
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
    def total_translate_tokens(self) -> int:
        """汇总所有翻译 Agent 的 token 用量"""
        return sum(a.total_tokens for a in self._translate_pool)

    def _make_translate_agent(self) -> BaseAgent:
        return BaseAgent(
            llm_engine=self.translate_llm,
            tools=[],
            config=AgentConfig(max_loops=1, debug=self.debug, show_usage=False),
            system_prompt=TRANSLATE_SYSTEM_PROMPT,
            agent_name="translator",
        )

    # 📘 教学笔记：清理 LLM 译文中泄漏的元数据标签
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

    def _translate_batch(
        self,
        texts: List[str],
        target_lang: str,
        lang_english: str,
        agent: BaseAgent = None,
        items: List[dict] = None,
        cross_page_hint: str = "",
    ) -> List[str]:
        """
        翻译一批文本，带智能重试。返回长度 == len(texts) 的结果列表。
        agent: 指定使用的Agent实例（多线程时每个线程用不同的Agent）
        items: 对应的 parsed item 列表（用于上下文感知翻译）
        cross_page_hint: 跨页翻译上下文提示（前文翻译对照表）
        """
        if not texts:
            return []

        if agent is None:
            agent = self._acquire_translate_agent()

        results = list(texts)  # 原文兜底
        pending_indices = list(range(len(texts)))

        # 📘 教学笔记：构建上下文感知的翻译输入
        has_context = items is not None and len(items) == len(texts)
        if has_context:
            enriched_texts = [_enrich_text_for_prompt(t, it) for t, it in zip(texts, items)]
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
                print(f"  [📝 翻译中] {count} 个段落...", flush=True)
            else:
                print(f"  [🔄 重试翻译] 第{attempt}次，{count} 个漏翻段落...", flush=True)

            logger.debug(f"翻译请求: {count} 段, 目标语言={target_lang}({lang_english}), attempt={attempt}")
            logger.debug(f"翻译输入摘要: {[t[:30] for t in pending_texts[:3]]}{'...' if count > 3 else ''}")

            translate_prompt = (
                f"将以下{count}个段落翻译成{target_lang}({lang_english})。"
                f"必须输出恰好{count}个元素的JSON数组，一一对应。"
                f"每一段都必须输出{target_lang}，不允许保留原文语言。\n"
            )
            # 📘 客户特殊需求注入（如有）
            user_inst = getattr(self, '_current_user_instruction', '')
            if user_inst:
                translate_prompt += f"⚠️ 客户特殊要求：{user_inst}\n"
            if context_hint:
                translate_prompt += f"{context_hint}\n"
            # 📘 跨页翻译上下文注入
            if cross_page_hint:
                translate_prompt += f"{cross_page_hint}\n"
            prompt_texts = [enriched_texts[i] for i in pending_indices] if has_context else pending_texts
            translate_prompt += f"输入：{json.dumps(prompt_texts, ensure_ascii=False)}"

            translate_results = self._call_llm_translate(agent, translate_prompt)

            if translate_results and len(translate_results) == count:
                for idx, translated in zip(pending_indices, translate_results):
                    results[idx] = translated
                logger.debug(f"翻译输出摘要: {[t[:30] for t in translate_results[:3]]}{'...' if count > 3 else ''}")
                print(f"  [✅ 翻译完成]", flush=True)
                pending_indices = []
                break

            if translate_results:
                got = len(translate_results)
                logger.warning(f"翻译结果数量不匹配: 期望 {count}，得到 {got}")
                if got > count:
                    if attempt < MAX_BATCH_RETRIES:
                        logger.info(f"返回多了 {got}>{count}，整批重试")
                    else:
                        logger.warning(f"最后一次重试仍多了，截断取前 {count} 个")
                        for j in range(count):
                            results[pending_indices[j]] = translate_results[j]
                        pending_indices = []
                else:
                    new_pending = []
                    for j in range(count):
                        if j < got:
                            results[pending_indices[j]] = translate_results[j]
                        else:
                            new_pending.append(pending_indices[j])
                    pending_indices = new_pending
                    logger.info(f"部分匹配 {got}/{count}，剩余 {len(pending_indices)} 个待重试")
            else:
                logger.warning(f"翻译完全失败（JSON 解析错误），{count} 个段落将重试")

            if not pending_indices:
                print(f"  [✅ 翻译完成（部分匹配）]", flush=True)
                break

        if pending_indices:
            logger.warning(f"翻译最终仍有 {len(pending_indices)} 个段落未翻译，使用原文兜底")
            print(f"  [⚠️ {len(pending_indices)} 个段落使用原文兜底]", flush=True)

        return results

    def translate_batch(
        self,
        texts: List[str],
        target_lang: str = "英文",
    ) -> List[str]:
        """
        翻译一批文本（纯翻译，无审校）。
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

        return self._translate_batch(texts, target_lang, lang_english)

    def translate_document(
        self,
        parsed_data: Dict[str, Any],
        target_lang: str = "英文",
        on_progress=None,
        user_instruction: str = "",
    ) -> Dict[str, str]:
        """
        翻译整个文档（v7 — 整页一次性翻译 + 跨页上下文）。

        📘 教学笔记：整页翻译 + 跨页上下文
        v6 按 batch 切分，同一页内的段落被拆到不同 batch，互相看不到。
        v7 直接把一整页所有段落一次性发给 LLM：
        - LLM 的注意力机制自然覆盖页内所有段落（标题、正文、表格互相关联）
        - 一次发、一次收，减少 API 调用次数，更快
        - 页间串行：前一页翻完 → 收集术语缓存 → 注入下一页 prompt

        📘 如果单页段落数超过 batch_size（极端情况），才拆分为多次调用。
        """
        # 📘 教学笔记：上下文感知预处理
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
        item_map: Dict[str, dict] = {}
        for item in parsed_data["items"]:
            if item.get("is_empty"):
                continue
            if item.get("full_text"):
                to_translate.append((item["key"], item["full_text"]))
                item_map[item["key"]] = item

        total = len(to_translate)
        logger.info(f"开始翻译文档: {total} 个翻译单元, batch_size={self.batch_size}")

        # 📘 教学笔记：客户特殊需求暂存（供 _translate_batch 使用）
        self._current_user_instruction = user_instruction

        lang_hint = {
            "英文": "English", "中文": "Chinese", "日文": "Japanese",
            "韩文": "Korean", "法文": "French", "德文": "German",
            "西班牙文": "Spanish", "俄文": "Russian",
        }
        lang_english = lang_hint.get(target_lang, target_lang)

        # ============================================================
        # 📘 教学笔记：按页分组
        # key 格式: "pg0_e1_r0_c0"（PDF）、"s0_sp0_p0"（PPT）、"p0"（Word）
        # 从 key 中提取页码前缀，按页分组，保持页内顺序。
        # ============================================================
        from collections import OrderedDict
        page_groups: OrderedDict[str, List[Tuple[str, str]]] = OrderedDict()
        for key, text in to_translate:
            page_prefix = key.split("_")[0] if "_" in key else "default"
            if page_prefix not in page_groups:
                page_groups[page_prefix] = []
            page_groups[page_prefix].append((key, text))

        num_pages = len(page_groups)
        translations = {}
        stopped_early = False

        # 📘 教学笔记：跨页翻译缓存 + 上下文
        # translation_cache: {原文: 译文} — 相同原文只翻译一次
        # cross_page_pairs: 最近的翻译对照（注入到 prompt 中）
        # cross_page_summary: 前文内容摘要（让 LLM 理解文档主题和语境）
        translation_cache: Dict[str, str] = {}
        cross_page_pairs: List[Tuple[str, str]] = []
        cross_page_summary: str = ""

        print(
            f"  [🚀 翻译] {total} 个段落, {num_pages} 页（整页翻译+跨页上下文）",
            flush=True,
        )
        completed_count = 0

        for page_num, (page_prefix, page_items_raw) in enumerate(page_groups.items()):
            if self._stop_event.is_set():
                stopped_early = True
                break

            # 📘 缓存命中 — 相同原文直接复用
            page_keys_to_translate = []
            page_texts_to_translate = []
            page_items_to_translate = []
            cache_hit_count = 0

            for key, text in page_items_raw:
                stripped = text.strip()
                if stripped in translation_cache:
                    translations[key] = translation_cache[stripped]
                    cache_hit_count += 1
                    completed_count += 1
                else:
                    page_keys_to_translate.append(key)
                    page_texts_to_translate.append(text)
                    page_items_to_translate.append(item_map.get(key))

            if cache_hit_count > 0:
                logger.info(
                    f"{page_prefix}: 缓存命中 {cache_hit_count} 个, "
                    f"需翻译 {len(page_texts_to_translate)} 个"
                )

            if not page_texts_to_translate:
                if on_progress:
                    on_progress(completed_count, total)
                continue

            # 📘 构建跨页上下文提示（内容摘要 + 术语对照）
            cross_page_hint = ""
            if cross_page_summary or cross_page_pairs:
                parts = []
                if cross_page_summary:
                    parts.append(f"[前文内容摘要: {cross_page_summary}]")
                if cross_page_pairs:
                    recent = cross_page_pairs[-30:]
                    pairs_str = "; ".join(f"{src}={tgt}" for src, tgt in recent)
                    parts.append(f"[前文术语参考（相同文字必须使用相同译法）: {pairs_str}]")
                cross_page_hint = "\n".join(parts)

            # 📘 教学笔记：整页一次性翻译
            # 把本页所有段落一次性发给 LLM，LLM 注意力机制自然覆盖页内上下文。
            # 只有当段落数超过 batch_size 时才拆分（极端情况，如超长表格页）。
            page_count = len(page_texts_to_translate)
            if page_count <= self.batch_size:
                # 📘 常规情况：整页一次调用
                agent = self._acquire_translate_agent()
                results = self._translate_batch(
                    page_texts_to_translate, target_lang, lang_english,
                    agent, page_items_to_translate, cross_page_hint,
                )
                for j, key in enumerate(page_keys_to_translate):
                    trans = results[j] if j < len(results) else page_texts_to_translate[j]
                    translations[key] = trans
                    orig = page_texts_to_translate[j].strip()
                    if orig and trans != orig:
                        translation_cache[orig] = trans
            else:
                # 📘 极端情况：单页段落数超过 batch_size，串行分批
                # 每批翻完后把结果加入缓存，下一批能看到前面的翻译
                logger.info(f"{page_prefix}: {page_count} 个段落超过 batch_size={self.batch_size}，分批串行")
                for i in range(0, page_count, self.batch_size):
                    if self._stop_event.is_set():
                        stopped_early = True
                        break
                    b_keys = page_keys_to_translate[i:i + self.batch_size]
                    b_texts = page_texts_to_translate[i:i + self.batch_size]
                    b_items = page_items_to_translate[i:i + self.batch_size]

                    # 📘 页内分批时，也把前面批次的翻译加入上下文
                    intra_hint = cross_page_hint
                    if i > 0:
                        intra_pairs = []
                        for prev_j in range(i):
                            prev_key = page_keys_to_translate[prev_j]
                            prev_text = page_texts_to_translate[prev_j].strip()
                            prev_trans = translations.get(prev_key, "")
                            if prev_text and prev_trans and prev_text != prev_trans and len(prev_text) < 60:
                                intra_pairs.append(f"{prev_text}={prev_trans}")
                        if intra_pairs:
                            intra_str = "; ".join(intra_pairs[-20:])
                            intra_hint = (cross_page_hint + " " if cross_page_hint else "") + \
                                f"[本页前文参考: {intra_str}]"

                    agent = self._acquire_translate_agent()
                    results = self._translate_batch(
                        b_texts, target_lang, lang_english,
                        agent, b_items, intra_hint,
                    )
                    for j, key in enumerate(b_keys):
                        trans = results[j] if j < len(results) else b_texts[j]
                        translations[key] = trans
                        orig = b_texts[j].strip()
                        if orig and trans != orig:
                            translation_cache[orig] = trans

            completed_count += len(page_keys_to_translate)
            if on_progress:
                on_progress(completed_count, total)

            # 📘 收集本页内容摘要 + 翻译对照，供下一页使用
            # 内容摘要：取本页前几段原文拼成简短描述，让 LLM 理解文档在讲什么
            page_originals = []
            for key, text in page_items_raw:
                stripped = text.strip()
                if stripped and len(stripped) > 3:
                    page_originals.append(stripped)
            if page_originals:
                # 📘 取本页前 5 段（每段截断 40 字符），拼成摘要
                snippet_parts = []
                for t in page_originals[:5]:
                    snippet_parts.append(t[:40] + ("..." if len(t) > 40 else ""))
                page_snippet = " / ".join(snippet_parts)
                # 📘 滚动摘要：保留最近 3 页的内容，避免 prompt 太长
                if cross_page_summary:
                    # 截断旧摘要，只保留最近 200 字符
                    old = cross_page_summary[-200:] if len(cross_page_summary) > 200 else cross_page_summary
                    cross_page_summary = f"{old} | {page_prefix}: {page_snippet}"
                else:
                    cross_page_summary = f"{page_prefix}: {page_snippet}"

            # 📘 术语对照（短文本）
            for key, text in page_items_raw:
                stripped = text.strip()
                trans = translations.get(key, "")
                if stripped and trans and stripped != trans and len(stripped) < 60:
                    if not any(src == stripped for src, _ in cross_page_pairs):
                        cross_page_pairs.append((stripped, trans))

            if stopped_early:
                break

        if stopped_early:
            logger.info(f"文档翻译被中断: 已完成 {len(translations)}/{total} 个翻译单元")
        else:
            cache_total = len(translation_cache)
            logger.info(
                f"文档翻译完成: {total} 个翻译单元, "
                f"跨页缓存 {cache_total} 个术语"
            )
        return translations
