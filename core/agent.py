# core/agent.py
import json
from typing import Generator, List
from core.llm_engine import ArkLLMEngine
from core.memory import ConversationMemory
from tools.base_tool import BaseTool

class BaseAgent:
    def __init__(self, llm_engine: ArkLLMEngine, tools: List[BaseTool]):
        self.llm = llm_engine
        self.tools_map = {tool.name: tool for tool in tools}
        self.api_tools =[tool.get_api_format() for tool in tools]
        
        simple_prompt = "你是一个聪明的 AI 助手。你可以使用工具来帮助用户解决问题。如果有工具，请优先考虑使用工具解决。"
        self.memory = ConversationMemory(system_prompt=simple_prompt)
        
        # 【新增】：更精细的全局 Token 账本
        self.total_tokens = 0
        self.total_prompt_tokens = 0      # 累计输入
        self.total_completion_tokens = 0  # 累计输出

    def chat(self, user_input: str) -> Generator[str, None, None]:
        self.memory.add_user_message(user_input)

        # 调试信息打印...
        print("\n" + "▼"*20 + "[Debug: 真正发送给模型的上下文] " + "▼"*20)
        print(self.memory.get_debug_info())
        print("▲"*65 + "\n")

        
        # 【新增】：本次对话的精细 Token 账本
        turn_tokens = 0 
        turn_prompt_tokens = 0
        turn_completion_tokens = 0

        while True:
            full_response = ""
            tool_calls_this_turn =[]
            
            for chunk in self.llm.stream_chat(self.memory.get_messages(), tools=self.api_tools):
                if chunk["type"] == "text":
                    full_response += chunk["content"]
                    yield chunk["content"]
                elif chunk["type"] == "tool_call":
                    tool_calls_this_turn.append(chunk)
                # 【核心修改】：把输入和输出的 Token 分别记账
                elif chunk["type"] == "usage":
                    turn_prompt_tokens += chunk["prompt_tokens"]
                    turn_completion_tokens += chunk["completion_tokens"]
                    turn_tokens += chunk["total_tokens"]
                    
                    self.total_prompt_tokens += chunk["prompt_tokens"]
                    self.total_completion_tokens += chunk["completion_tokens"]
                    self.total_tokens += chunk["total_tokens"]

            if tool_calls_this_turn:
                tool_results_str = ""
                
                for tc in tool_calls_this_turn:
                    action_name = tc["name"]
                    action_args_str = tc["arguments"]
                    
                    yield f"\n\n⚙️[原生 API 动作]: 模型请求调用[{action_name}]，参数: {action_args_str}...\n"
                    
                    try:
                        action_params = json.loads(action_args_str)
                    except json.JSONDecodeError:
                        action_params = {}
                        
                    if action_name in self.tools_map:
                        tool_result = self.tools_map[action_name].execute(action_params) 
                    else:
                        tool_result = f"未知的工具: {action_name}"
                        
                    yield f" 拿到结果：{tool_result}\n"
                    tool_results_str += f"调用 [{action_name}] 参数 {action_args_str} 的结果是: {tool_result}\n"
                
                yield "\n🧠[Agent]: 正在综合处理所有结果...\n\n"
                
                self.memory.add_ai_message(f"(内部记录: 我并发调用了 {len(tool_calls_this_turn)} 个工具)")
                self.memory.add_user_message(f"系统返回了工具调用的汇总结果：\n{tool_results_str}\n请基于上述所有结果回答我。")
                continue 

            if not tool_calls_this_turn:
                self.memory.add_ai_message(full_response)
                
                # 【核心修改】：打印超级详细的账单
                yield f"\n\n[📊 Token 消耗] 本次消耗: {turn_tokens} (输入: {turn_prompt_tokens}, 输出: {turn_completion_tokens}) | 累计消耗: {self.total_tokens} (输入: {self.total_prompt_tokens}, 输出: {self.total_completion_tokens})\n"
                break