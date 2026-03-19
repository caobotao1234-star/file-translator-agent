# translator/scan_agent.py
# =============================================================
# 📘 教学笔记：扫描件翻译 Agent（ScanAgent）
# =============================================================
# 这是整个 Agent 架构的核心——真正的"Agent"。
#
# 📘 与 v7.1 固定流水线的区别：
#   v7.1: 每页都走 CV → OCR → Vision LLM，不管文档是什么类型
#   Agent: 大脑看到图片后自己决定该调什么工具、按什么顺序
#
# 📘 ReAct 循环（Reasoning + Acting）：
#   1. 观察（Observe）：Agent 大脑看到页面图片 + 工具结果
#   2. 思考（Think）：决定下一步该做什么
#   3. 行动（Act）：调用工具（OCR/CV/翻译）
#   4. 回到 1，直到大脑认为处理完成（返回最终 JSON）
#
# 📘 自我审查（Self-Review）：
#   处理完成后，Agent 大脑检查输出质量：
#   - 文字提取完整吗？有没有遗漏？
#   - 翻译覆盖率够吗？
#   - 结构正确吗？
#   未通过则重新调用工具补充，最多重试 2 次。
#
# 📘 五个处理阶段：
#   1. PDF 渲染（PyMuPDF）→ 每页 JPEG bytes
#   2. Agent Brain 分析+策略（外部模型 + OCR/CV 工具）
#   3. 翻译（doubao via TranslationTool）
#   4. 自我审查（外部模型）
#   5. Word 生成（python-docx via WordWriterTool）
# =============================================================

import json
import time
import base64
import fitz  # PyMuPDF
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.agent_events import AgentEvent
from core.logger import get_logger
from tools.scan_tools import OCRTool, CVTool, TranslationTool, WordWriterTool, ImageGenTool
from tools.dynamic_tools import DynamicToolRegistry, CreateCustomToolTool

logger = get_logger("scan_agent")

# 📘 教学笔记：Agent 大脑的系统提示词
# 这是 Agent 的"灵魂"——告诉大脑它是谁、能做什么、该怎么做。
# 好的 system prompt 是 Agent 质量的关键。
SCAN_AGENT_SYSTEM_PROMPT = """\
你是文档翻译 Agent。分析扫描件图片，提取结构化内容并翻译。

## 工具
- ocr_extract_text: OCR 文字识别（返回文字和坐标）
- cv_detect_layout: 表格线和图片区域检测
- translate_texts: 文本翻译
- generate_translated_image: 图片生成（可选，仅排版极复杂时使用）

## 流程
1. 观察图片，判断类型（表格/证件/纯文本/混合）
2. 调用工具：有表格先 cv_detect_layout 再 ocr_extract_text，纯文本直接 ocr_extract_text
3. 调用 translate_texts 翻译
4. 输出 JSON（不要 markdown code block 包裹）

⚠️ 重要：每个工具只需调用一次！OCR 和 CV 结果已缓存，重复调用浪费资源。

## 输出 JSON 格式
{
  "page_type": "table_document" | "certificate" | "text_document" | "mixed",
  "elements": [
    {"type": "table", "col_widths": [30, 40, 30], "rows": [{"cells": [{"text": "原文", "colspan": 1, "rowspan": 1, "bold": false, "align": "center", "borders": {"top": true, "bottom": true, "left": true, "right": false}, "vertical": false}]}]},
    {"type": "paragraph", "text": "段落文字", "bold": false, "align": "left", "font_size": "normal"},
    {"type": "image_region", "image_index": 0, "description": "图片描述"}
  ],
  "items": [
    {"key": "pg{页码}_e{元素索引}_r{行}_c{列}", "text": "原文", "translation": "译文"},
    {"key": "pg{页码}_e{元素索引}_para", "text": "原文", "translation": "译文"}
  ]
}

## 规则
- 表格行列数必须与原文一致，col_widths 总和 = 100
- 不要遗漏文字，只在原文有线处标 borders 为 true
- 翻译目标语言：{{target_lang}}
"""

# 📘 教学笔记：统一审查提示词（v5 — 内容+排版+翻译质量）
# v5 架构中，审校职责统一由规划者管理。
# 自我审查同时检查：结构完整性、翻译质量、排版合理性。
SELF_REVIEW_PROMPT = """\
审查翻译结果，对比原始页面图片：
1. 文字是否遗漏？2. 译文是否准确？3. 表格结构是否正确？

当前结果：
{result_json}

合格回复：{{"passed": true, "reason": ""}}
不合格回复：{{"passed": false, "reason": "具体问题", "fix_actions": ["修正建议"]}}
"""


