# translator/layout_agent.py
# =============================================================
# 📘 教学笔记：PDF 排版 Agent（Layout Agent）
# =============================================================
# 这是翻译流程的最后一环——排版质量保障。
#
# 📘 核心理念：让 Agent 自己审查、自己修正
#   传统方案：写死规则（溢出就缩字）→ 不灵活，效果差
#   Agent 方案：规划者看图+数据 → 自己决定怎么修 → 验证效果 → 迭代
#
# 📘 工作流程（ReAct 循环）：
#   1. 渲染翻译后的页面截图
#   2. 调用 measure_overflow 获取溢出数据
#   3. 规划者分析：哪些需要缩字？哪些需要重新翻译？
#   4. 调用 resize_font / retranslate_shorter 修正
#   5. 再次渲染+检测，验证效果
#   6. 满意 → 输出最终结果；不满意 → 继续迭代
#
# 📘 与 ScanAgent 的区别：
#   ScanAgent: 处理扫描件（OCR + 翻译 + Word 生成）
#   LayoutAgent: 处理普通 PDF 的排版修正（翻译后的后处理）
#
# 📘 token 成本控制：
#   - 最多 3 轮迭代（balance 质量 vs 成本）
#   - 每轮只渲染有问题的页面（不渲染全部）
#   - 如果第一轮检测没有溢出，直接跳过（零成本）
# =============================================================

import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.logger import get_logger
from tools.layout_tools import (
    MeasureOverflowTool,
    ResizeFontTool,
    RetranslateShorterTool,
    RenderPageTool,
    SaveLayoutRuleTool,
)
from tools.dynamic_tools import DynamicToolRegistry, CreateCustomToolTool

logger = get_logger("layout_agent")

# 📘 Layout Agent 的系统提示词
LAYOUT_AGENT_SYSTEM_PROMPT = """\
你是一个专业的 PDF 排版审校 Agent。你的任务是检查翻译后的 PDF 文档排版质量，并修正溢出问题。

## 你的目标
确保翻译后的 PDF 文档：
1. 所有译文都在原始文本框内，不溢出
2. 字号不会太小（最小不低于 5pt），保持可读性
3. 译文内容准确，不因为缩短而丢失关键信息
4. 整体排版与原文档尽量一致

## 你的工具
- measure_overflow: 检测文本块溢出情况（先调这个了解全局状况）
- resize_font: 调整字号（轻微溢出时优先用这个）
- retranslate_shorter: 重新翻译更短版本（严重溢出或缩字后仍放不下时用）
- render_page_preview: 渲染页面截图供你视觉审查（需要看效果时用）
- save_layout_rule: 沉淀有效的修正策略为持久化规则（发现通用规律时用）

## 工作流程
1. 先调 measure_overflow(page_index=-1) 检测所有页面
2. 如果没有溢出（全部 ok），直接输出 DONE
3. 如果有溢出，按严重程度处理：
   - overflow_ratio <= 1.3（轻微）：resize_font 缩小到 85%
   - overflow_ratio <= 2.0（中等）：resize_font 缩小到 70%
   - overflow_ratio > 2.0（严重）：retranslate_shorter 重新翻译
4. 修正后再次 measure_overflow 验证
5. 如果发现某个修正策略反复有效，用 save_layout_rule 沉淀

## 输出格式
当你完成所有修正后，输出 JSON：
{"status": "done", "fixed_count": N, "summary": "修正摘要"}

## 创建自定义工具
如果你发现现有工具无法解决某个问题，可以调用 create_custom_tool 创建新工具。
- 代码必须定义 run(params, context) 函数，返回 JSON 字符串
- context 包含: translations, overrides, parsed_data 等共享数据
- 只能使用安全模块: json, re, math, string, collections, itertools 等
- 创建后的工具立即可用，并会持久化保存供未来复用
- 典型场景：批量按规则调整字号、检测特定语言对的字符宽度比、按文本角色分组处理等

## 重要规则
- 不要过度缩小字号，最小 5pt
- 重新翻译时要保持核心含义，只是表达更精简
- 标题类文本优先缩短译文，正文类优先缩小字号
- 每轮最多处理 20 个溢出块，避免一次改太多
"""


