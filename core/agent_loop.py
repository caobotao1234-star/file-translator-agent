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

        # 📘 工具注册表
        self.tools: Dict[str, BaseTool] = {}
        self.tool_schemas: List[dict] = []
        for tool in tools:
            self.tools[tool.name] = tool
            self.tool_schemas.append(tool.get_schema())

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

            # 📘 Step 1: 检查用户是否有新消息（交互式）
            while self.message_queue.has_pending():
                new_msg = self.message_queue.pop()
                if new_msg:
                    logger.info(f"收到用户新消息: {new_msg[:100]}")
                    self.messages.append({"role": "user", "content": new_msg})
                    self._notify_message("user", new_msg)

            # 📘 Step 2: 调用模型
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
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}")
                error_msg = f"模型调用出错: {type(e).__name__}: {e}"
                self._notify_message("assistant", error_msg)
                break

            self.stats["turns"] += 1

            # 📘 Step 3: 模型要调工具 -> 执行 -> 反馈 -> 继续循环
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

                continue  # 继续循环

            # 📘 Step 4: 模型返回纯文本 -> 任务完成
            if text_content:
                final_text = text_content
                self._notify_message("assistant", text_content)
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
