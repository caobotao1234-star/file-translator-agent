import json
from typing import List, Dict

class ConversationMemory:
    """负责管理对话历史/上下文"""
    
    def __init__(self, system_prompt: str):
        self.messages =[
            {"role": "system", "content": system_prompt.strip()}
        ]

    def add_user_message(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def add_ai_message(self, content: str):
        self.messages.append({"role": "assistant", "content": content})

    def get_messages(self) -> List[Dict]:
        return self.messages
        
    def get_debug_info(self) -> str:
        """用于调试打印"""
        return json.dumps(self.messages, indent=2, ensure_ascii=False)