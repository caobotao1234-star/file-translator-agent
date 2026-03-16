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
