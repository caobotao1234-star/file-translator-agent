# core/agent_config.py
from dataclasses import dataclass

@dataclass
class AgentConfig:
    max_loops: int = 8
    debug: bool = False
    show_usage: bool = True
    enable_memory_summary: bool = False
    # 📘 新增：持久化配置
    enable_persistence: bool = False       # 是否开启对话持久化
    storage_dir: str = "chat_history"      # 对话历史存储目录