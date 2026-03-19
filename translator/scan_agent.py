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
你是一个专业的文档分析和翻译 Agent。你的任务是分析扫描件文档图片，提取结构化内容，并完成翻译。

## 你的能力
你可以调用以下工具：
- ocr_extract_text: OCR 文字识别（返回文字内容和位置坐标）
- cv_detect_layout: 表格线和图片区域检测（返回水平线、垂直线、图片区域）
- translate_texts: 文本翻译（使用 doubao 模型翻译为目标语言）
- generate_translated_image: 图片生成（可选，将页面区域重新绘制为目标语言版本）

## 图片生成能力（如果可用）
当你判断某些内容用图片生成方式能更好地保持原文的布局和视觉效果时，
可以调用 generate_translated_image 工具。典型场景：
- 排版极其复杂的证件、名片、海报
- 包含大量装饰性文字和图形元素的页面
- 文字与背景图片紧密融合的区域

调用流程（两步走）：
1. 你先自己翻译出准确的译文（translated_text）
2. 你再写一段详细的图片生成提示词（image_prompt），描述：
   - 原文的布局结构（位置、大小、排列）
   - 字体风格（大小、粗细、颜色）
   - 需要保持的设计元素（边框、背景、logo）
   - 将译文放在对应位置的具体指令
3. 调用 generate_translated_image，传入译文和提示词

注意：图片生成成本较高，只在你认为确实需要时才使用。
大多数文档（表格、纯文本）用 OCR + 翻译 + Word 生成即可。

## 工作流程
1. 观察页面图片，判断文档类型（表格文档/证件/纯文本/混合等）
2. 根据文档类型决定调用哪些工具：
   - 有表格 → 先调 cv_detect_layout 检测表格线，再调 ocr_extract_text 识别文字
   - 纯文本/证件 → 直接调 ocr_extract_text
   - 有图片 → cv_detect_layout 会检测图片区域
   - 排版极复杂 → 考虑用 generate_translated_image
3. 综合工具结果和你的视觉理解，生成结构化数据
4. 调用 translate_texts 翻译所有文本
5. 输出最终的 JSON 结构化数据

## 输出格式
当你完成分析和翻译后，输出严格的 JSON（不要包裹在 markdown code block 中）：
{
  "page_type": "table_document" | "certificate" | "text_document" | "mixed",
  "elements": [
    {
      "type": "table",
      "col_widths": [30, 40, 30],
      "rows": [
        {
          "cells": [
            {
              "text": "原文内容",
              "colspan": 1,
              "rowspan": 1,
              "bold": false,
              "align": "center",
              "borders": {"top": true, "bottom": true, "left": true, "right": false},
              "vertical": false
            }
          ]
        }
      ]
    },
    {"type": "paragraph", "text": "段落文字", "bold": false, "align": "left", "font_size": "normal"},
    {"type": "image_region", "image_index": 0, "description": "图片描述"}
  ],
  "items": [
    {"key": "pg{页码}_e{元素索引}_r{行}_c{列}", "text": "原文", "translation": "译文"},
    {"key": "pg{页码}_e{元素索引}_para", "text": "原文", "translation": "译文"}
  ]
}

## 重要规则
- 表格的行列数必须与原文完全一致
- 不要遗漏任何文字内容
- col_widths 总和必须等于 100
- 只在原文画了线的地方标 borders 为 true
- key 命名规则：pg{页码}_e{元素索引}_r{行}_c{列}（表格）或 pg{页码}_e{元素索引}_para（段落）
- 图片区域标记位置即可，不需要识别图片内容
- 翻译目标语言：{{target_lang}}

## 创建自定义工具
如果你发现现有工具无法解决某个问题，可以调用 create_custom_tool 创建新工具。
- 代码必须定义 run(params, context) 函数，返回 JSON 字符串
- context 包含 page_images 等共享数据
- 只能使用安全模块: json, re, math, string, collections, itertools 等
- 创建后的工具立即可用，并会持久化保存供未来复用
"""

# 📘 教学笔记：统一审查提示词（v5 — 内容+排版+翻译质量）
# v5 架构中，审校职责统一由规划者管理。
# 自我审查同时检查：结构完整性、翻译质量、排版合理性。
SELF_REVIEW_PROMPT = """\
请审查以下文档分析和翻译结果的质量。对比原始页面图片，检查：

1. **文字提取完整性**：是否有遗漏的文字？OCR 结果是否完整？
2. **翻译质量**：译文是否准确？是否有漏译、误译、语法错误？术语是否统一？
3. **翻译覆盖率**：所有文字都翻译了吗？是否有原文残留？
4. **结构正确性**：表格行列数对吗？合并单元格对吗？
5. **边框准确性**：只在原文有线的地方标了 true 吗？
6. **排版合理性**：译文长度是否合理？标题是否简洁？正文是否通顺？

当前结果：
{result_json}

