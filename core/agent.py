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

    def chat(self, user_input: str) -> Generator[str, None, None]:
        self.memory.add_user_message(user_input)

        while True:
            full_response = ""
            # 【核心修改】：创建一个箱子，装下这一轮所有的工具请求
            tool_calls_this_turn =[]
            
            for chunk in self.llm.stream_chat(self.memory.get_messages(), tools=self.api_tools):
                if chunk["type"] == "text":
                    full_response += chunk["content"]
                    yield chunk["content"]
                elif chunk["type"] == "tool_call":
                    tool_calls_this_turn.append(chunk)

            # 如果这轮有工具请求（可能是一个，也可能是多个并发）
            if tool_calls_this_turn:
                tool_results_str = ""
                
                # 排队挨个执行工具
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
                    # 把每个工具的结果记录到小本本上
                    tool_results_str += f"调用 [{action_name}] 参数 {action_args_str} 的结果是: {tool_result}\n"
                
                yield "\n🧠[Agent]: 正在综合处理所有结果...\n\n"
                
                # 一次性把所有的结果告诉大模型
                self.memory.add_ai_message(f"(内部记录: 我并发调用了 {len(tool_calls_this_turn)} 个工具)")
                self.memory.add_user_message(f"系统返回了工具调用的汇总结果：\n{tool_results_str}\n请基于上述所有结果回答我。")
                continue # 进入下一轮循环，让模型生成最终回答

            # 如果没有任何工具被调用，说明对话结束，退出循环
            if not tool_calls_this_turn:
                self.memory.add_ai_message(full_response)
                break