# tools/memory_tools.py
# =============================================================
# 📘 教学笔记：记忆工具
# =============================================================
# Agent 自己决定什么时候读、什么时候写记忆。
# 记忆内容包括：术语表、内容摘要、翻译缓存、用户偏好。
# 这替代了之前的 cross_page_context 补丁。
# =============================================================

import json
from typing import Dict, List

from core.agent_loop import BaseTool
from core.logger import get_logger

logger = get_logger("memory_tools")


class MemoryStore:
    """
    📘 Agent 的记忆存储

    跨页、跨文件的持久化记忆。Agent 通过工具读写。
    """

    def __init__(self):
        self.glossary: Dict[str, str] = {}  # 术语表 {原文: 译文}
        self.summary: str = ""  # 文档内容摘要
        self.user_preferences: List[str] = []  # 用户偏好
        self.translation_cache: Dict[str, str] = {}  # 翻译缓存

    def to_dict(self) -> dict:
        return {
            "glossary": dict(list(self.glossary.items())[-50:]),
            "summary": self.summary[-500:] if self.summary else "",
            "user_preferences": self.user_preferences[-20:],
            "cache_size": len(self.translation_cache),
        }


class ReadMemoryTool(BaseTool):
    """📘 读取跨页记忆"""

    name = "read_memory"
    description = (
        "读取跨页/跨文件记忆，包括术语表、内容摘要、用户偏好。"
        "在翻译新页面前调用，确保术语一致性。"
    )
    parameters = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, memory: MemoryStore):
        self.memory = memory

    def execute(self, params: dict) -> str:
        return json.dumps(self.memory.to_dict(), ensure_ascii=False)


class UpdateMemoryTool(BaseTool):
    """📘 更新跨页记忆"""

    name = "update_memory"
    description = (
        "更新跨页记忆。可以添加术语、更新摘要、记录用户偏好。"
        "翻译完一页后调用，把重要术语和内容摘要存下来。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "glossary_add": {
                "type": "object",
                "description": "要添加的术语 {原文: 译文}",
            },
            "summary_append": {
                "type": "string",
                "description": "要追加的内容摘要",
            },
            "user_preference": {
                "type": "string",
                "description": "要记录的用户偏好",
            },
        },
    }

    def __init__(self, memory: MemoryStore):
        self.memory = memory

    def execute(self, params: dict) -> str:
        added = []

        glossary_add = params.get("glossary_add", {})
        if glossary_add:
            self.memory.glossary.update(glossary_add)
            added.append(f"术语 +{len(glossary_add)}")

        summary = params.get("summary_append", "")
        if summary:
            if self.memory.summary:
                self.memory.summary += " | " + summary
            else:
                self.memory.summary = summary
            added.append("摘要已更新")

        pref = params.get("user_preference", "")
        if pref:
            self.memory.user_preferences.append(pref)
            added.append(f"偏好: {pref}")

        return json.dumps({
            "updated": ", ".join(added) if added else "无更新",
            "glossary_total": len(self.memory.glossary),
        }, ensure_ascii=False)