如果结果质量合格，回复 JSON：{{"passed": true, "reason": ""}}
如果有问题需要修正，回复 JSON：{{"passed": false, "reason": "具体问题描述", "fix_actions": ["需要重新OCR第X区域", "需要补充翻译", "需要修正译文"]}}
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
        max_tool_calls: int = 10,
        max_review_retries: int = 3,
    ):
        """
        📘 参数说明：
        - brain_engine: ExternalLLMEngine 实例（Agent 大脑，Gemini/Claude/GPT）
        - translate_pipeline: TranslatePipeline 实例（翻译用 doubao）
        - format_engine: FormatEngine 实例（Word 格式用）
        - image_gen_engine: 图片生成模型引擎（可选，如 gemini-3-pro-image-preview）
        - max_tool_calls: 单页最大工具调用次数（防止无限循环）
        - max_review_retries: 自我审查最大重试次数
        """
        self.brain_engine = brain_engine
        self.translate_pipeline = translate_pipeline
        self.format_engine = format_engine
        self.image_gen_engine = image_gen_engine
        self.max_tool_calls = max_tool_calls
        self.max_review_retries = max_review_retries

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
            zoom = 200 / 72.0  # 200 DPI
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            jpeg_bytes = pix.tobytes("jpeg", jpg_quality=88)
            page_images.append(jpeg_bytes)
            page_images_b64.append(base64.b64encode(jpeg_bytes).decode("utf-8"))

        doc.close()
        logger.info(f"PDF 渲染完成: {num_pages} 页")

        # 📘 初始化工具（需要 page_images 作为上下文）
        context = {"page_images": page_images}
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

        # 📘 从 TranslatePipeline 收集翻译模型的 token 用量
        if self.translate_pipeline:
            self.stats["translate_tokens"]["prompt"] += self.translate_pipeline.total_translate_tokens
            # 📘 pipeline 只暴露 total，无法拆分 prompt/completion，全计入 prompt

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
                            f"提取结构化内容并翻译为{target_lang}。"
                        ),
                    },
                ],
            },
        ]

        # 📘 构建工具列表（给 Brain 的 tools 参数）
        # WordWriterTool 不暴露给 Brain——Word 生成由 ScanAgent 统一调用
        tool_schemas = [
            self.tools["ocr_extract_text"].get_api_format(),
            self.tools["cv_detect_layout"].get_api_format(),
            self.tools["translate_texts"].get_api_format(),
        ]
        # 📘 图片生成工具（可选）：让 Brain 自主决定是否调用
        if "generate_translated_image" in self.tools:
            tool_schemas.append(
                self.tools["generate_translated_image"].get_api_format()
            )
        # 📘 动态工具：create_custom_tool + 已加载的动态工具
        if "create_custom_tool" in self.tools:
            tool_schemas.append(self.tools["create_custom_tool"].get_api_format())
        if hasattr(self, 'dynamic_registry'):
            tool_schemas.extend(self.dynamic_registry.get_tool_schemas())

        # 📘 ReAct 循环
        tool_call_count = 0
        final_text = ""

        while tool_call_count < self.max_tool_calls:
            # 📘 调用 Agent Brain
            tool_calls_in_turn = []
            text_in_turn = ""

            try:
                for chunk in self.brain_engine.stream_chat(messages, tools=tool_schemas):
                    if chunk["type"] == "text":
                        text_in_turn += chunk["content"]
                    elif chunk["type"] == "tool_call":
                        tool_calls_in_turn.append(chunk)
                    elif chunk["type"] == "usage":
                        self.stats["planner_tokens"]["prompt"] += chunk.get("prompt_tokens", 0)
                        self.stats["planner_tokens"]["completion"] += chunk.get("completion_tokens", 0)
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
            for chunk in self.brain_engine.stream_chat(messages):
                if chunk["type"] == "text":
                    final_text += chunk["content"]
                elif chunk["type"] == "usage":
                    self.stats["planner_tokens"]["prompt"] += chunk.get("prompt_tokens", 0)
                    self.stats["planner_tokens"]["completion"] += chunk.get("completion_tokens", 0)

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
                for chunk in self.brain_engine.stream_chat(messages):
                    if chunk["type"] == "text":
                        review_text += chunk["content"]
                    elif chunk["type"] == "usage":
                        self.stats["reviewer_tokens"]["prompt"] += chunk.get("prompt_tokens", 0)
                        self.stats["reviewer_tokens"]["completion"] += chunk.get("completion_tokens", 0)
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

                # 📘 重新处理该页
                try:
                    page_structure, items, translations = self._process_single_page(
                        page_idx=page_idx,
                        page_image_b64=page_image_b64,
                        target_lang=target_lang,
                        on_event=on_event,
                    )
                except Exception as e:
                    logger.error(f"第 {page_idx} 页重试失败: {e}")
                    reason = f"重试失败: {str(e)}"
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
