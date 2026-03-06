# tools/basic_tools.py
from datetime import datetime
from tools.base_tool import BaseTool
import urllib.request
import xml.etree.ElementTree as ET
from tools.base_tool import BaseTool
from ddgs import DDGS

class TimeTool(BaseTool):
    name = "get_time"
    description = '获取当前精确时间。参数：无。'

    def execute(self, params: dict) -> str:
        now = datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")

class WeatherTool(BaseTool):
    name = "get_weather"
    description = '查询指定城市的天气'
    # 告诉模型：我需要一个 city 字段，是字符串类型，必填
    parameters = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "需要查询天气的城市名称，如：北京、上海"
            }
        },
        "required": ["city"]
    }
    
    def execute(self, params: dict) -> str:
        # execute 里面的代码保持你原来的不变
        city = params.get("city", "")
        mock_weather_data = {
            "北京": "晴转多云，气温 15~22℃",
            "上海": "小雨，气温 10~18℃",
            "广州": "雷阵雨，气温 22~28℃"
        }
        return mock_weather_data.get(city, f"抱歉，暂未查到 {city} 的天气数据。")

class CalculatorTool(BaseTool):
    name = "calculator"
    description = '执行基础数学计算'
    parameters = {
        "type": "object",
        "properties": {
            "num1": {"type": "number", "description": "第一个数字"},
            "num2": {"type": "number", "description": "第二个数字"},
            "operator": {"type": "string", "description": "运算符，支持 + - * /"}
        },
        "required":["num1", "num2", "operator"]
    }
    
    def execute(self, params: dict) -> str:
        # 【修复】：把你原本的计算逻辑补回来了！
        try:
            num1 = float(params.get("num1", 0))
            num2 = float(params.get("num2", 0))
            operator = params.get("operator", "+")
            
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
            return f"执行计算时出错：{e}"
        
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
        
class WebSearchTool(BaseTool):
    name = "web_search"
    description = '搜索引擎工具。当你需要查询不知道的知识、实时新闻或信息时使用。'
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，例如：'苹果公司现任CEO是谁'"
            }
        },
        "required": ["query"]
    }

    def execute(self, params: dict) -> str:
        query = params.get("query", "")
        if not query:
            return "错误：搜索关键词不能为空"
        
        try:
            results = DDGS().text(query, max_results=3)
            if not results:
                return "未搜索到相关结果"
            
            res_str = ""
            for i, r in enumerate(results):
                res_str += f"[{i+1}] 标题: {r['title']}\n摘要: {r['body']}\n\n"
            return res_str
        except Exception as e:
            return f"搜索出错: {e}"