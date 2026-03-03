import json
from typing import List, Dict

class ConversationMemory:
    """负责管理对话历史/上下文（带滑动窗口截断功能）"""
    
    # 默认最多记住最近的 8 条对话（可根据模型 Token 上限调整）
    def __init__(self, system_prompt: str, max_history_length: int = 100):
        # 1. 独立保存系统提示词（上帝指令，绝对不能丢！）
        self.system_prompt_msg = {"role": "system", "content": system_prompt.strip()}
        
        # 2. 专门用于存储用户和 AI 的动态对话列表
        self.chat_history =[]  
        
        # 3. 记忆容量上限
        self.max_history_length = max_history_length

    def add_user_message(self, content: str):
        self.chat_history.append({"role": "user", "content": content})
        self._truncate()  # 每次加入新记忆后，检查是否需要遗忘

    def add_ai_message(self, content: str):
        self.chat_history.append({"role": "assistant", "content": content})
        self._truncate()  # 每次加入新记忆后，检查是否需要遗忘

    def _truncate(self):
        """核心：滑动窗口截断，防止 Token 爆炸"""
        if len(self.chat_history) > self.max_history_length:
            # 丢弃最老的记忆，只保留最新的 max_history_length 条
            self.chat_history = self.chat_history[-self.max_history_length:]

    def get_messages(self) -> List[Dict]:
        """每次请求模型时，把 '系统提示词' 和 '截断后的最近对话' 拼接起来"""
        return[self.system_prompt_msg] + self.chat_history
        
    def get_debug_info(self) -> str:
        """用于调试打印，让你清楚地看到 AI 现在脑子里装了什么"""
        return json.dumps(self.get_messages(), indent=2, ensure_ascii=False)