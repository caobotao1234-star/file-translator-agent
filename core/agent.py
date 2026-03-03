from typing import Generator
from core.llm_engine import ArkLLMEngine
from core.memory import ConversationMemory

class BaseAgent:
    """Agent 大脑，负责统筹 记忆、模型、以及工具"""
    
    def __init__(self, llm_engine: ArkLLMEngine, memory: ConversationMemory):
        self.llm = llm_engine
        self.memory = memory
        self.tools = []  # <---[预留] 后续这里可以注册外部工具箱

    def chat(self, user_input: str) -> Generator[str, None, None]:
        """
        处理单次对话的核心逻辑 (流式返回)
        """
        # 1. 记录用户输入到记忆
        self.memory.add_user_message(user_input)

        # ---------------------------------------------------------
        # [预留位]：未来这里可以加入 Tool Calling 逻辑
        # 比如：判断意图 -> 调用搜索工具 -> 把搜索结果追加到 memory 中
        # ---------------------------------------------------------

        # 2. 调用 LLM 进行思考和回复
        full_response = ""
        for chunk in self.llm.stream_chat(self.memory.get_messages()):
            full_response += chunk
            yield chunk  # 将每一个字的生成抛给上一层 (如UI界面)

        # 3. 将 AI 完整的回答存入记忆
        self.memory.add_ai_message(full_response)