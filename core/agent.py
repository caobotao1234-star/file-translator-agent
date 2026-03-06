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
        
        # 1. 拿到 API 标准格式的工具列表
        self.api_tools =[tool.get_api_format() for tool in tools]
        
        # 2. 极简的系统提示词，不再需要长篇大论教它写 JSON 了！
        simple_prompt = "你是一个聪明的 AI 助手。你可以使用工具来帮助用户解决问题。如果有工具，请优先考虑使用工具解决。"
        self.memory = ConversationMemory(system_prompt=simple_prompt)

    def chat(self, user_input: str) -> Generator[str, None, None]:
        self.memory.add_user_message(user_input)

        # 调试信息打印...
        print("\n" + "▼"*20 + "[Debug: 真正发送给模型的上下文] " + "▼"*20)
        print(self.memory.get_debug_info())
        print("▲"*65 + "\n")

        while True:
            full_response = ""
            tool_called = False
            
            # 将工具喂给底层引擎
            for chunk in self.llm.stream_chat(self.memory.get_messages(), tools=self.api_tools):
                # 收到纯文本，直接打印输出
                if chunk["type"] == "text":
                    full_response += chunk["content"]
                    yield chunk["content"]
                    
                # 收到工具调用指令
                elif chunk["type"] == "tool_call":
                    tool_called = True
                    action_name = chunk["name"]
                    action_args_str = chunk["arguments"]
                    
                    yield f"\n\n⚙️ [原生 API 动作]: 模型请求调用[{action_name}]，参数: {action_args_str}...\n"
                    
                    try:
                        action_params = json.loads(action_args_str)
                    except json.JSONDecodeError:
                        action_params = {}
                        
                    if action_name in self.tools_map:
                        tool_result = self.tools_map[action_name].execute(action_params) 
                    else:
                        tool_result = f"未知的工具: {action_name}"
                        
                    yield f" 拿到结果：{tool_result}\n🧠 [Agent]: 正在思考结果...\n\n"
                    
                    # 记住刚才的操作，告诉模型结果
                    self.memory.add_ai_message(f"(内部记录: 我请求调用了工具 {action_name})")
                    self.memory.add_user_message(f"系统返回的工具结果是：{tool_result}。请基于结果回答我最初的问题。")
                    break # 跳出 for 循环，进入外层的 while 重新请求模型

            # 如果本轮没有调用工具，说明对话正常结束
            if not tool_called:
                self.memory.add_ai_message(full_response)
                break