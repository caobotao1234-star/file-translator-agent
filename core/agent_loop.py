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
        📘 教学笔记：Context Window 压缩（参考 Claude Code 的 Compressor）

        当消息历史接近 token 上限时，自动压缩：
        1. 保留 system prompt（第一条消息）
        2. 保留最近 N 轮对话
        3. 中间的旧消息压缩为摘要

        关键信息（术语表、用户偏好）应该已经通过 memory 工具外置，
        不怕被压缩丢失。
        """
        estimated = self._estimate_tokens()
        threshold = int(ESTIMATED_CONTEXT_WINDOW * CONTEXT_COMPRESS_THRESHOLD)

        if estimated < threshold:
            return  # 还没到压缩阈值

        logger.info(
            f"Context 接近上限: ~{estimated} tokens "
            f"(阈值 {threshold})，开始压缩"
        )

        # 📘 压缩策略：保留 system prompt + 最近 10 条消息
        # 中间的消息替换为一条摘要
        keep_recent = 10
        if len(self.messages) <= keep_recent + 2:
            return  # 消息太少，不需要压缩

        system_msg = self.messages[0]  # system prompt
        old_messages = self.messages[1:-keep_recent]
        recent_messages = self.messages[-keep_recent:]

        # 📘 从旧消息中提取摘要
        summary_parts = []
        for msg in old_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "assistant" and content:
                # 只保留 assistant 的文本回复（不保留工具调用细节）
                summary_parts.append(content[:200])
            elif role == "tool":
                # 工具结果只保留前 100 字符
                if isinstance(content, str) and len(content) > 100:
                    summary_parts.append(f"[工具结果: {content[:100]}...]")

        summary = " | ".join(summary_parts[-5:])  # 最多保留 5 段摘要
        if not summary:
            summary = f"(已压缩 {len(old_messages)} 条旧消息)"

        compressed_msg = {
            "role": "user",
            "content": f"[上下文摘要: {summary}]\n请继续之前的任务。",
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

            # 📘 Step 5: 模型返回纯文本 -> 可能完成，也可能用户有后续
            if text_content:
                final_text = text_content
                self._notify_message("assistant", text_content)

                # 📘 教学笔记：不立即退出，检查用户是否有待处理的消息
                # 用户可能在 Agent 工作期间发了消息（修改需求、纠正翻译等）
                # 如果有待处理消息，继续循环让模型处理
                if self.message_queue.has_pending():
                    logger.info("Agent 输出了文本，但用户有待处理消息，继续循环")
                    continue

                # 📘 短暂等待：给用户一个窗口期发送后续指令
                # 等 3 秒，如果用户在这期间发了消息，继续处理
                import time
                time.sleep(3)
                if self.message_queue.has_pending():
                    logger.info("Agent 等待期间收到用户新消息，继续循环")
                    continue

                # 没有新消息，真正结束
                break

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
