# core/llm_router.py
from typing import Dict, Optional
from core.llm_engine import ArkLLMEngine
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：LLM 路由器（LLM Router）
# =============================================================
# 为什么需要多模型管理？
#
# 不同任务对模型的要求不同：
#   - 初翻/草稿 → 便宜快速的模型（如 doubao-lite），降低成本
#   - 审校/精翻 → 贵但精准的模型（如 doubao-pro），保证质量
#   - 格式分析 → 可能用专门的模型或同一个模型的不同参数
#
# LLMRouter 就是一个"模型调度中心"：
#   - 注册多个模型（每个有一个别名，如 "fast", "quality"）
#   - 通过别名获取对应的 LLM 引擎实例
#   - 设置一个默认模型，不指定时自动使用
#
# 这样 Agent 代码里不需要硬编码 model_id，
# 只需要说"我要用 quality 模型"就行了。
# 换模型只需要改配置，不用改业务代码。
# =============================================================

logger = get_logger("llm_router")


class LLMRouter:
    """
    LLM 模型路由器：管理多个 LLM 引擎实例，按别名调度。

    用法：
        router = LLMRouter(api_key="xxx")
        router.register("fast", model_id="ep-xxx-lite")
        router.register("quality", model_id="ep-xxx-pro")
        router.set_default("fast")

        llm = router.get("quality")  # 获取指定模型
        llm = router.get()           # 获取默认模型
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.engines: Dict[str, ArkLLMEngine] = {}
        self.default_name: Optional[str] = None

    def register(
        self,
        name: str,
        model_id: str,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> "LLMRouter":
        """
        注册一个 LLM 模型。

        参数：
            name: 别名（如 "fast", "quality", "review"）
            model_id: 火山引擎的 endpoint ID
            max_retries: 最大重试次数
            retry_base_delay: 重试初始等待秒数

        返回 self，支持链式调用：
            router.register("fast", "ep-xxx").register("quality", "ep-yyy")
        """
        engine = ArkLLMEngine(
            api_key=self.api_key,
            model_id=model_id,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
        )
        self.engines[name] = engine
        logger.info(f"已注册 LLM 模型: {name} -> {model_id}")

        # 如果是第一个注册的模型，自动设为默认
        if self.default_name is None:
            self.default_name = name

        return self

    def set_default(self, name: str) -> "LLMRouter":
        """设置默认模型"""
        if name not in self.engines:
            raise ValueError(
                f"模型 '{name}' 未注册。已注册: {list(self.engines.keys())}"
            )
        self.default_name = name
        logger.info(f"默认 LLM 模型设为: {name}")
        return self

    def get(self, name: str = None) -> ArkLLMEngine:
        """
        获取一个 LLM 引擎实例。

        参数：
            name: 模型别名，为 None 时返回默认模型
        """
        if name is None:
            name = self.default_name
        if name is None or name not in self.engines:
            available = list(self.engines.keys())
            raise ValueError(f"模型 '{name}' 不可用。已注册: {available}")
        return self.engines[name]

    def list_models(self) -> Dict[str, str]:
        """列出所有已注册的模型，返回 {别名: model_id}"""
        return {
            name: engine.model_id
            for name, engine in self.engines.items()
        }
