# core/memory.py
import json
from typing import List, Dict

class ConversationMemory:
    # 新增 max_memory_length 参数，默认只保留最近 10 条消息
    def __init__(self, system_prompt: str, max_memory_length: int = 100):
        self.system_message = {"role": "system", "content": system_prompt}
        self.messages: List[Dict] =[]
        self.max_memory_length = max_memory_length

    def add_user_message(self, content: str):
        self.messages.append({"role": "user", "content": content})
        self._trim() # 每次添加新消息后，检查是否需要修剪记忆

    def add_ai_message(self, content: str):
        self.messages.append({"role": "assistant", "content": content})
        self._trim()
        
    def add_assistant_tool_call(self, tool_calls: List[Dict], content: str = ""):
        """记录模型发起工具调用的这个动作"""
        self.messages.append({
            "role": "assistant",
            "content": content, # 可能伴随的思考过程文本
            "tool_calls": tool_calls
        })
        self._trim()

    def add_tool_message(self, tool_call_id: str, name: str, content: str):
        """记录工具执行的最终结果"""
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content
        })
        self._trim()

    def _trim(self):
        # 【核心逻辑：滑动窗口】
        # 如果当前记忆长度超过了限制，就砍掉最老的消息，只保留最新的部分
        if len(self.messages) > self.max_memory_length:
            self.messages = self.messages[-self.max_memory_length:]

    def get_messages(self) -> List[Dict]:
        # 无论记忆怎么修剪，第一条永远必须是 System Prompt
        return [self.system_message] + self.messages

    def get_debug_info(self) -> str:
        return json.dumps(self.get_messages(), ensure_ascii=False, indent=2)