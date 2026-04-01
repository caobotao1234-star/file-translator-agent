# tools/interaction_tools.py
# =============================================================
# 📘 教学笔记：交互工具
# =============================================================
# ask_user: Agent 主动向用户提问（阻塞等待回答）
# report_progress: Agent 报告进度给 GUI
# =============================================================

import json
import queue
import threading
from typing import Callable, Optional

from core.agent_loop import BaseTool
from core.logger import get_logger

logger = get_logger("interaction_tools")


class AskUserTool(BaseTool):
    """
    📘 向用户提问

    Agent 遇到不确定的地方（人名翻译、专业术语、排版选择）时调用。
    GUI 显示问题，等待用户回答。
    """

    name = "ask_user"
    description = (
        "向用户提问并等待回答。用于不确定的翻译选择、"
        "专业术语确认、排版方案选择等。用户不回答则自行决定。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要问用户的问题",
            },
        },
        "required": ["question"],
    }

    def __init__(self, on_ask: Callable[[str], Optional[str]] = None):
        """
        on_ask: 回调函数，接收问题字符串，返回用户回答（或 None 超时）
        """
        self._on_ask = on_ask

    def execute(self, params: dict) -> str:
        question = params["question"]
        logger.info(f"Agent 提问: {question}")

        if self._on_ask:
            try:
                answer = self._on_ask(question)
                if answer:
                    logger.info(f"用户回答: {answer}")
                    return answer
            except Exception as e:
                logger.warning(f"等待用户回答失败: {e}")

        return "用户未回答，请自行决定并继续。"


class ReportProgressTool(BaseTool):
    """
    📘 报告进度

    Agent 每完成一步都调用，通知 GUI 更新进度。
    """

    name = "report_progress"
    description = (
        "报告翻译进度给用户。每完成一页或一个重要步骤时调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "current": {
                "type": "integer",
                "description": "当前完成的页数/步骤数",
            },
            "total": {
                "type": "integer",
                "description": "总页数/总步骤数",
            },
            "message": {
                "type": "string",
                "description": "进度描述信息",
            },
        },
        "required": ["message"],
    }

    def __init__(self, on_progress: Callable[[int, int, str], None] = None):
        self._on_progress = on_progress

    def execute(self, params: dict) -> str:
        current = params.get("current", 0)
        total = params.get("total", 0)
        message = params.get("message", "")

        logger.info(f"进度: {current}/{total} - {message}")

        if self._on_progress:
            try:
                self._on_progress(current, total, message)
            except Exception:
                pass

        return json.dumps({"acknowledged": True}, ensure_ascii=False)
