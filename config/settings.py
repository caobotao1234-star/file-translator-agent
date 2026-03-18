import os
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()


class Config:
    ARK_API_KEY = os.getenv("ARK_API_KEY")
    DEFAULT_MODEL_ID = os.getenv("DEFAULT_MODEL_ID")
    VISION_MODEL_ID = os.getenv("VISION_MODEL_ID", "")
    VOLC_ACCESSKEY = os.getenv("VOLC_ACCESSKEY", "")
    VOLC_SECRETKEY = os.getenv("VOLC_SECRETKEY", "")

    if not ARK_API_KEY:
        raise ValueError("请在 .env 文件中设置 ARK_API_KEY")

    @staticmethod
    def get_available_models() -> dict:
        """
        解析 .env 中的 AVAILABLE_MODELS，返回 {显示名: 模型ID}。
        格式: 显示名=模型ID,显示名=模型ID,...
        如果未配置，返回 DEFAULT_MODEL_ID 作为唯一选项。
        """
        raw = os.getenv("AVAILABLE_MODELS", "")
        models = {}
        if raw.strip():
            for pair in raw.split(","):
                pair = pair.strip()
                if "=" in pair:
                    name, model_id = pair.split("=", 1)
                    models[name.strip()] = model_id.strip()
                elif pair:
                    # 没有显示名，用模型ID本身
                    models[pair] = pair
        # 确保默认模型在列表里
        default = Config.DEFAULT_MODEL_ID
        if default and default not in models.values():
            models[default] = default
        return models if models else {default: default}

    @staticmethod
    def get_vision_models() -> dict:
        """
        📘 解析 .env 中的 VISION_MODELS，返回支持多模态的模型列表。
        格式同 AVAILABLE_MODELS。
        """
        raw = os.getenv("VISION_MODELS", "")
        models = {}
        if raw.strip():
            for pair in raw.split(","):
                pair = pair.strip()
                if "=" in pair:
                    name, model_id = pair.split("=", 1)
                    models[name.strip()] = model_id.strip()
                elif pair:
                    models[pair] = pair
        return models

    @staticmethod
    def get_agent_brain_config():
        """
        📘 教学笔记：读取 Agent Brain 配置

        Agent Brain 是扫描件翻译的"大脑"，使用外部高能力模型。
        未配置时返回 None，TranslatorAgent 会回退到 v7.1 固定流水线。

        API Key 优先级：
        1. AGENT_BRAIN_API_KEY（通用 key，最高优先级）
        2. 各 provider 专用 key（如 GEMINI_API_KEY）

        返回: dict 或 None
        """
        from core.external_llm_engine import PROVIDER_CONFIG

        provider = os.getenv("AGENT_BRAIN_PROVIDER", "").strip().lower()
        if not provider:
            return None

        model = os.getenv("AGENT_BRAIN_MODEL", "").strip()
        if not model:
            return None

        # 📘 API Key 优先级：通用 key > provider 专用 key
        api_key = os.getenv("AGENT_BRAIN_API_KEY", "").strip()
        if not api_key and provider in PROVIDER_CONFIG:
            env_key = PROVIDER_CONFIG[provider]["env_key"]
            api_key = os.getenv(env_key, "").strip()

        if not api_key:
            return None

        return {
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "max_tokens": int(os.getenv("AGENT_BRAIN_MAX_TOKENS", "8192")),
            "temperature": float(os.getenv("AGENT_BRAIN_TEMPERATURE", "0.1")),
            "max_retries": int(os.getenv("AGENT_BRAIN_MAX_RETRIES", "3")),
        }

    @staticmethod
    def validate_agent_brain_model(provider: str = None, model: str = None):
        """
        📘 教学笔记：模型能力检测

        检查配置的模型是否支持视觉输入和工具调用。
        已知支持的模型直接返回 True，未知模型返回警告。

        返回: (supported: bool, warning: str or None)
        """
        # 📘 已知支持视觉+工具调用的模型（关键词匹配）
        KNOWN_CAPABLE_PATTERNS = [
            "gemini-3.1", "gemini-2.5", "gemini-2.0", "gemini-1.5-pro",
            "claude-sonnet", "claude-opus", "claude-3.5", "claude-3-5",
            "gpt-4o", "gpt-4-turbo", "gpt-4.1",
            "nanobanana-pro",
        ]

        if not provider or not model:
            config = Config.get_agent_brain_config()
            if not config:
                return False, "Agent Brain 未配置"
            provider = config["provider"]
            model = config["model"]

        model_lower = model.lower()
        for pattern in KNOWN_CAPABLE_PATTERNS:
            if pattern in model_lower:
                return True, None

        warning = (
            f"模型 '{model}' 不在已知支持视觉+工具调用的列表中。"
            f"建议使用 gemini-2.5-pro、claude-sonnet-4、gpt-4o 或 nanobanana-pro。"
        )
        return False, warning
