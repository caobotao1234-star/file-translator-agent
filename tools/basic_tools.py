# tools/basic_tools.py
from datetime import datetime
from tools.base_tool import BaseTool
import urllib.request
import xml.etree.ElementTree as ET
from tools.base_tool import BaseTool

class TimeTool(BaseTool):
    name = "get_time"
    description = '获取当前精确时间。参数：无。'

    def execute(self, params: dict) -> str:
        now = datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")

class WeatherTool(BaseTool):
    name = "get_weather"
    description = '查询指定城市的天气。参数：{"city": "城市名称"}'

    def execute(self, params: dict) -> str:
        city = params.get("city", "")
        mock_weather_data = {
            "北京": "晴转多云，气温 15~22℃，适合出行",
            "上海": "小雨，气温 10~18℃，出门请带伞",
            "广州": "雷阵雨，气温 22~28℃，极为闷热"
        }
        return mock_weather_data.get(city, f"抱歉，暂未查到 {city} 的天气数据。")
    
class CalculatorTool(BaseTool):
    name = "calculator"
    # 【关键】在这里向大模型清晰地描述你需要哪几个参数
    description = '执行基础数学计算。参数格式：{"num1": 数字, "num2": 数字, "operator": "运算符，支持 + - * /"}'

    def execute(self, params: dict) -> str:
        # 1. 从 Agent 传过来的 JSON 字典中安全提取多个参数
        try:
            num1 = float(params.get("num1", 0))
            num2 = float(params.get("num2", 0))
            operator = params.get("operator", "+")
            
            # 2. 执行核心计算逻辑
            if operator == "+":
                result = num1 + num2
            elif operator == "-":
                result = num1 - num2
            elif operator == "*":
                result = num1 * num2
            elif operator == "/":
                if num2 == 0:
                    return "错误：除数不能为0"
                result = num1 / num2
            else:
                return f"错误：不支持的运算符 {operator}"
            
            return str(result)
            
        except Exception as e:
            return f"执行计算时出错：{e}。请检查参数是否为数字。"
        
class NewsTool(BaseTool):
    name = "get_today_news"
    description = '获取今天的实时新闻头条。参数：无。'

    def execute(self, params: dict) -> str:
        try:
            # 抓取新浪新闻的公开 RSS 订阅源（完全免费稳定，无需翻墙）
            url = "https://rss.sina.com.cn/news/china/focus15.xml"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            response = urllib.request.urlopen(req, timeout=5)
            xml_data = response.read()
            
            # 解析 XML 数据
            root = ET.fromstring(xml_data)
            snippets =[]
            # 提取前 5 条最新新闻的标题
            for item in root.findall('./channel/item')[:5]:
                title = item.find('title').text
                snippets.append(f"📰 {title}")
            
            return "今日最新国内/国际头条：\n" + "\n".join(snippets)
        except Exception as e:
            return f"获取新闻失败：{e}"