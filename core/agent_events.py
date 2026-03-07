# core/agent_events.py
from dataclasses import dataclass, field
from typing import Any, Dict

@dataclass
class AgentEvent:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)