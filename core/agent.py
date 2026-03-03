from typing import Generator
from core.llm_engine import ArkLLMEngine
from core.memory import ConversationMemory
from tools.basic_tools import get_current_time  # <--- 引入我们的工具

class BaseAgent:
    def __init__(self, llm_engine: ArkLLMEngine, memory: ConversationMemory):
        self.llm = llm_engine
        self.memory = memory

    def chat(self, user_input: str) -> Generator[str, None, None]:
        # 1. 记录用户输入
        self.memory.add_user_message(user_input)

        # 打印 Debug
        print("\n" + "▼"*20 + " [Debug: 真正发送给模型的上下文] " + "▼"*20)
        print(self.memory.get_debug_info())
        print("▲"*65 + "\n")

        # 2. 开启一个循环，允许 Agent 进行多次 "思考-行动" (ReAct 机制的雏形)
        while True:
            full_response = ""
            
            # 让 LLM 生成回答
            for chunk in self.llm.stream_chat(self.memory.get_messages()):
                full_response += chunk
                yield chunk  # 抛给 UI 打印
                
            # 把这段回答存入记忆
            self.memory.add_ai_message(full_response)

            # ==========================================
            # 🔧 [Agent 核心逻辑]：工具拦截器 (Tool Interceptor)
            # ==========================================
            # 【修改这里】：去掉两端空格后，必须【完全等于】这个指令，而不是仅仅包含
            if full_response.strip() == "[TOOL: get_time]":
                yield "\n\n🛠️ [系统动作]: 触发了获取时间工具，正在执行本地代码..."
                
                # 执行 Python 代码拿到真正的时间
                real_time = get_current_time()
                yield f" 拿到结果：{real_time}\n"
                
                # 关键：以系统/用户的口吻，把工具的结果告诉大模型
                tool_feedback = f"系统工具返回结果：当前精确时间是 {real_time}。请根据这个时间回答我刚才的问题。"
                self.memory.add_user_message(tool_feedback)
                
                yield "🧠 [Agent 思考]: 拿到时间了，正在重新组织语言...\n\n"
                # 继续 while 循环，模型会带着新的时间记忆再次生成回答！
                continue  
            
            # 如果没有触发工具，说明模型已经给出了最终答案，跳出循环
            else:
                break