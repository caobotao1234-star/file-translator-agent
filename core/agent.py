import json
from typing import Generator
from core.llm_engine import ArkLLMEngine
from core.memory import ConversationMemory
# 导入我们所有的工具
from tools.basic_tools import get_current_time, get_weather 

class BaseAgent:
    def __init__(self, llm_engine: ArkLLMEngine, memory: ConversationMemory):
        self.llm = llm_engine
        self.memory = memory

    def chat(self, user_input: str) -> Generator[str, None, None]:
        self.memory.add_user_message(user_input)

        # 打印 Debug，观察记忆
        print("\n" + "▼"*20 + " [Debug: 真正发送给模型的上下文] " + "▼"*20)
        print(self.memory.get_debug_info())
        print("▲"*65 + "\n")

        while True:
            full_response = ""
            for chunk in self.llm.stream_chat(self.memory.get_messages()):
                full_response += chunk
                yield chunk 
                
            self.memory.add_ai_message(full_response)

            # ==========================================
            # 🔧 [Agent 核心逻辑]：JSON 结构化工具拦截器
            # ==========================================
            # 1. 清理模型输出，防止它自作聪明带上 Markdown 格式（如 ```json ... ```）
            clean_text = full_response.strip()
            if clean_text.startswith("```json"):
                clean_text = clean_text[7:]
            if clean_text.startswith("```"):
                clean_text = clean_text[3:]
            if clean_text.endswith("```"):
                clean_text = clean_text[:-3]
            clean_text = clean_text.strip()

            # 2. 尝试将文本解析为 JSON 字典
            try:
                tool_call_dict = json.loads(clean_text)
                
                # 判断是不是我们约定的工具调用格式
                if isinstance(tool_call_dict, dict) and "action" in tool_call_dict:
                    action_name = tool_call_dict["action"]
                    action_params = tool_call_dict.get("action_input", {})

                    yield f"\n\n🛠️ [系统动作]: 解析到工具请求 [{action_name}]，参数: {action_params}，正在执行...\n"
                    
                    # 3. 工具路由 (Tool Router) -----------------
                    tool_result = ""
                    if action_name == "get_time":
                        tool_result = get_current_time()
                    elif action_name == "get_weather":
                        # 从参数字典中安全提取 'city'，默认为空字符串
                        city = action_params.get("city", "")
                        tool_result = get_weather(city)
                    else:
                        tool_result = f"错误：未知的工具 '{action_name}'"
                    # ----------------------------------------
                    
                    yield f" 拿到结果：{tool_result}\n"
                    
                    # 4. 把执行结果作为一轮新的输入塞给大模型
                    tool_feedback = f"【系统内部反馈】工具执行完毕。结果是：{tool_result}。请基于此结果，用自然语言回答用户。"
                    self.memory.add_user_message(tool_feedback)
                    
                    yield "🧠 [Agent 思考]: 已将结果存入记忆，正在生成最终回答...\n\n"
                    continue  # 继续循环，让大模型根据新拿到的记忆作答
                    
            except json.JSONDecodeError:
                # 解析 JSON 失败，说明大模型输出的是普通的人类聊天语言（自然语言）
                # 这是好事情，说明它已经得出结论或者不需要用工具了，直接跳出循环即可
                break