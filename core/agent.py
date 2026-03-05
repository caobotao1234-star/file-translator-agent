import json
import re # 引入正则库
from typing import Generator, List
from core.llm_engine import ArkLLMEngine
from core.memory import ConversationMemory
from tools.base_tool import BaseTool
from prompts.system_prompts import AGENT_SYSTEM_PROMPT_TEMPLATE

class BaseAgent:
    def __init__(self, llm_engine: ArkLLMEngine, tools: List[BaseTool]):
        self.llm = llm_engine
        
        # 1. 组装工具字典，方便后续根据 name 快速查找: {"get_time": TimeTool()}
        self.tools_map = {tool.name: tool for tool in tools}
        
        # 2. 动态生成工具说明书
        tool_desc_list =[]
        for i, tool in enumerate(tools):
            tool_desc_list.append(f"{i+1}. action: '{tool.name}'\n   - 功能与参数: {tool.description}")
        tool_descriptions_str = "\n".join(tool_desc_list)
        
        # 3. 将说明书填入 Prompt 模板
        final_system_prompt = AGENT_SYSTEM_PROMPT_TEMPLATE.format(
            tool_descriptions=tool_descriptions_str
        )
        
        # 4. 初始化带有完整动态 Prompt 的记忆库
        self.memory = ConversationMemory(system_prompt=final_system_prompt)

    def chat(self, user_input: str) -> Generator[str, None, None]:
        self.memory.add_user_message(user_input)

        # 调试信息打印...
        print("\n" + "▼"*20 + "[Debug: 真正发送给模型的上下文] " + "▼"*20)
        print(self.memory.get_debug_info())
        print("▲"*65 + "\n")

        while True:
            full_response = ""
            for chunk in self.llm.stream_chat(self.memory.get_messages()):
                full_response += chunk
                yield chunk 
                
            self.memory.add_ai_message(full_response)

            # ==========================================
            # 🔧 [强力正则解析器]：从混合文本中提取 JSON
            # ==========================================
            # 匹配 ```json ... ``` 中的内容，re.DOTALL 让 . 可以匹配换行符
            match = re.search(r'```json\s*(.*?)\s*```', full_response, re.DOTALL)
            
            tool_call_dict = None
            if match:
                json_str = match.group(1).strip()
                try:
                    tool_call_dict = json.loads(json_str)
                except json.JSONDecodeError:
                    yield "\n\n⚠️ [系统警告]: 模型输出了不合法的 JSON 格式，无法解析。\n"
                    break # 退出当前思考循环

            # 如果成功解析出字典，且包含 action
            if tool_call_dict and isinstance(tool_call_dict, dict) and "action" in tool_call_dict:
                action_name = tool_call_dict["action"]
                action_params = tool_call_dict.get("action_input", {})

                yield f"\n\n🛠️ [系统动作]: 请求调用[{action_name}]，参数: {action_params}...\n"
                
                if action_name in self.tools_map:
                    tool_instance = self.tools_map[action_name]
                    tool_result = tool_instance.execute(action_params) 
                else:
                    tool_result = f"错误：未知的工具 '{action_name}'"
                
                yield f" 拿到结果：{tool_result}\n"
                
                tool_feedback = f"【系统内部反馈】工具执行完毕。结果是：{tool_result}。请基于此结果回答。"
                self.memory.add_user_message(tool_feedback)
                
                yield "🧠 [Agent 思考]: 已将结果存入记忆，正在生成最终回答...\n\n"
                continue # 继续死循环，让大模型读入结果后重新输出
            else:
                # 如果没有匹配到包含 action 的 JSON，说明模型认为不需要调用工具，或者回答完毕
                break