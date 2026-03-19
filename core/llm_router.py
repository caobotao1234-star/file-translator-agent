# core/llm_router.py
from typing import Dict, Optional, Union
from core.llm_engine import ArkLLMEngine
from core.external_llm_engine import ExternalLLMEngine, create_external_engine, PROVIDER_CONFIG
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：LLM 路由器（LLM Router）
# =============================================================
# 为什么需要多模型管理？
#
# 不同任务对模型的要求不同：
#   - 翻译 → doubao-seed-2-0-pro（高质量翻译）
#   - 规划者 → gemini-2.5-pro（vision + tool_call）
#   - 图片生成 → gemini-3-pro-image-preview
#
# 📘 v6 统一架构：
#   所有模型（火山引擎 + Gemini + Claude + GPT）都走 ExternalLLMEngine。
#   火山引擎 API 也是 OpenAI 兼容协议，不需要单独的 ArkLLMEngine。
#   统一后所有模型能力等价：vision、tool_call、extra_content 全部支持。
#
# LLMRouter 就是一个"模型调度中心"：
#   - 注册多个模型（每个有一个别名，如 "translate", "agent_brain"）
#   - 通过别名获取对应的 LLM 引擎实例
#   - 所有引擎都有相同的 stream_chat 接口
# =============================================================

logger = get_logger("llm_router")


class LLMRouter:
    """
    📘 LLM 模型路由器：管理多个 LLM 引擎实例，按别名调度。

    v6 统一架构：所有模型都走 ExternalLLMEngine（OpenAI 兼容协议），
    火山引擎和外部模型不再区分，能力完全等价。

    用法：
        router = LLMRouter(api_key="xxx")
        router.register_model("translate", model_str="doubao-seed-2-0-pro-260215")
        router.register_model("agent_brain", model_str="gemini:gemini-2.5-pro")

        llm = router.get("translate")     # 获取翻译模型
        llm = router.get("agent_brain")   # 获取规划者
    """

    # 📘 LLMEngine 类型：统一为 ExternalLLMEngine（鸭子类型兼容 ArkLLMEngine）
    LLMEngine = Union[ExternalLLMEngine, ArkLLMEngine]

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.engines: Dict[str, "LLMRouter.LLMEngine"] = {}
        self.default_name: Optional[str] = None

    def register(
        self,
        name: str,
        model_id: str,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> "LLMRouter":
        """
        📘 注册一个火山引擎模型。

        v6 统一架构：内部也走 ExternalLLMEngine（OpenAI 兼容协议），
        这样火山引擎模型也支持 vision + tool_call + extra_content。

        参数：
            name: 别名（如 "translate", "agent_brain"）
            model_id: 火山引擎的模型 ID（如 "doubao-seed-2-0-pro-260215"）
        """
        engine = create_external_engine(
            provider="ark",
            model_id=model_id,
            api_key=self.api_key,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
        )
        self.engines[name] = engine
        logger.info(f"已注册 LLM 模型: {name} -> {model_id}")

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

    def get(self, name: str = None) -> "LLMRouter.LLMEngine":
        """
        获取一个 LLM 引擎实例。

        📘 v6: 返回的引擎都是 ExternalLLMEngine，
        支持 vision + tool_call + extra_content。
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

    def register_external(
        self,
        name: str,
        provider: str,
        model_id: str,
        api_key: str = None,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> "LLMRouter":
        """
        📘 注册一个外部模型引擎（Gemini/Claude/GPT/NanoBanana）。

        📘 v6 统一架构：register() 和 register_external() 内部都用 ExternalLLMEngine，
        区别只是 provider 不同。保留此方法是为了向后兼容 .env 配置方式。
        """
        engine = create_external_engine(
            provider=provider,
            model_id=model_id,
            api_key=api_key,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
        )
        self.engines[name] = engine
        logger.info(f"已注册外部 LLM 模型: {name} -> {provider}/{model_id}")

        if self.default_name is None:
            self.default_name = name

        return self

    def register_model(
        self,
        name: str,
        model_str: str,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> "LLMRouter":
        """
        📘 教学笔记：智能模型注册（自动识别 provider）

        根据 model_str 格式自动选择 provider：
        - "gemini:gemini-2.5-pro" → provider=gemini
        - "doubao-seed-2-0-pro-260215" → provider=ark（向后兼容）

        📘 v6 统一架构：无论哪个 provider，内部都走 ExternalLLMEngine。
        """
        from config.settings import Config
        provider, model_id = Config.parse_model_id(model_str)

        if provider == "ark":
            return self.register(
                name=name,
                model_id=model_id,
                max_retries=max_retries,
                retry_base_delay=retry_base_delay,
            )
        else:
            return self.register_external(
                name=name,
                provider=provider,
                model_id=model_id,
                max_retries=max_retries,
                retry_base_delay=retry_base_delay,
            )

