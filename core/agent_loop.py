# core/agent_loop.py
# =============================================================
# 📘 教学笔记：核心 Agent Loop
# =============================================================
# 借鉴 Claude Code 的设计哲学：
#   一个简单的 while(tool_call) 循环 + 丰富的工具集 = 可控的自主性
#
# 这是整个翻译 Agent 的心脏。模型自己决定：
#   - 调什么工具
#   - 什么顺序
#   - 什么时候停
#
# 我们只提供工具和目标，不规定步骤。
# =============================================================

import json
import queue
import threading
import time
from typing import Any, Callable, Dict, Generator, List, Optional

from core.logger import get_logger

logger = get_logger("agent_loop")

MAX_TURNS = 50  # 单次任务最大循环次数，防止失控
CONTEXT_COMPRESS_THRESHOLD = 0.85  # 消息历史占 context window 的比例超过此值时压缩
ESTIMATED_CONTEXT_WINDOW = 128000  # 估算的 context window 大小（tokens）
CHARS_PER_TOKEN = 3.5  # 粗略估算：平均每个 token 约 3.5 个字符


class MessageQueue:
    """
    📘 教学笔记：用户消息异步注入队列（h2A 机制）

    Agent Loop 跑在后台线程，用户随时可以从 GUI 线程注入消息。
    Agent 每轮循环开始时检查队列，有新消息就先处理。
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()

    def inject(self, message: str):
        """GUI 线程调用：用户发送消息"""
        self._queue.put(message)

    def has_pending(self) -> bool:
        return not self._queue.empty()

    def pop(self) -> Optional[str]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def clear(self):
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


class BaseTool:
    """
    📘 教学笔记：工具基类

    每个工具只需要定义：
    - name: 工具名
    - description: 功能描述（给模型看）
    - parameters: JSON Schema（给模型看）
    - execute(params) -> str: 执行逻辑（返回字符串结果）
    """
    name: str = ""
    description: str = ""
    parameters: dict = {}

    def get_schema(self) -> dict:
        """返回 OpenAI function calling 格式的 schema"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, params: dict) -> str:
        raise NotImplementedError


