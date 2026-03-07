# core/agent_config.py
from dataclasses import dataclass

@dataclass
class AgentConfig:
    max_loops: int = 8
    debug: bool = False
    show_usage: bool = True
    enable_memory_summary: bool = False