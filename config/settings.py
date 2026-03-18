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

    # =============================================================
    # 📘 教学笔记：统一模型架构
    # =============================================================
    # 火山引擎（doubao）和外部模型（Gemini/Claude/GPT）完全等价。
    # 每个模型用 "provider:model_id" 格式标识：
    #   - "ark:doubao-seed-1-8-251228"  → 火山引擎
    #   - "gemini:gemini-3.1-pro-preview" → Gemini
    # GUI 下拉框里所有模型混在一起，用户自由选择。
    # =============================================================

    @staticmethod
    def _parse_model_list(raw: str) -> dict:
        """📘 通用模型列表解析：支持 显示名=模型ID 格式"""
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
    def get_available_models() -> dict:
        """
        📘 返回所有可用模型（火山引擎 + 外部模型统一列表）。

        火山引擎模型从 AVAILABLE_MODELS 读取。
        外部模型从 GEMINI_MODELS 等读取，自动加 "gemini:" 前缀。
        返回 {显示名: 模型标识}，模型标识格式：
        - 火山引擎: "doubao-seed-1-8-251228"（无前缀，向后兼容）
        - 外部模型: "gemini:gemini-3.1-pro-preview"（带 provider 前缀）
        """
        # 📘 火山引擎模型
        models = Config._parse_model_list(os.getenv("AVAILABLE_MODELS", ""))
        default = Config.DEFAULT_MODEL_ID
        if default and default not in models.values():
            models[default] = default

        # 📘 外部模型：从各 provider 的模型列表中读取
        from core.external_llm_engine import PROVIDER_CONFIG
        for provider, cfg in PROVIDER_CONFIG.items():
            api_key = os.getenv(cfg["env_key"], "").strip()
            if not api_key:
                continue  # 没有 API key 的 provider 跳过
            env_name = f"{provider.upper()}_MODELS"
            raw = os.getenv(env_name, "").strip()
            if raw:
                for pair in raw.split(","):
                    pair = pair.strip()
                    if "=" in pair:
                        name, mid = pair.split("=", 1)
                        models[f"🌐 {name.strip()}"] = f"{provider}:{mid.strip()}"
                    elif pair:
                        models[f"🌐 {pair}"] = f"{provider}:{pair}"

        return models if models else {default: default}

    @staticmethod
    def get_vision_models() -> dict:
        """
        📘 返回支持多模态（图片输入）的模型列表。
        同样合并火山引擎 + 外部模型。
        """
        models = Config._parse_model_list(os.getenv("VISION_MODELS", ""))

        # 📘 外部 vision 模型
        from core.external_llm_engine import PROVIDER_CONFIG
        for provider, cfg in PROVIDER_CONFIG.items():
            api_key = os.getenv(cfg["env_key"], "").strip()
            if not api_key:
                continue
            env_name = f"{provider.upper()}_VISION_MODELS"
            raw = os.getenv(env_name, "").strip()
            if raw:
                for pair in raw.split(","):
                    pair = pair.strip()
                    if "=" in pair:
                        name, mid = pair.split("=", 1)
                        models[f"🌐 {name.strip()}"] = f"{provider}:{mid.strip()}"
                    elif pair:
                        models[f"🌐 {pair}"] = f"{provider}:{pair}"

        return models

    @staticmethod
    def get_image_gen_models() -> dict:
        """
        📘 返回支持图片生成/编辑的模型列表。
        如 gemini-3-pro-image-preview。
        """
        models = {}
        from core.external_llm_engine import PROVIDER_CONFIG
        for provider, cfg in PROVIDER_CONFIG.items():
            api_key = os.getenv(cfg["env_key"], "").strip()
            if not api_key:
                continue
            env_name = f"{provider.upper()}_IMAGE_MODELS"
            raw = os.getenv(env_name, "").strip()
            if raw:
                for pair in raw.split(","):
                    pair = pair.strip()
                    if "=" in pair:
                        name, mid = pair.split("=", 1)
                        models[f"🎨 {name.strip()}"] = f"{provider}:{mid.strip()}"
                    elif pair:
                        models[f"🎨 {pair}"] = f"{provider}:{pair}"
        return models

    @staticmethod
    def parse_model_id(model_str: str) -> tuple:
        """
        📘 教学笔记：解析模型标识

        "gemini:gemini-3.1-pro-preview" → ("gemini", "gemini-3.1-pro-preview")
        "doubao-seed-1-8-251228" → ("ark", "doubao-seed-1-8-251228")

        无前缀的默认为火山引擎（ark），向后兼容。
        """
        if ":" in model_str:
            provider, model_id = model_str.split(":", 1)
            return provider.strip(), model_id.strip()
        return "ark", model_str.strip()

    @staticmethod
    def get_agent_brain_config():
        """
        📘 教学笔记：读取 Agent Brain 配置

        Agent Brain 是扫描件翻译的"大脑"，使用外部高能力模型。
        未配置时返回 None，TranslatorAgent 会回退到 v7.1 固定流水线。

        返回: dict 或 None
        """
        from core.external_llm_engine import PROVIDER_CONFIG

        provider = os.getenv("AGENT_BRAIN_PROVIDER", "").strip().lower()
        if not provider:
            return None

        model = os.getenv("AGENT_BRAIN_MODEL", "").strip()
        if not model:
            return None

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
        📘 模型能力检测：检查是否支持视觉输入和工具调用。
        返回: (supported: bool, warning: str or None)
        """
        KNOWN_CAPABLE_PATTERNS = [
            "gemini-3", "gemini-2.5", "gemini-2.0", "gemini-1.5-pro",
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
