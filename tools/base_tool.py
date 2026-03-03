# tools/base_tool.py
from abc import ABC, abstractmethod

class BaseTool(ABC):
    """所有工具的基类/接口"""
    
    # 工具的名称（模型调用时输出的 action）
    name: str = ""
    # 工具的说明（自动生成进 Prompt 给模型看的）
    description: str = ""

    @abstractmethod
    def execute(self, params: dict) -> str:
        """执行工具的具体逻辑，子类必须实现"""
        pass