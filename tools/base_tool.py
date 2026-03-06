# tools/base_tool.py
from abc import ABC, abstractmethod

class BaseTool(ABC):
    name: str = ""
    description: str = ""
    # 新增：工具的参数定义（默认是空参数的 JSON Schema）
    parameters: dict = {"type": "object", "properties": {}}

    @abstractmethod
    def execute(self, params: dict) -> str:
        pass

    # 新增：将我们的 Python 工具类，转换成大模型 API 认识的标准字典格式
    def get_api_format(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }