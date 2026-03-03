import os
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

class Config:
    ARK_API_KEY = os.getenv("ARK_API_KEY")
    DEFAULT_MODEL_ID = os.getenv("DEFAULT_MODEL_ID")

    if not ARK_API_KEY:
        raise ValueError("请在 .env 文件中设置 ARK_API_KEY")