# tools/basic_tools.py
from datetime import datetime

def get_current_time() -> str:
    """获取当前的系统本地时间"""
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")

def get_weather(city: str) -> str:
    """获取指定城市的天气（模拟数据）"""
    # 这里我们用假数据模拟，未来你可以替换为真实的天气 API
    mock_weather_data = {
        "北京": "晴转多云，气温 15~22℃，适合出行",
        "上海": "小雨，气温 10~18℃，出门请带伞",
        "广州": "雷阵雨，气温 22~28℃，极为闷热"
    }
    # 如果城市在字典里，返回对应天气；否则返回默认提示
    return mock_weather_data.get(city, f"抱歉，暂未查到 {city} 的天气数据。")