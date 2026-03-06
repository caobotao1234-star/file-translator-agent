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