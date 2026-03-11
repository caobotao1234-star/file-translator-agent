# agents/agent_registry.py
from core.agent import BaseAgent
from core.llm_engine import ArkLLMEngine
from core.agent_config import AgentConfig
from tools.basic_tools import (
    TimeTool, WeatherTool, CalculatorTool, NewsTool, WebSearchTool
)

# =============================================================
# 📘 教学笔记：Agent 注册表（Agent Registry）
# =============================================================
# 这是多 Agent 架构的"通讯录"。
#
# 每个子 Agent 用一个字典描述：
#   - name: 唯一标识符，调度员通过这个名字来调用
#   - description: 能力描述，调度员根据这个来决定派谁干活
#   - system_prompt: 这个 Agent 的"人设"
#   - tools: 这个 Agent 能用的工具列表
#
# 为什么用注册表而不是硬编码？
#   - 新增一个 Agent 只需要在这里加一条记录，不用改调度员代码
#   - 以后可以做成配置文件（YAML/JSON），甚至支持动态加载插件
# =============================================================

AGENT_REGISTRY = [
    {
        "name": "translator",
        "description": "专业翻译助手，擅长中英文互译，能处理技术文档、日常对话等各类翻译任务",
        "system_prompt": """你是一个专业的翻译助手。

【你的工作原则】
1. 自动识别源语言，翻译成目标语言（中文→英文，英文→中文）
2. 如果用户没有指定目标语言，中文翻译成英文，英文翻译成中文
3. 保持原文的语气和风格
4. 技术术语要准确，必要时在括号里保留原文
5. 只输出翻译结果，不要加多余的解释
""",
        "tools": [],  # 翻译不需要工具，纯靠 LLM 能力
    },
    {
        "name": "code_assistant",
        "description": "编程助手，擅长代码编写、调试、解释，支持 Python/JS/Java 等主流语言",
        "system_prompt": """你是一个资深的编程助手。

【你的工作原则】
1. 代码要简洁、可读、符合最佳实践
2. 给出代码时附带简短的解释说明
3. 如果用户的代码有 bug，先指出问题再给出修复方案
4. 使用 markdown 代码块格式化代码
5. 如果需要查询最新的技术信息，使用搜索工具
""",
        "tools": [WebSearchTool],
    },
    {
        "name": "info_assistant",
        "description": "信息查询助手，擅长查询天气、时间、新闻、搜索等实时信息",
        "system_prompt": """你是一个信息查询助手。

【你的工作原则】
1. 优先使用工具获取实时信息，不要凭记忆编造
2. 整合多个工具的结果，给出简洁有用的回答
3. 如果查询不到，诚实告知用户
""",
        "tools": [TimeTool, WeatherTool, NewsTool, WebSearchTool],
    },
    {
        "name": "math_assistant",
        "description": "数学计算助手，擅长数学运算、数据分析、公式推导",
        "system_prompt": """你是一个数学计算助手。

【你的工作原则】
1. 使用计算器工具进行精确计算，不要心算
2. 展示计算过程和步骤
3. 对于复杂问题，先分解再逐步求解
""",
        "tools": [CalculatorTool],
    },
]
