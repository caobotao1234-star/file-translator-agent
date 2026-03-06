# core/memory.py
import json
from typing import List, Dict

class ConversationMemory:
    # 为了方便测试，你可以先把 agent.py 里的 max_memory_length 改小一点，比如 6
    def __init__(self, system_prompt: str, max_memory_length: int = 20):
        # 把原始的系统提示词存起来
        self.base_system_prompt = system_prompt
        self.messages: List[Dict] =[]
        self.max_memory_length = max_memory_length
        # 【新增】：用于存放长期记忆的摘要
        self.memory_summary = "" 

    def add_user_message(self, content: str):
        self.messages.append({"role": "user", "content": content})
        self._trim()

    def add_ai_message(self, content: str):
        self.messages.append({"role": "assistant", "content": content})
        self._trim()

    def add_assistant_tool_call(self, tool_calls: List[Dict], content: str = ""):
        self.messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls
        })
        self._trim()

    def add_tool_message(self, tool_call_id: str, name: str, content: str):
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content
        })
        self._trim()

    def _trim(self):
        # 1. 如果没超长，直接返回
        if len(self.messages) <= self.max_memory_length:
            return

        # 2. 【核心改变：寻找安全边界】
        # 我们从预定的截断点开始，往后寻找最近的一个 user 消息作为新的起点
        # 这样就能绝对保证不会把 assistant 和 tool 这一对“苦命鸳鸯”拆散
        cut_index = len(self.messages) - self.max_memory_length
        
        while cut_index < len(self.messages):
            if self.messages[cut_index]["role"] == "user":
                break
            cut_index += 1
            
        # 极端情况防御：如果后面全是工具调用，找不到 user，那就暂时不裁剪了
        if cut_index == len(self.messages):
            return
            
        # 3. 把即将被遗忘的消息提取出来
        forgotten_messages = self.messages[:cut_index]
        # 更新短期记忆列表
        self.messages = self.messages[cut_index:]
        
        # 4. 【记忆凝缩机制】
        # 进阶玩法：其实可以在这里调用 LLM 给 forgotten_messages 写一段总结。
        # 为了不阻塞当前的聊天流，我们先做一个简单的标记机制，这代表了 Agent 拥有了“潜意识”的框架。
        user_queries = [msg["content"] for msg in forgotten_messages if msg["role"] == "user"]
        if user_queries:
            self.memory_summary += f"\n- 用户曾经问过或讨论过: {', '.join(user_queries)[:100]}..."

    def get_messages(self) -> List[Dict]:
        # 【新增】：动态拼接系统提示词
        # 每次发给模型前，把“潜意识摘要”拼接到 System Prompt 的末尾
        final_system_content = self.base_system_prompt
        if self.memory_summary:
            final_system_content += f"\n\n【你的长期记忆/潜意识】\n请记住以下早期对话的要点，用户可能会再次提及：{self.memory_summary}"
            
        system_message = {"role": "system", "content": final_system_content}
        return [system_message] + self.messages

    def get_debug_info(self) -> str:
        return json.dumps(self.get_messages(), ensure_ascii=False, indent=2)