class PDFLayoutAgent:
    """
    📘 PDF 排版 Agent：翻译后自动检测溢出并修正。

    使用 Agent Brain（Gemini/Claude）作为规划者，
    通过 ReAct 循环自主决策修正策略。

    📘 零成本快速路径：
    如果 measure_overflow 检测到没有溢出，直接返回，
    不调用 Agent Brain，不消耗任何外部模型 token。
    """

    def __init__(
        self,
        brain_engine,
        translate_pipeline,
        format_engine,
        max_rounds: int = 3,
        max_tool_calls_per_round: int = 10,
    ):
        """
        📘 参数说明：
        - brain_engine: Agent Brain 引擎（Gemini/Claude）
        - translate_pipeline: 翻译流水线（重新翻译时用）
        - format_engine: 格式引擎
        - max_rounds: 最大迭代轮数（检测→修正→验证 算一轮）
        - max_tool_calls_per_round: 每轮最大工具调用次数
        """
        self.brain_engine = brain_engine
        self.translate_pipeline = translate_pipeline
        self.format_engine = format_engine
        self.max_rounds = max_rounds
        self.max_tool_calls_per_round = max_tool_calls_per_round

        # 📘 统计
        self.stats = {
            "rounds": 0,
            "tool_calls": 0,
            "fixed_count": 0,
            "brain_tokens": {"prompt": 0, "completion": 0},
        }

    def review_and_fix(
        self,
        source_path: str,
        parsed_data: Dict[str, Any],
        translations: Dict[str, str],
        target_lang: str = "英文",
    ) -> Tuple[Dict[str, str], Dict[str, dict]]:
        """
        📘 主入口：审查并修正 PDF 排版。

        返回: (updated_translations, layout_overrides)
        - updated_translations: 可能包含重新翻译的译文
        - layout_overrides: {key: {fontsize: N}} 字号覆盖

        📘 零成本快速路径：
        先用纯算法检测溢出，如果没有溢出直接返回，
        不调用 Agent Brain，不消耗 token。
        """
        start_time = time.time()
        overrides: Dict[str, dict] = {}

        # 📘 构建工具上下文（所有工具共享）
        context = {
            "source_path": source_path,
            "parsed_data": parsed_data,
            "translations": translations,
            "overrides": overrides,
            "translate_pipeline": self.translate_pipeline,
            "format_engine": self.format_engine,
        }

        # 📘 初始化工具
        tools = {
            "measure_overflow": MeasureOverflowTool(context=context),
            "resize_font": ResizeFontTool(context=context),
            "retranslate_shorter": RetranslateShorterTool(context=context),
            "render_page_preview": RenderPageTool(context=context),
            "save_layout_rule": SaveLayoutRuleTool(context=context),
        }

        # 📘 动态工具系统：加载已有 + 注册创建工具
        self.dynamic_registry = DynamicToolRegistry()
        dynamic_tools = self.dynamic_registry.load_tools(context=context)
        if dynamic_tools:
            tools.update(dynamic_tools)
            logger.info(f"已加载 {len(dynamic_tools)} 个动态工具")

        # 📘 注册 create_custom_tool（让 Brain 可以创建新工具）
        if self.brain_engine:
            tools["create_custom_tool"] = CreateCustomToolTool(
                registry=self.dynamic_registry, context=context,
            )

        # ── 零成本快速路径：纯算法检测 ──
        measure_result = tools["measure_overflow"].execute({"page_index": -1})
        measure_data = json.loads(measure_result)
        summary = measure_data.get("summary", {})

        if summary.get("overflow", 0) == 0 and summary.get("tight", 0) == 0:
            logger.info("排版检测通过：无溢出，跳过 Agent 审查")
            print("  [✅ 排版检测] 无溢出，跳过排版修正", flush=True)
            return translations, overrides

        overflow_count = summary.get("overflow", 0)
        tight_count = summary.get("tight", 0)
        logger.info(
            f"排版检测：{overflow_count} 个溢出 + {tight_count} 个偏紧，"
            f"启动 Layout Agent"
        )
        print(
            f"  [🔍 排版检测] {overflow_count} 个溢出 + {tight_count} 个偏紧，"
            f"启动排版修正...",
            flush=True,
        )

        # ── Agent Brain 不可用时：纯算法自动修正 ──
        if not self.brain_engine:
            logger.info("Agent Brain 不可用，使用纯算法自动修正")
            self._auto_fix_overflow(measure_data, tools, target_lang)
            elapsed = round(time.time() - start_time, 1)
            print(f"  [✅ 排版修正] 纯算法修正完成，耗时 {elapsed}s", flush=True)
            return translations, overrides

        # ── Agent Brain ReAct 循环 ──
        self._agent_fix_loop(tools, context, target_lang)

        elapsed = round(time.time() - start_time, 1)
        fixed = self.stats["fixed_count"]
        rounds = self.stats["rounds"]
        print(
            f"  [✅ 排版修正] {fixed} 个文本块已修正，"
            f"{rounds} 轮迭代，耗时 {elapsed}s",
            flush=True,
        )

        return translations, overrides

    def _auto_fix_overflow(
        self,
        measure_data: dict,
        tools: dict,
        target_lang: str,
    ):
        """
        📘 纯算法自动修正（Agent Brain 不可用时的 fallback）。

        策略简单但有效：
        - overflow_ratio <= 1.3（偏紧）：缩小到 85%
        - overflow_ratio <= 2.0（溢出）：缩小到 70%
        - overflow_ratio > 2.0（严重溢出）：重新翻译更短版本
        """
        items = measure_data.get("items", [])
        problem_items = [i for i in items if i["status"] in ("tight", "overflow")]

        if not problem_items:
            return

        # 📘 按严重程度分组
        resize_85_keys = []
        resize_70_keys = []
        retranslate_items = []

        for item in problem_items:
            ratio = item["overflow_ratio"]
            key = item["key"]
            if ratio <= 1.3:
                resize_85_keys.append(key)
            elif ratio <= 2.0:
                resize_70_keys.append(key)
            else:
                # 📘 严重溢出：估算目标字符数
                avail_parts = item.get("avail_size", "100x50").split("x")
                avail_w = float(avail_parts[0]) if len(avail_parts) == 2 else 100
                avail_h = float(avail_parts[1]) if len(avail_parts) == 2 else 50
                font_size = item.get("font_size", 10)
                chars_per_line = max(1, int(avail_w / (font_size * 0.55)))
                max_lines = max(1, int(avail_h / (font_size * 1.3)))
                max_chars = chars_per_line * max_lines
                retranslate_items.append({"key": key, "max_chars": max(10, max_chars)})

        # 📘 执行缩字
        if resize_85_keys:
            tools["resize_font"].execute({"keys": resize_85_keys, "scale": 0.85})
            self.stats["fixed_count"] += len(resize_85_keys)
            self.stats["tool_calls"] += 1

        if resize_70_keys:
            tools["resize_font"].execute({"keys": resize_70_keys, "scale": 0.70})
            self.stats["fixed_count"] += len(resize_70_keys)
            self.stats["tool_calls"] += 1

        # 📘 执行重新翻译
        if retranslate_items:
            tools["retranslate_shorter"].execute({
                "items": retranslate_items,
                "target_lang": target_lang,
            })
            self.stats["fixed_count"] += len(retranslate_items)
            self.stats["tool_calls"] += 1

        logger.info(
            f"纯算法修正: 缩85%={len(resize_85_keys)}, "
            f"缩70%={len(resize_70_keys)}, "
            f"重译={len(retranslate_items)}"
        )

    def _agent_fix_loop(
        self,
        tools: dict,
        context: dict,
        target_lang: str,
    ):
        """
        📘 Agent Brain ReAct 循环：让规划者自主决策排版修正。

        流程：
        1. 发送 measure_overflow 结果给 Brain
        2. Brain 返回 tool_call → 执行工具 → 反馈结果
        3. Brain 返回最终 JSON → 结束
        4. 每轮最多 max_tool_calls_per_round 次工具调用
        5. 最多 max_rounds 轮迭代

        📘 与 ScanAgent._process_single_page 的区别：
        ScanAgent 处理的是图片（vision），这里处理的是结构化数据。
        但 ReAct 循环的模式完全一样——这就是 Agent 架构的复用性。
        """
        # 📘 构建工具 schema（给 Brain 的 tools 参数）
        tool_schemas = [t.get_api_format() for t in tools.values()]

        # 📘 初始检测结果（已经在 review_and_fix 中做过了，这里重新获取最新状态）
        initial_measure = tools["measure_overflow"].execute({"page_index": -1})

        messages = [
            {"role": "system", "content": LAYOUT_AGENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"以下是翻译后 PDF 的排版检测结果，目标语言是{target_lang}。\n"
                    f"请分析溢出情况并修正。\n\n"
                    f"检测结果：\n{initial_measure}"
                ),
            },
        ]

        for round_idx in range(self.max_rounds):
            self.stats["rounds"] = round_idx + 1
            tool_calls_this_round = 0

            while tool_calls_this_round < self.max_tool_calls_per_round:
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
                            self.stats["brain_tokens"]["prompt"] += chunk.get("prompt_tokens", 0)
                            self.stats["brain_tokens"]["completion"] += chunk.get("completion_tokens", 0)
                except Exception as e:
                    logger.error(f"Layout Agent Brain 调用失败: {e}")
                    # 📘 Brain 失败时回退到纯算法
                    measure_data = json.loads(initial_measure)
                    self._auto_fix_overflow(measure_data, tools, target_lang)
                    return

                # 📘 情况1：Brain 返回了工具调用
                if tool_calls_in_turn:
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
                        # 📘 Gemini thought_signature 回传（同 ScanAgent）
                        if "extra_content" in tc:
                            tc_entry["extra_content"] = tc["extra_content"]
                        assistant_msg["tool_calls"].append(tc_entry)
                    messages.append(assistant_msg)

                    # 📘 逐个执行工具
                    for tc in tool_calls_in_turn:
                        tool_name = tc["name"]
                        tool_call_id = tc["id"]
                        tool_calls_this_round += 1
                        self.stats["tool_calls"] += 1

                        try:
                            tool_params = json.loads(tc["arguments"])
                        except json.JSONDecodeError:
                            tool_params = {}

                        logger.info(
                            f"Layout Agent: 调用 {tool_name} "
                            f"(轮次 {round_idx + 1}, 第 {tool_calls_this_round} 次)"
                        )

                        if tool_name in tools:
                            tool_result = tools[tool_name].execute(tool_params)
                        else:
                            # 📘 检查是否是刚创建的动态工具
                            dynamic_tool = self.dynamic_registry.get_tool(tool_name) if hasattr(self, 'dynamic_registry') else None
                            if dynamic_tool:
                                tools[tool_name] = dynamic_tool
                                tool_result = dynamic_tool.execute(tool_params)
                                # 📘 动态工具创建后需要更新 tool_schemas
                                tool_schemas = [t.get_api_format() for t in tools.values()]
                            else:
                                tool_result = json.dumps(
                                    {"error": f"未知工具: {tool_name}"},
                                    ensure_ascii=False,
                                )

                        # 📘 统计修正数量（resize_font 和 retranslate_shorter）
                        if tool_name in ("resize_font", "retranslate_shorter"):
                            try:
                                result_data = json.loads(tool_result)
                                count = result_data.get("adjusted", 0) or result_data.get("retranslated", 0)
                                self.stats["fixed_count"] += count
                            except Exception:
                                pass

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": tool_result,
                        })

                    continue  # 继续 ReAct 循环

                # 📘 情况2：Brain 返回纯文本（最终结果）
                if text_in_turn:
                    # 📘 尝试解析最终 JSON
                    try:
                        result = json.loads(text_in_turn.strip())
                        if result.get("status") == "done":
                            logger.info(
                                f"Layout Agent 完成: {result.get('summary', '')}"
                            )
                            return
                    except json.JSONDecodeError:
                        pass
                    # 📘 不是有效的结束 JSON，可能是思考过程，继续
                    logger.debug(f"Layout Agent 文本输出: {text_in_turn[:200]}")
                    break  # 跳出内层循环，进入下一轮

            # 📘 达到单轮工具调用上限
            if tool_calls_this_round >= self.max_tool_calls_per_round:
                logger.info(
                    f"Layout Agent 轮次 {round_idx + 1} 达到工具调用上限 "
                    f"{self.max_tool_calls_per_round}"
                )

        logger.info(f"Layout Agent 达到最大轮数 {self.max_rounds}，结束")

