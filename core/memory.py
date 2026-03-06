# core/memory.py
import json
from typing import List, Dict
# 【新增】：把大模型引擎引入进来
from core.llm_engine import ArkLLMEngine

class ConversationMemory:
    # 【修改】：__init__ 方法现在需要接收 llm_engine
    def __init__(self, system_prompt: str, llm_engine: ArkLLMEngine, max_memory_length: int = 20):
        self.base_system_prompt = system_prompt
        self.messages: List[Dict] =[]
        self.max_memory_length = max_memory_length
        self.memory_summary = "" 
        
        # 将大模型引擎存为内部属性，供后台提炼记忆时使用
        self.llm_engine = llm_engine

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

        # 2. 寻找安全边界（依然保留，防止切断工具链）
        cut_index = len(self.messages) - self.max_memory_length
        while cut_index < len(self.messages):
            if self.messages[cut_index]["role"] == "user":
                break
            cut_index += 1
            
        if cut_index == len(self.messages):
            return
            
        # 3. 把即将被遗忘的消息提取出来
        forgotten_messages = self.messages[:cut_index]
        self.messages = self.messages[cut_index:]
        
        # -----------------------------------------------------
        # 🌟 4. 【核心升级：LLM 智能记忆凝缩】
        # -----------------------------------------------------
        # 把被遗忘的对话拼成一段可读的剧本
        chat_history_str = ""
        for msg in forgotten_messages:
            if msg["role"] == "user":
                chat_history_str += f"用户：{msg['content']}\n"
            elif msg["role"] == "assistant" and msg.get("content"):
                chat_history_str += f"AI：{msg['content']}\n"

        if not chat_history_str.strip():
            return

        # 构造给 LLM 的“记忆提炼指令”
        summarize_prompt = f"""
你是一个专业的记忆整理助手。你需要帮主线 AI 提炼和更新重要的长期记忆。

【之前的旧记忆】：
{self.memory_summary if self.memory_summary else "无"}

【刚刚被遗忘的对话片段】：
{chat_history_str}

【你的任务】：
请结合“旧记忆”和“遗忘的对话”，提取出用户的人设、偏好、核心事实等高价值信息。
剔除寒暄、报错、无关紧要的废话。如果有冲突的偏好（比如以前说喜欢A，现在说喜欢B），以最新的对话为准。
请用极其简练的语言输出最新记忆摘要（不要超过 100 字）。只输出摘要文本本身。
"""
        print("\n[🧠 记忆系统]: 检测到短期记忆已满，正在后台运行大模型凝缩长时记忆...")
        
        # 悄悄调用 LLM 生成摘要（不抛出到终端流，只是静默收集文本）
        new_summary = ""
        for chunk in self.llm_engine.stream_chat([{"role": "user", "content": summarize_prompt}]):
            if chunk["type"] == "text":
                new_summary += chunk["content"]
        
        self.memory_summary = new_summary.strip()
        print(f"[🧠 记忆系统]: 凝缩完成！最新潜意识更新为：\n   👉 {self.memory_summary}\n")

    def get_messages(self) -> List[Dict]:
        final_system_content = self.base_system_prompt
        if self.memory_summary:
            final_system_content += f"\n\n【你的长期记忆/潜意识】\n请记住以下早期对话的核心要点，它们对当前对话非常重要：\n{self.memory_summary}"
            
        system_message = {"role": "system", "content": final_system_content}
        return [system_message] + self.messages

    def get_debug_info(self) -> str:
        return json.dumps(self.get_messages(), ensure_ascii=False, indent=2)