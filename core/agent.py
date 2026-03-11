# core/agent.py
import json
from typing import Generator, List

from core.llm_engine import ArkLLMEngine, LLMRetryError
from core.memory import ConversationMemory
from core.agent_config import AgentConfig
from core.agent_events import AgentEvent
from core.storage import ChatStorage
from tools.base_tool import BaseTool
from prompts.system_prompts import AGENT_SYSTEM_PROMPT


class BaseAgent:
    def __init__(
        self,
        llm_engine: ArkLLMEngine,
        tools: List[BaseTool],
        config: AgentConfig | None = None,
        session_id: str | None = None,
    ):
        self.llm = llm_engine
        self.tools_map = {tool.name: tool for tool in tools}
        self.api_tools = [tool.get_api_format() for tool in tools]
        self.config = config or AgentConfig()

        # 📘 初始化存储引擎（如果开启了持久化）
        self.storage = None
        if self.config.enable_persistence:
            self.storage = ChatStorage(storage_dir=self.config.storage_dir)
            if session_id is None:
                session_id = ChatStorage.generate_session_id()

        self.session_id = session_id

        self.memory = ConversationMemory(
            system_prompt=AGENT_SYSTEM_PROMPT,
            llm_engine=self.llm,
            enable_summary=self.config.enable_memory_summary,
            debug=self.config.debug,
            storage=self.storage,
            session_id=self.session_id,
        )

        # 如果指定了 session_id，尝试恢复历史对话
        if self.storage and self.session_id:
            self.memory.load_from_storage()

        self.total_tokens = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def chat(self, user_input: str) -> Generator[AgentEvent, None, None]:
        self.memory.add_user_message(user_input)

        if self.config.debug:
            yield AgentEvent(
                type="debug",
                data={"messages": self.memory.get_debug_info()}
            )

        turn_tokens = 0
        turn_prompt_tokens = 0
        turn_completion_tokens = 0

        current_loop = 0
        while current_loop < self.config.max_loops:
            current_loop += 1

            full_response = ""
            tool_calls_this_turn = []

            # =============================================================
            # 📘 教学笔记：Agent 层的错误处理
            # =============================================================
            # LLM 引擎内部已经有了重试机制，但如果所有重试都失败了，
            # 会抛出 LLMRetryError。Agent 需要优雅地捕获它，
            # 把错误信息通过事件系统告诉用户，而不是让程序直接崩溃。
            #
            # 这就是"分层错误处理"的思想：
            #   - LLM 引擎层：负责重试（战术层面）
            #   - Agent 层：负责兜底和用户通知（战略层面）
            # =============================================================
            try:
                for chunk in self.llm.stream_chat(
                    self.memory.get_messages(),
                    tools=self.api_tools
                ):
                    if chunk["type"] == "text":
                        full_response += chunk["content"]
                        yield AgentEvent(type="text_delta", data={"content": chunk["content"]})

                    elif chunk["type"] == "tool_call":
                        tool_calls_this_turn.append(chunk)

                    elif chunk["type"] == "usage":
                        turn_prompt_tokens += chunk["prompt_tokens"]
                        turn_completion_tokens += chunk["completion_tokens"]
                        turn_tokens += chunk["total_tokens"]

                        self.total_prompt_tokens += chunk["prompt_tokens"]
                        self.total_completion_tokens += chunk["completion_tokens"]
                        self.total_tokens += chunk["total_tokens"]

            except LLMRetryError as e:
                error_msg = f"抱歉，AI 服务暂时不可用（{e}），请稍后再试。"
                self.memory.add_ai_message(error_msg)
                yield AgentEvent(type="error", data={"message": str(e)})
                yield AgentEvent(type="text_delta", data={"content": error_msg})
                yield AgentEvent(type="final", data={"content": error_msg})
                return

            if tool_calls_this_turn:
                api_tool_calls = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    for tc in tool_calls_this_turn
                ]

                self.memory.add_assistant_tool_call(
                    tool_calls=api_tool_calls,
                    content=full_response,
                )

                for tc in tool_calls_this_turn:
                    action_id = tc["id"]
                    action_name = tc["name"]
                    action_args_str = tc["arguments"]

                    yield AgentEvent(
                        type="tool_call",
                        data={
                            "id": action_id,
                            "name": action_name,
                            "arguments": action_args_str,
                        },
                    )

                    tool_result = ""
                    try:
                        action_params = json.loads(action_args_str) if action_args_str else {}

                        if action_name not in self.tools_map:
                            tool_result = f"系统错误：未知工具 '{action_name}'"
                        else:
                            tool = self.tools_map[action_name]
                            is_valid, error_msg = tool.validate_params(action_params)
                            if not is_valid:
                                tool_result = f"调用失败：{error_msg}"
                            else:
                                tool_result = tool.execute(action_params)

                    except json.JSONDecodeError:
                        tool_result = f"调用失败：参数不是合法 JSON ({action_args_str})"
                    except Exception as e:
                        tool_result = f"执行失败：{str(e)}"

                    self.memory.add_tool_message(
                        tool_call_id=action_id,
                        name=action_name,
                        content=str(tool_result),
                    )

                    yield AgentEvent(
                        type="tool_result",
                        data={
                            "id": action_id,
                            "name": action_name,
                            "result": str(tool_result),
                        },
                    )

                if current_loop == self.config.max_loops:
                    yield AgentEvent(
                        type="warning",
                        data={"message": "tool loop exceeded max_loops"}
                    )
                    self.memory.add_user_message(
                        "系统警告：工具调用次数已达上限，请基于现有信息给出最终结论。"
                    )
                else:
                    yield AgentEvent(
                        type="status",
                        data={"message": "processing_tool_results"}
                    )

                continue

            self.memory.add_ai_message(full_response)

            if self.config.show_usage:
                yield AgentEvent(
                    type="usage",
                    data={
                        "turn_tokens": turn_tokens,
                        "turn_prompt_tokens": turn_prompt_tokens,
                        "turn_completion_tokens": turn_completion_tokens,
                        "total_tokens": self.total_tokens,
                        "total_prompt_tokens": self.total_prompt_tokens,
                        "total_completion_tokens": self.total_completion_tokens,
                    },
                )

            yield AgentEvent(type="final", data={"content": full_response})

            # 📘 每轮对话结束后自动持久化
            self.memory.save_to_storage()
            break