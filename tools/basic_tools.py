# tools/basic_tools.py
from datetime import datetime

def get_current_time() -> str:
    """获取当前的系统本地时间"""
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")