class AgentLoop:
    """
    📘 教学笔记：核心 Agent Loop

    while(tool_call) -> execute -> feed back -> repeat

    这就是全部。没有 if/else 判断文件类型，没有硬编码步骤。
    模型看到工具列表和用户需求后，自己决定怎么做。
    """

    def __init__(
        self,
        llm_engine,
        tools: List[BaseTool],
        system_prompt: str,
        max_turns: int = MAX_TURNS,
        on_message: Callable[[str, str], None] = None,
        on_tool_call: Callable[[str, dict], None] = None,
        on_token_update: Callable[[dict], None] = None,
        skill_loader=None,
    ):
        """
        📘 参数：
        - llm_engine: ExternalLLMEngine 实例（支持 stream_chat + tools）
        - tools: 工具列表
        - system_prompt: 系统提示词（给目标，不给步骤）
        - max_turns: 最大循环次数
        - on_message: 回调，Agent 输出文本时通知 GUI (role, content)
        - on_tool_call: 回调，Agent 调用工具时通知 GUI (tool_name, params)
        """
        self.llm_engine = llm_engine
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.on_message = on_message
        self.on_tool_call = on_tool_call
        self.on_token_update = on_token_update
        self.skill_loader = skill_loader

        # 📘 工具注册表
        self.tools: Dict[str, BaseTool] = {}
        self.tool_schemas: List[dict] = []
        for tool in tools:
            self.tools[tool.name] = tool
            # 📘 兼容旧工具（get_api_format）和新工具（get_schema）
            if hasattr(tool, 'get_schema'):
                self.tool_schemas.append(tool.get_schema())
            elif hasattr(tool, 'get_api_format'):
                self.tool_schemas.append(tool.get_api_format())
            else:
                self.tool_schemas.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                })

        # 📘 扁平消息历史（Claude Code 风格）
        self.messages: List[dict] = [
            {"role": "system", "content": system_prompt},
        ]

        # 📘 用户消息队列（交互式）
        self.message_queue = MessageQueue()

        # 📘 停止标志
        self._stop_event = threading.Event()

        # 📘 统计
        self.stats = {
            "turns": 0,
            "tool_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

        logger.info(
            f"AgentLoop 初始化: {len(self.tools)} 个工具, "
            f"max_turns={max_turns}"
        )

    def _estimate_tokens(self) -> int:
        """
        📘 粗略估算当前消息历史的 token 数

        精确计算需要 tokenizer，这里用字符数 / 3.5 粗略估算。
        中文字符密度更高（约 1.5 token/字），英文约 0.75 token/word。
        取平均值 3.5 chars/token 作为折中。
        """
        total_chars = 0
        for msg in self.messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                # 多模态消息（图片+文本）
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total_chars += len(part.get("text", ""))
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        total_chars += 1000  # 图片大约占 ~1000 tokens
            # tool_calls 也占 token
            if "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    total_chars += len(fn.get("name", ""))
                    total_chars += len(fn.get("arguments", ""))
        return int(total_chars / CHARS_PER_TOKEN)

    def _maybe_compress(self):
        """
        📘 教学笔记：Context Window 压缩（借鉴 Claude Code 的 Compressor）

        当消息历史接近 token 上限时，用 LLM 生成结构化摘要。
        不是简单的字符截断，而是让 LLM 理解对话内容后生成高质量摘要。

        摘要包含（参考 Claude Code 的 compact prompt）：
        1. 用户的请求和意图
        2. 关键技术细节（文件名、翻译术语）
        3. 已完成的工作
        4. 遇到的错误和修复
        5. 用户的所有反馈（原文）
        6. 待办任务
        7. 当前正在做的工作
        """
        estimated = self._estimate_tokens()
        threshold = int(ESTIMATED_CONTEXT_WINDOW * CONTEXT_COMPRESS_THRESHOLD)

        if estimated < threshold:
            return  # 还没到压缩阈值

        logger.info(
            f"Context 接近上限: ~{estimated} tokens "
            f"(阈值 {threshold})，开始压缩"
        )

        keep_recent = 10
        if len(self.messages) <= keep_recent + 2:
            return

        system_msg = self.messages[0]
        old_messages = self.messages[1:-keep_recent]
        recent_messages = self.messages[-keep_recent:]

        # 📘 用 LLM 生成结构化摘要
        summary = self._llm_compress(old_messages)

        compressed_msg = {
            "role": "user",
            "content": f"[以下是之前对话的结构化摘要]\n{summary}\n[摘要结束，请继续任务]",
        }

        self.messages = [system_msg, compressed_msg] + recent_messages
        new_estimated = self._estimate_tokens()
        logger.info(
            f"Context 压缩完成: {estimated} -> ~{new_estimated} tokens, "
            f"压缩了 {len(old_messages)} 条消息"
        )

    def run(self, user_message: str) -> str:
        """
        📘 核心方法：执行一次完整的 Agent 任务

        用户说什么，Agent 就去做。返回 Agent 的最终文本回复。
        """
        self.messages.append({"role": "user", "content": user_message})
        self._notify_message("user", user_message)

        final_text = ""

        for turn in range(self.max_turns):
            if self._stop_event.is_set():
                logger.info("收到停止信号，Agent 退出")
                break

            # 📘 Step 1: 检查 context window 是否需要压缩
            self._maybe_compress()

            # 📘 Step 2: 检查用户是否有新消息（交互式）
            while self.message_queue.has_pending():
                new_msg = self.message_queue.pop()
                if new_msg:
                    logger.info(f"收到用户新消息: {new_msg[:100]}")
                    self.messages.append({"role": "user", "content": new_msg})
                    self._notify_message("user", new_msg)

            # 📘 Step 3: 调用模型
            tool_calls = []
            text_content = ""

            try:
                for chunk in self.llm_engine.stream_chat(
                    self.messages,
                    tools=self.tool_schemas if self.tool_schemas else None,
                    max_tokens=16384,
                ):
                    if chunk["type"] == "text":
                        text_content += chunk["content"]
                    elif chunk["type"] == "tool_call":
                        tool_calls.append(chunk)
                    elif chunk["type"] == "usage":
                        self.stats["prompt_tokens"] += chunk.get("prompt_tokens", 0)
                        self.stats["completion_tokens"] += chunk.get("completion_tokens", 0)
                        self._notify_token_update()
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}")
                error_msg = f"模型调用出错: {type(e).__name__}: {e}"
                self._notify_message("assistant", error_msg)
                break

            self.stats["turns"] += 1

            # 📘 Step 4: 模型要调工具 -> 执行 -> 反馈 -> 继续循环
            if tool_calls:
                # 把 assistant 消息（含 tool_calls）加入历史
                assistant_msg = {"role": "assistant", "content": text_content or None}
                assistant_msg["tool_calls"] = []
                for tc in tool_calls:
                    tc_entry = {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    if "extra_content" in tc:
                        tc_entry["extra_content"] = tc["extra_content"]
                    assistant_msg["tool_calls"].append(tc_entry)
                self.messages.append(assistant_msg)

                # 逐个执行工具
                for tc in tool_calls:
                    tool_name = tc["name"]
                    tool_call_id = tc["id"]

                    try:
                        tool_params = json.loads(tc["arguments"])
                    except json.JSONDecodeError:
                        tool_params = {}

                    logger.info(f"[turn {turn+1}] 调用工具: {tool_name}")
                    self._notify_tool_call(tool_name, tool_params)
                    self.stats["tool_calls"] += 1

                    # 执行工具
                    if tool_name in self.tools:
                        try:
                            tool_result = self.tools[tool_name].execute(tool_params)
                        except Exception as e:
                            logger.error(f"工具 {tool_name} 执行失败: {e}")
                            tool_result = json.dumps(
                                {"error": f"{type(e).__name__}: {e}"},
                                ensure_ascii=False,
                            )
                    else:
                        tool_result = json.dumps(
                            {"error": f"未知工具: {tool_name}"},
                            ensure_ascii=False,
                        )

                    # 📘 借鉴 Claude Code：工具结果预算
                    # 限制单条工具结果的大小，防止一个巨大的返回撑爆 context。
                    # 但图片类工具（base64）不截断 — 截断后图片不可用。
                    MAX_TOOL_RESULT_CHARS = 30000  # 约 8500 tokens
                    SKIP_TRUNCATE_TOOLS = {"get_page_image", "render_slide"}
                    if (
                        isinstance(tool_result, str)
                        and len(tool_result) > MAX_TOOL_RESULT_CHARS
                        and tool_name not in SKIP_TRUNCATE_TOOLS
                    ):
                        original_len = len(tool_result)
                        tool_result = (
                            tool_result[:MAX_TOOL_RESULT_CHARS]
                            + f"\n...[结果被截断: 原始 {original_len} 字符, "
                            f"保留前 {MAX_TOOL_RESULT_CHARS} 字符]"
                        )
                        logger.info(
                            f"工具 {tool_name} 结果截断: "
                            f"{original_len} -> {MAX_TOOL_RESULT_CHARS} 字符"
                        )

                    # 反馈给模型
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": tool_result,
                    })

                    # 📘 Skill 按需加载：工具返回后检查是否需要注入 Skill
                    if self.skill_loader and tool_name == "parse_document":
                        self._inject_skills_from_parse(tool_result)
                    elif self.skill_loader and tool_name == "write_document":
                        self._inject_skills({"phase": "after_write"})

                continue  # 继续循环

            # 📘 Step 5: 模型返回纯文本 -> 可能完成，也可能被截断
            if text_content:
                # 📘 借鉴 Claude Code：检测输出截断并自动恢复
                # 如果 completion_tokens 接近 max_tokens，说明输出被截断了
                # 注入恢复消息让模型接着说，不要道歉不要重复
                completion_tokens = self.stats.get("completion_tokens", 0)
                if completion_tokens > 14000 and not tool_calls:
                    logger.info(
                        f"检测到输出可能被截断 "
                        f"(completion_tokens={completion_tokens})，注入恢复消息"
                    )
                    self.messages.append({"role": "assistant", "content": text_content})
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "输出被截断了。直接从断点继续，"
                            "不要道歉，不要重复已经说过的内容，"
                            "把剩余工作拆成更小的步骤完成。"
                        ),
                    })
                    self._notify_message("system", "输出被截断，自动恢复中...")
                    continue

                final_text = text_content
                self._notify_message("assistant", text_content)

                # 📘 教学笔记：任务完成后进入待命模式
                # 用户可能要打开文件看看效果，然后提修改意见。
                # 持续轮询消息队列，直到超时（5分钟没消息就真正退出）。
                import time
                idle_timeout = 300  # 5 分钟无消息则退出
                idle_start = time.time()
                while not self._stop_event.is_set():
                    if self.message_queue.has_pending():
                        # 用户发了新消息，跳出待命，回到主循环处理
                        logger.info("待命期间收到用户新消息，继续处理")
                        break
                    if time.time() - idle_start > idle_timeout:
                        logger.info(f"待命 {idle_timeout}s 无新消息，Agent 退出")
                        break
                    time.sleep(0.5)  # 每 0.5 秒检查一次
                else:
                    break  # stop_event 被设置，退出

                # 如果是超时退出，真正结束
                if not self.message_queue.has_pending():
                    break
                # 否则继续主循环（下一轮会读到用户消息）

        # 📘 达到上限
        if not final_text and not self._stop_event.is_set():
            logger.warning(f"Agent 达到 {self.max_turns} 轮上限")
            final_text = "任务未在限定轮次内完成。"
            self._notify_message("assistant", final_text)

        logger.info(
            f"Agent 完成: {self.stats['turns']} 轮, "
            f"{self.stats['tool_calls']} 次工具调用, "
            f"tokens: {self.stats['prompt_tokens']}+{self.stats['completion_tokens']}"
        )
        return final_text

    def stop(self):
        """优雅停止"""
        self._stop_event.set()

    def reset(self):
        """重置状态（新任务）"""
        self._stop_event.clear()
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.message_queue.clear()
        self.stats = {
            "turns": 0, "tool_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
        }

    def _notify_message(self, role: str, content: str):
        if self.on_message:
            try:
                self.on_message(role, content)
            except Exception:
                pass

    def _notify_tool_call(self, tool_name: str, params: dict):
        if self.on_tool_call:
            try:
                self.on_tool_call(tool_name, params)
            except Exception:
                pass

    def _notify_token_update(self):
        if self.on_token_update:
            try:
                self.on_token_update(self.stats)
            except Exception:
                pass

    # 📘 借鉴 Claude Code 的 compact prompt 结构
    COMPACT_PROMPT = (
        "请为以下对话生成结构化摘要。这个摘要将替代原始对话，"
        "所以必须保留所有关键信息。不要调用任何工具，只输出纯文本。\n\n"
        "摘要必须包含以下部分：\n"
        "1. 用户请求：用户要求做什么（原文引用关键需求）\n"
        "2. 已完成工作：已经完成了哪些步骤，输出了什么文件\n"
        "3. 翻译术语：已确定的术语对照（原文=译文）\n"
        "4. 用户反馈：用户提出的所有修改意见和偏好（必须原文保留）\n"
        "5. 遇到的问题：出现过什么错误，怎么解决的\n"
        "6. 待办事项：还有什么没做完的\n"
        "7. 当前状态：最后在做什么，做到哪一步了\n\n"
        "对话内容：\n"
    )

    def _llm_compress(self, old_messages: list) -> str:
        """
        📘 用 LLM 生成结构化摘要（借鉴 Claude Code 的 Compressor）

        把旧消息发给 LLM，让它生成高质量的结构化摘要。
        如果 LLM 调用失败，降级为简单的文本截断。
        """
        # 📘 构建对话文本（只提取文本内容，跳过图片和大型工具结果）
        conversation_parts = []
        for msg in old_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                if isinstance(content, str):
                    conversation_parts.append(f"用户: {content[:500]}")
                elif isinstance(content, list):
                    texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                    if texts:
                        conversation_parts.append(f"用户: {' '.join(texts)[:500]}")
            elif role == "assistant":
                if content:
                    conversation_parts.append(f"助手: {content[:500]}")
            elif role == "tool":
                if isinstance(content, str) and len(content) > 200:
                    conversation_parts.append(f"工具结果: {content[:200]}...")
                elif isinstance(content, str):
                    conversation_parts.append(f"工具结果: {content}")

        conversation_text = "\n".join(conversation_parts)

        # 📘 限制发给压缩 LLM 的文本量（避免压缩本身超 token）
        if len(conversation_text) > 15000:
            conversation_text = conversation_text[:15000] + "\n...(截断)"

        compress_prompt = self.COMPACT_PROMPT + conversation_text

        try:
            summary_text = ""
            for chunk in self.llm_engine.stream_chat(
                [{"role": "user", "content": compress_prompt}],
                max_tokens=2048,
            ):
                if chunk["type"] == "text":
                    summary_text += chunk["content"]
                elif chunk["type"] == "usage":
                    # 压缩的 token 也计入统计
                    self.stats["prompt_tokens"] += chunk.get("prompt_tokens", 0)
                    self.stats["completion_tokens"] += chunk.get("completion_tokens", 0)

            if summary_text.strip():
                logger.info(f"LLM 压缩完成: {len(summary_text)} 字符")
                return summary_text.strip()
        except Exception as e:
            logger.warning(f"LLM 压缩失败，降级为简单截断: {e}")

        # 📘 降级：简单截断
        fallback_parts = []
        for msg in old_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and isinstance(content, str):
                fallback_parts.append(content[:200])
            elif role == "assistant" and content:
                fallback_parts.append(content[:200])
        return " | ".join(fallback_parts[-5:]) or f"(已压缩 {len(old_messages)} 条消息)"

    def _inject_skills_from_parse(self, parse_result: str):
        """📘 从 parse_document 结果中提取 doc_type，加载匹配的 Skill"""
        try:
            data = json.loads(parse_result)
            doc_type = data.get("doc_type", "")
            if doc_type:
                self._inject_skills({"doc_type": doc_type})
        except (json.JSONDecodeError, Exception):
            pass

    def _inject_skills(self, context: dict):
        """📘 根据上下文匹配并注入 Skill 到消息历史"""
        if not self.skill_loader:
            return
        matched = self.skill_loader.match_skills(context)
        for skill in matched:
            skill_text = self.skill_loader.load_skill(skill)
            # 📘 作为 system 级消息注入，模型会当作重要指令
            self.messages.append({
                "role": "user",
                "content": f"[系统加载了专业技能包: {skill.name}]\n{skill_text}",
            })
            logger.info(f"注入 Skill: {skill.name}")