class ScanAgent:
    """
    📘 教学笔记：扫描件翻译 Agent

    这是一个真正的 Agent——有大脑（LLM）、有工具（OCR/CV/翻译/Word），
    能自主决策处理策略，而不是按固定流水线执行。

    职责：
    1. 将 PDF 渲染为页面图片
    2. 逐页调用 Agent Brain 处理（ReAct 循环）
    3. 自我审查每页结果
    4. 汇总结果，调用 Word Writer 生成文档
    5. 通过事件机制报告进度
    """

    def __init__(
        self,
        brain_engine,
        translate_pipeline,
        format_engine,
        image_gen_engine=None,
        max_tool_calls: int = 5,
        max_review_retries: int = 2,
        on_token_update: Callable[["ScanAgent"], None] = None,
    ):
        """
        📘 参数说明：
        - brain_engine: ExternalLLMEngine 实例（Agent 大脑，Gemini/Claude/GPT）
        - translate_pipeline: TranslatePipeline 实例（翻译用 doubao）
        - format_engine: FormatEngine 实例（Word 格式用）
        - image_gen_engine: 图片生成模型引擎（可选，如 gemini-3-pro-image-preview）
        - max_tool_calls: 单页最大工具调用次数（防止无限循环）
        - max_review_retries: 自我审查最大重试次数
        - on_token_update: 每次 token 用量变化时的回调（供 GUI 实时更新）
        """
        self.brain_engine = brain_engine
        self.translate_pipeline = translate_pipeline
        self.format_engine = format_engine
        self.image_gen_engine = image_gen_engine
        self.max_tool_calls = max_tool_calls
        self.max_review_retries = max_review_retries
        self.on_token_update = on_token_update

        # 📘 统计信息（v5: 4 个维度 — planner/translate/image_gen/reviewer）
        # planner = Agent Brain 的 token（分析+决策+审查）
        # translate = TranslationTool 调用的翻译模型 token
        # image_gen = ImageGenTool 调用的图片生成模型 token
        # reviewer = 自我审查阶段的 Brain token（从 planner 中拆分）
        self.stats = {
            "total_time_seconds": 0,
            "planner_tokens": {"prompt": 0, "completion": 0},
            "translate_tokens": {"prompt": 0, "completion": 0},
            "image_gen_tokens": {"prompt": 0, "completion": 0},
            "reviewer_tokens": {"prompt": 0, "completion": 0},
            "tool_calls": {"ocr": 0, "cv": 0, "translate": 0, "word_writer": 0, "image_gen": 0},
            "review_results": [],
        }

        # 📘 工具注册表（在 process_scan_pdf 中初始化，因为需要 page_images）
        self.tools = {}

        logger.info(
            f"ScanAgent 初始化完成 "
            f"(max_tool_calls={max_tool_calls}, max_review_retries={max_review_retries})"
        )

    def process_scan_pdf(
        self,
        filepath: str,
        output_path: str,
        target_lang: str = "英文",
        on_event: Callable[[AgentEvent], None] = None,
    ) -> Dict[str, Any]:
        """
        📘 教学笔记：端到端处理扫描件 PDF

        这是 ScanAgent 的主入口。流程：
        1. PDF → 每页 JPEG bytes（PyMuPDF 渲染）
        2. 逐页调用 _process_single_page（ReAct 循环）
        3. 每页完成后自我审查（_self_review）
        4. 汇总所有页面结果
        5. 调用 WordWriterTool 生成 .docx

        📘 与 v7.1 parse_scan_pdf 的区别：
        v7.1 返回 parsed_data，需要外部再调翻译和写入。
        Agent 模式是端到端的——分析、翻译、生成全在这里完成。
        """
        start_time = time.time()
        self._emit_event(on_event, "start", {"filepath": filepath})

        # ── 1. PDF 渲染 ──
        logger.info(f"开始 Agent 模式处理扫描件: {filepath}")
        print(f"[🤖 Agent 模式] 扫描件翻译 Agent 启动...", flush=True)

        doc = fitz.open(filepath)
        num_pages = len(doc)

        page_images = []  # List[bytes] 每页 JPEG
        page_images_b64 = []  # List[str] 每页 base64（给 Agent Brain 看）

        for i in range(num_pages):
            page = doc[i]
            # 📘 教学笔记：双分辨率策略
            # OCR 需要高分辨率（200 DPI）才能准确识别小字，
            # 但 LLM 视觉分析只需要看清布局（150 DPI 足够）。
            # 用高分辨率给 OCR/CV 工具，低分辨率给 Brain 看，节省 image tokens。

            # 📘 高分辨率：给 OCR/CV 工具用
            zoom_hi = 200 / 72.0
            mat_hi = fitz.Matrix(zoom_hi, zoom_hi)
            pix_hi = page.get_pixmap(matrix=mat_hi)
            jpeg_hi = pix_hi.tobytes("jpeg", jpg_quality=88)
            page_images.append(jpeg_hi)

            # 📘 低分辨率：给 Brain 看（节省 ~40% image tokens）
            zoom_lo = 150 / 72.0
            mat_lo = fitz.Matrix(zoom_lo, zoom_lo)
            pix_lo = page.get_pixmap(matrix=mat_lo)
            jpeg_lo = pix_lo.tobytes("jpeg", jpg_quality=75)
            page_images_b64.append(base64.b64encode(jpeg_lo).decode("utf-8"))

        doc.close()
        logger.info(f"PDF 渲染完成: {num_pages} 页")

        # 📘 初始化工具（需要 page_images 作为上下文）
        # 📘 教学笔记：current_page_index 机制
        # Brain 经常传错 page_index（比如总是传 0），因为它不知道当前处理的是第几页。
        # 解决方案：context 中维护 current_page_index，工具优先使用它。
        # 这样即使 Brain 传了错误的 page_index，工具也能用正确的页码。
        context = {"page_images": page_images, "current_page_index": 0}
        self.tools = {
            "ocr_extract_text": OCRTool(context=context),
            "cv_detect_layout": CVTool(context=context),
            "translate_texts": TranslationTool(translate_pipeline=self.translate_pipeline),
            "generate_word_document": WordWriterTool(
                format_engine=self.format_engine,
                page_images=page_images,
            ),
        }
        # 📘 图片生成工具（可选）：Agent Brain 自主决定是否调用
        if self.image_gen_engine:
            self.tools["generate_translated_image"] = ImageGenTool(
                image_gen_engine=self.image_gen_engine,
                context=context,
            )
            logger.info("图片生成工具已注册，Agent Brain 可自主调用")

        # 📘 动态工具系统：加载已有 + 注册创建工具
        self.dynamic_registry = DynamicToolRegistry()
        dynamic_tools = self.dynamic_registry.load_tools(context=context)
        if dynamic_tools:
            self.tools.update(dynamic_tools)
            logger.info(f"已加载 {len(dynamic_tools)} 个动态工具")
        self.tools["create_custom_tool"] = CreateCustomToolTool(
            registry=self.dynamic_registry, context=context,
        )

        # ── 2. 逐页处理 ──
        all_items = []
        all_page_structures = []
        all_translations = {}

        for page_idx in range(num_pages):
            progress_pct = int((page_idx / num_pages) * 80)  # 0-80% 给页面处理
            self._emit_event(on_event, "page_start", {
                "page_index": page_idx,
                "total_pages": num_pages,
                "progress_pct": progress_pct,
            })
            # 📘 更新 context 中的当前页码，工具会优先使用这个值
            context["current_page_index"] = page_idx
            print(
                f"  [🤖 第 {page_idx + 1}/{num_pages} 页] Agent Brain 分析中...",
                flush=True,
            )

            try:
                page_structure, page_items, page_translations = self._process_single_page(
                    page_idx=page_idx,
                    page_image_b64=page_images_b64[page_idx],
                    target_lang=target_lang,
                    on_event=on_event,
                )

                # ── 3. 自我审查 ──
                self._emit_event(on_event, "review", {
                    "page_index": page_idx,
                    "step": "审查",
                    "progress_pct": progress_pct + 5,
                })

                review_passed, review_reason, page_structure, page_items, page_translations = (
                    self._self_review(
                        page_idx=page_idx,
                        page_image_b64=page_images_b64[page_idx],
                        page_structure=page_structure,
                        items=page_items,
                        translations=page_translations,
                        target_lang=target_lang,
                        on_event=on_event,
                    )
                )

                all_page_structures.append(page_structure)
                all_items.extend(page_items)
                all_translations.update(page_translations)

                elem_count = len(page_structure.get("elements", []))
                logger.info(
                    f"第 {page_idx + 1} 页完成: {elem_count} 个元素, "
                    f"{len(page_items)} 个翻译单元, "
                    f"审查{'通过' if review_passed else '未通过: ' + review_reason}"
                )

            except Exception as e:
                # 📘 单页失败不影响其他页面——优雅降级
                logger.error(f"第 {page_idx + 1} 页处理失败: {type(e).__name__}: {e}")
                print(f"  [⚠️ 第 {page_idx + 1} 页] 处理失败: {e}", flush=True)
                all_page_structures.append({"page_type": "error", "elements": []})
                self.stats["review_results"].append({
                    "page": page_idx,
                    "passed": False,
                    "reason": f"处理异常: {str(e)}",
                    "retries": 0,
                })

        # ── 4. 生成 Word 文档 ──
        self._emit_event(on_event, "generating", {
            "step": "生成",
            "progress_pct": 85,
        })
        print(f"[🤖 生成文档] 调用 Word Writer...", flush=True)

        try:
            writer_result = self.tools["generate_word_document"].execute({
                "page_structures": all_page_structures,
                "translations": all_translations,
                "output_path": output_path,
            })
            writer_data = json.loads(writer_result)
            if "error" in writer_data:
                logger.error(f"Word 生成失败: {writer_data['error']}")
                raise RuntimeError(writer_data["error"])
            final_output_path = writer_data.get("output_path", output_path)
        except Exception as e:
            logger.error(f"Word 生成异常: {e}")
            final_output_path = output_path

        # ── 5. 统计 ──
        self.stats["total_time_seconds"] = round(time.time() - start_time, 1)
        self._notify_token_update()

        self._emit_event(on_event, "complete", {
            "progress_pct": 100,
            "stats": self.stats,
        })

        total_items = len(all_items)
        total_translated = len(all_translations)
        print(
            f"[🤖 Agent 完成] {num_pages} 页, {total_items} 个翻译单元, "
            f"翻译 {total_translated} 个, 耗时 {self.stats['total_time_seconds']}s",
            flush=True,
        )

        return {
            "source": "scan_agent",
            "source_type": "scan",
            "filepath": filepath,
            "output_path": final_output_path,
            "items": all_items,
            "page_structures": all_page_structures,
            "page_images": page_images,
            "stats": self.stats,
        }

    def _emit_event(self, on_event, event_type: str, data: dict):
        """📘 发射进度事件给 GUI"""
        if on_event:
            try:
                on_event(AgentEvent(type=event_type, data=data))
            except Exception as e:
                logger.debug(f"事件发射失败: {e}")

    def _notify_token_update(self):
        """📘 通知 GUI 更新 token 用量（实时刷新）"""
        if self.on_token_update:
            try:
                self.on_token_update(self)
            except Exception:
                pass

    def _process_single_page(
        self,
        page_idx: int,
        page_image_b64: str,
        target_lang: str,
        on_event: Callable[[AgentEvent], None] = None,
    ) -> Tuple[dict, List[dict], Dict[str, str]]:
        """
        📘 教学笔记：用 Agent Brain 处理单页（ReAct 循环）

        这是 Agent 架构的核心——ReAct（Reasoning + Acting）循环：

        1. 发送页面图片 + system prompt 给 Agent Brain
        2. Brain 返回 tool_call → 执行工具 → 将结果反馈给 Brain
        3. Brain 返回 text（最终 JSON）→ 解析结构化数据 → 结束
        4. 工具调用次数上限 max_tool_calls，达到上限强制结束

        📘 为什么叫 ReAct？
        Reasoning（推理）：Brain 看到图片/工具结果后思考下一步
        Acting（行动）：Brain 决定调用哪个工具
        这个循环让 Agent 能自适应不同文档类型。

        返回: (page_structure, items, translations)
        """
        # 📘 构建初始消息：system prompt + 页面图片
        system_prompt = SCAN_AGENT_SYSTEM_PROMPT.replace("{{target_lang}}", target_lang)

        # 📘 教学笔记：预执行 OCR + CV，减少 ReAct 循环次数
        # 之前 Brain 每页要调 2-4 次工具（OCR、CV），每次都要重发图片 + 对话历史，
        # 导致 prompt tokens 爆炸。优化：先跑 OCR 和 CV，把结果直接塞进初始消息，
        # Brain 只需要看结果 → 翻译 → 输出 JSON，最少只需 1-2 轮 ReAct。
        ocr_result = self.tools["ocr_extract_text"].execute({"page_index": page_idx})
        cv_result = self.tools["cv_detect_layout"].execute({"page_index": page_idx})

        # 📘 统计预执行的工具调用
        self.stats["tool_calls"]["ocr"] = self.stats["tool_calls"].get("ocr", 0) + 1
        self.stats["tool_calls"]["cv"] = self.stats["tool_calls"].get("cv", 0) + 1

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{page_image_b64}",
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"请分析这张文档图片（第 {page_idx} 页），"
                            f"提取结构化内容并翻译为{target_lang}。\n\n"
                            f"## 已有的 OCR 结果\n{ocr_result}\n\n"
                            f"## 已有的 CV 布局检测结果\n{cv_result}\n\n"
                            f"OCR 和 CV 已执行完毕，请直接使用上述结果。"
                            f"如需翻译请调用 translate_texts，然后输出最终 JSON。"
                        ),
                    },
                ],
            },
        ]

        # 📘 构建工具列表（给 Brain 的 tools 参数）
        # 📘 教学笔记：精简工具列表
        # OCR 和 CV 已预执行，Brain 通常只需要 translate_texts。
        # 但保留 OCR/CV 以防 Brain 需要对特定区域重新识别。
        tool_schemas = [
            self.tools["ocr_extract_text"].get_api_format(),
            self.tools["cv_detect_layout"].get_api_format(),
            self.tools["translate_texts"].get_api_format(),
        ]
        # 📘 图片生成工具（可选）
        if "generate_translated_image" in self.tools:
            tool_schemas.append(
                self.tools["generate_translated_image"].get_api_format()
            )

        # 📘 ReAct 循环
        tool_call_count = 0
        final_text = ""

        while tool_call_count < self.max_tool_calls:
            # 📘 调用 Agent Brain
            tool_calls_in_turn = []
            text_in_turn = ""

            try:
                for chunk in self.brain_engine.stream_chat(
                    messages, tools=tool_schemas, max_tokens=16384,
                ):
                    if chunk["type"] == "text":
                        text_in_turn += chunk["content"]
                    elif chunk["type"] == "tool_call":
                        tool_calls_in_turn.append(chunk)
                    elif chunk["type"] == "usage":
                        self.stats["planner_tokens"]["prompt"] += chunk.get("prompt_tokens", 0)
                        self.stats["planner_tokens"]["completion"] += chunk.get("completion_tokens", 0)
                        self._notify_token_update()
            except Exception as e:
                logger.error(f"Agent Brain 调用失败: {e}")
                logger.error(f"已收集的文本: {text_in_turn[:200] if text_in_turn else '(空)'}")
                raise

            # 📘 情况1：Brain 返回了工具调用 → 执行工具，继续循环
            if tool_calls_in_turn:
                # 📘 把 Brain 的 assistant 消息（含 tool_calls）加入对话历史
                assistant_msg = {"role": "assistant", "content": text_in_turn or None}
                assistant_msg["tool_calls"] = []
                for tc in tool_calls_in_turn:
                    tc_entry = {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    # 📘 教学笔记：Gemini thought_signature 回传
                    # Gemini 3.x 要求把 thought_signature 原样回传，
                    # 否则下一轮 API 调用会返回 400 错误。
                    # extra_content 由 ExternalLLMEngine 从流式响应中捕获并透传。
                    if "extra_content" in tc:
                        tc_entry["extra_content"] = tc["extra_content"]
                    assistant_msg["tool_calls"].append(tc_entry)
                messages.append(assistant_msg)

                # 📘 逐个执行工具调用
                for tc in tool_calls_in_turn:
                    tool_name = tc["name"]
                    tool_call_id = tc["id"]

                    # 📘 教学笔记：Gemini 拼接工具名防御
                    # Gemini 偶尔会把两个工具名拼在一起返回，
                    # 如 "cv_detect_layoutocr_extract_text"。
                    # 检测方法：如果 tool_name 不在已知工具中，
                    # 尝试从已知工具名列表中拆分出第一个匹配的。
                    if tool_name not in self.tools:
                        known_names = sorted(self.tools.keys(), key=len, reverse=True)
                        split_found = False
                        for known in known_names:
                            if tool_name.startswith(known) and len(tool_name) > len(known):
                                remainder = tool_name[len(known):]
                                if remainder in self.tools:
                                    logger.warning(
                                        f"检测到 Gemini 拼接工具名: '{tool_name}' → "
                                        f"拆分为 '{known}' + '{remainder}'，使用第一个"
                                    )
                                    tool_name = known
                                    split_found = True
                                    break
                        if not split_found:
                            # 📘 也可能是反过来拼的，检查 endswith
                            for known in known_names:
                                if tool_name.endswith(known) and len(tool_name) > len(known):
                                    prefix = tool_name[:-len(known)]
                                    if prefix in self.tools:
                                        logger.warning(
                                            f"检测到 Gemini 拼接工具名: '{tool_name}' → "
                                            f"拆分为 '{prefix}' + '{known}'，使用第一个"
                                        )
                                        tool_name = prefix
                                        split_found = True
                                        break

                    tool_call_count += 1

                    # 📘 统计工具调用次数
                    stat_key = {
                        "ocr_extract_text": "ocr",
                        "cv_detect_layout": "cv",
                        "translate_texts": "translate",
                        "generate_translated_image": "image_gen",
                    }.get(tool_name, tool_name)
                    self.stats["tool_calls"][stat_key] = (
                        self.stats["tool_calls"].get(stat_key, 0) + 1
                    )

                    # 📘 解析参数并执行
                    try:
                        tool_params = json.loads(tc["arguments"])
                    except json.JSONDecodeError:
                        tool_params = {}

                    logger.info(
                        f"第 {page_idx} 页: 调用工具 {tool_name} "
                        f"(第 {tool_call_count}/{self.max_tool_calls} 次)"
                    )

                    if tool_name in self.tools:
                        tool_result = self.tools[tool_name].execute(tool_params)
                    else:
                        # 📘 检查是否是刚创建的动态工具
                        dynamic_tool = self.dynamic_registry.get_tool(tool_name) if hasattr(self, 'dynamic_registry') else None
                        if dynamic_tool:
                            self.tools[tool_name] = dynamic_tool
                            tool_result = dynamic_tool.execute(tool_params)
                            # 📘 更新 tool_schemas 让 Brain 知道新工具可用
                            tool_schemas = [
                                t.get_api_format() for name, t in self.tools.items()
                                if name != "generate_word_document"
                            ]
                        else:
                            tool_result = json.dumps(
                                {"error": f"未知工具: {tool_name}"},
                                ensure_ascii=False,
                            )

                    # 📘 把工具结果作为 tool message 反馈给 Brain
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": tool_result,
                    })

                    self._emit_event(on_event, "tool_call", {
                        "page_index": page_idx,
                        "tool_name": tool_name,
                        "call_count": tool_call_count,
                    })

                    # 📘 翻译工具执行后通知 token 更新（pipeline tokens 变化了）
                    if tool_name == "translate_texts":
                        self._notify_token_update()

                continue  # 继续 ReAct 循环

            # 📘 情况2：Brain 返回了纯文本（最终结果）→ 解析并结束
            if text_in_turn:
                final_text = text_in_turn
                break

        # 📘 达到工具调用上限，强制结束
        if tool_call_count >= self.max_tool_calls and not final_text:
            logger.warning(
                f"第 {page_idx} 页: 工具调用达到上限 {self.max_tool_calls}，"
                f"强制要求 Brain 输出结果"
            )
            # 📘 追加一条消息，要求 Brain 立即输出结果
            messages.append({
                "role": "user",
                "content": "工具调用次数已达上限。请立即根据已有信息输出最终的 JSON 结构化数据。",
            })
            for chunk in self.brain_engine.stream_chat(messages, max_tokens=16384):
                if chunk["type"] == "text":
                    final_text += chunk["content"]
                elif chunk["type"] == "usage":
                    self.stats["planner_tokens"]["prompt"] += chunk.get("prompt_tokens", 0)
                    self.stats["planner_tokens"]["completion"] += chunk.get("completion_tokens", 0)
                    self._notify_token_update()

        # 📘 解析 Brain 输出的 JSON
        logger.info(f"第 {page_idx} 页: Brain 输出 {len(final_text)} 字符")
        logger.debug(f"第 {page_idx} 页 Brain 原始输出前 500 字符: {final_text[:500]}")
        page_structure, items, translations = self._parse_brain_output(
            final_text, page_idx
        )

        return page_structure, items, translations

    def _parse_brain_output(
        self, text: str, page_idx: int
    ) -> Tuple[dict, List[dict], Dict[str, str]]:
        """
        📘 教学笔记：解析 Agent Brain 输出的 JSON

        Brain 输出的 JSON 包含 page_structure 和 items（含翻译）。
        需要从中提取：
        1. page_structure: 给 scan_writer 用的结构化数据
        2. items: 翻译单元列表（与 parse_scan_pdf 兼容）
        3. translations: {key: 译文} 映射

        📘 容错处理：
        LLM 输出的 JSON 经常有小问题（markdown 包裹、trailing comma 等），
        复用 scan_parser 的 _parse_structure_json 做容错解析。
        """
        from translator.scan_parser import _parse_structure_json

        structure = _parse_structure_json(text)
        if not structure:
            logger.warning(f"第 {page_idx} 页: Brain 输出 JSON 解析失败")
            logger.debug(f"Brain 原始输出: {text[:500]}")
            return {"page_type": "error", "elements": []}, [], {}

        # 📘 从 Brain 输出中提取 items 和 translations
        items = []
        translations = {}

        # 📘 方式1：Brain 直接输出了 items 数组（推荐格式）
        brain_items = structure.pop("items", [])
        for item in brain_items:
            key = item.get("key", "")
            text_val = item.get("text", "")
            translation = item.get("translation", "")
            if key and text_val:
                items.append({
                    "key": key,
                    "type": "table_cell" if "_r" in key and "_c" in key else "pdf_block",
                    "full_text": text_val,
                    "is_empty": False,
                    "dominant_format": {
                        "font_name": "Unknown",
                        "font_size": 10,
                        "font_color": "#000000",
                        "bold": False,
                    },
                })
                if translation:
                    translations[key] = translation

        # 📘 方式2：如果 Brain 没输出 items，从 elements 中提取
        if not items:
            items, translations = self._extract_items_from_elements(
                structure, page_idx
            )

        return structure, items, translations

    def _extract_items_from_elements(
        self, structure: dict, page_idx: int
    ) -> Tuple[List[dict], Dict[str, str]]:
        """
        📘 从 page_structure 的 elements 中提取 items 和 translations

        这是 fallback 路径——如果 Brain 没有直接输出 items 数组，
        就从 elements 的表格/段落中按 v7.1 的规则提取。
        """
        items = []
        translations = {}
        elements = structure.get("elements", [])

        for elem_idx, elem in enumerate(elements):
            elem_type = elem.get("type", "")

            if elem_type == "table":
                for row_idx, row in enumerate(elem.get("rows", [])):
                    cells = row.get("cells", row) if isinstance(row, dict) else row
                    if isinstance(cells, dict):
                        cells = cells.get("cells", [])
                    for col_idx, cell in enumerate(cells):
                        # 📘 支持 "lines" 数组格式
                        cell_lines = cell.get("lines")
                        if cell_lines and isinstance(cell_lines, list):
                            cell_text = "\n".join(
                                l.get("text", "") for l in cell_lines
                            ).strip()
                        else:
                            cell_text = cell.get("text", "").strip()

                        if cell_text:
                            key = f"pg{page_idx}_e{elem_idx}_r{row_idx}_c{col_idx}"
                            items.append({
                                "key": key,
                                "type": "table_cell",
                                "full_text": cell_text,
                                "is_empty": False,
                                "dominant_format": {
                                    "font_name": "Unknown",
                                    "font_size": 10,
                                    "font_color": "#000000",
                                    "bold": cell.get("bold", False),
                                },
                            })
                            # 📘 如果 cell 有 translation 字段，直接用
                            trans = cell.get("translation", "")
                            if trans:
                                translations[key] = trans

            elif elem_type == "paragraph":
                para_text = elem.get("text", "").strip()
                if para_text:
                    key = f"pg{page_idx}_e{elem_idx}_para"
                    items.append({
                        "key": key,
                        "type": "pdf_block",
                        "full_text": para_text,
                        "is_empty": False,
                        "dominant_format": {
                            "font_name": "Unknown",
                            "font_size": 11,
                            "font_color": "#000000",
                            "bold": elem.get("bold", False),
                        },
                    })
                    trans = elem.get("translation", "")
                    if trans:
                        translations[key] = trans

        return items, translations

    def _self_review(
        self,
        page_idx: int,
        page_image_b64: str,
        page_structure: dict,
        items: list,
        translations: Dict[str, str],
        target_lang: str,
        on_event: Callable[[AgentEvent], None] = None,
    ) -> Tuple[bool, str, dict, list, Dict[str, str]]:
        """
        📘 教学笔记：自我审查（Self-Review）

        Agent 大脑检查自己的输出质量：
        1. 发送页面图片 + 提取结果给 Brain
        2. Brain 判断是否通过
        3. 未通过 → 重新处理（最多 max_review_retries 次）
        4. 2 次重试后仍未通过 → 标记质量问题并继续

        📘 为什么需要自我审查？
        LLM 不是完美的——可能遗漏文字、搞错表格结构。
        让 Agent 自己检查一遍，能发现并修正大部分问题。
        这比人工检查便宜得多，而且是自动的。

        返回: (passed, reason, page_structure, items, translations)
        """
        retries = 0
        passed = False
        reason = ""

        while retries <= self.max_review_retries:
            # 📘 构建审查请求
            result_json = json.dumps({
                "page_structure": page_structure,
                "items_count": len(items),
                "translations_count": len(translations),
                "translations_sample": dict(list(translations.items())[:5]),
            }, ensure_ascii=False, indent=2)

            review_prompt = SELF_REVIEW_PROMPT.format(result_json=result_json)

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{page_image_b64}",
                            },
                        },
                        {"type": "text", "text": review_prompt},
                    ],
                },
            ]

            # 📘 调用 Brain 审查
            review_text = ""
            try:
                for chunk in self.brain_engine.stream_chat(messages, max_tokens=4096):
                    if chunk["type"] == "text":
                        review_text += chunk["content"]
                    elif chunk["type"] == "usage":
                        self.stats["reviewer_tokens"]["prompt"] += chunk.get("prompt_tokens", 0)
                        self.stats["reviewer_tokens"]["completion"] += chunk.get("completion_tokens", 0)
                        self._notify_token_update()
            except Exception as e:
                logger.warning(f"第 {page_idx} 页审查调用失败: {e}")
                # 📘 审查失败不阻塞流程，标记并继续
                passed = True
                reason = f"审查调用失败: {str(e)}"
                break

            # 📘 解析审查结果
            try:
                from translator.scan_parser import _parse_structure_json
                review_result = _parse_structure_json(review_text)
                if not review_result:
                    # 📘 尝试直接 json.loads
                    review_result = json.loads(review_text.strip())
            except (json.JSONDecodeError, Exception):
                review_result = None

            if review_result and review_result.get("passed", False):
                passed = True
                reason = review_result.get("reason", "")
                logger.info(f"第 {page_idx} 页审查通过")
                break

            # 📘 审查未通过
            reason = (
                review_result.get("reason", "审查未通过")
                if review_result
                else "审查结果解析失败"
            )
            retries += 1

            if retries <= self.max_review_retries:
                logger.info(
                    f"第 {page_idx} 页审查未通过 (原因: {reason})，"
                    f"第 {retries} 次重试..."
                )
                print(
                    f"  [🔄 第 {page_idx + 1} 页] 审查未通过，重试中 ({retries}/{self.max_review_retries})...",
                    flush=True,
                )

                # 📘 教学笔记：增量修正 vs 完全重来
                # 之前审查未通过时完全重新处理该页（重发图片 + 重新 OCR + 重新翻译），
                # 浪费大量 token。优化：把审查反馈发给 Brain，让它基于已有结果修正，
                # 只需 1 次 Brain 调用（不含图片），而不是完整的 ReAct 循环。
                try:
                    fix_messages = [
                        {"role": "system", "content": SCAN_AGENT_SYSTEM_PROMPT.replace("{{target_lang}}", target_lang)},
                        {
                            "role": "user",
                            "content": (
                                f"以下是第 {page_idx} 页的分析结果，审查发现问题：{reason}\n\n"
                                f"当前结果：\n{json.dumps(page_structure, ensure_ascii=False, indent=2)}\n\n"
                                f"请修正上述问题，输出完整的修正后 JSON。"
                            ),
                        },
                    ]
                    fix_text = ""
                    for chunk in self.brain_engine.stream_chat(fix_messages, max_tokens=16384):
                        if chunk["type"] == "text":
                            fix_text += chunk["content"]
                        elif chunk["type"] == "usage":
                            self.stats["planner_tokens"]["prompt"] += chunk.get("prompt_tokens", 0)
                            self.stats["planner_tokens"]["completion"] += chunk.get("completion_tokens", 0)
                            self._notify_token_update()

                    if fix_text:
                        new_structure, new_items, new_translations = self._parse_brain_output(
                            fix_text, page_idx
                        )
                        if new_structure.get("page_type") != "error":
                            page_structure = new_structure
                            items = new_items
                            translations = new_translations
                except Exception as e:
                    logger.error(f"第 {page_idx} 页增量修正失败: {e}")
                    reason = f"修正失败: {str(e)}"
                    break
            else:
                logger.warning(
                    f"第 {page_idx} 页审查 {self.max_review_retries} 次重试后仍未通过: {reason}"
                )
                print(
                    f"  [⚠️ 第 {page_idx + 1} 页] 审查未通过，标记质量问题并继续",
                    flush=True,
                )

        # 📘 记录审查结果
        self.stats["review_results"].append({
            "page": page_idx,
            "passed": passed,
            "reason": reason,
            "retries": retries,
        })

        return passed, reason, page_structure, items, translations
