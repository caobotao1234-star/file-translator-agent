# tools/base_tool.py
from abc import ABC, abstractmethod

class BaseTool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}

    @abstractmethod
    def execute(self, params: dict) -> str:
        pass

    # 【新增】：通用的参数校验器（安检员）
    def validate_params(self, params: dict) -> tuple[bool, str]:
        """检查模型传来的参数是否包含了所有的 required 字段"""
        required_keys = self.parameters.get("required", [])
        missing_keys =[k for k in required_keys if k not in params]
        
        if missing_keys:
            return False, f"缺少必填参数: {', '.join(missing_keys)}"
        return True, "参数校验通过"

    def get_api_format(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }