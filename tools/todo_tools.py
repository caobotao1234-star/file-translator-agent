# tools/todo_tools.py
# =============================================================
# 📘 教学笔记：任务追踪工具（借鉴 Claude Code 的 TodoWrite）
# =============================================================
# Agent 自己维护一个结构化任务列表：
# - pending: 待做
# - in_progress: 正在做（同一时间只能有一个）
# - completed: 已完成
#
# 好处：
# 1. Agent 工作更有条理（自己知道做到哪了）
# 2. 用户能看到实时进度（GUI 显示任务列表）
# 3. 压缩时任务列表作为关键信息保留
# =============================================================

import json
from typing import Callable, Dict, List, Optional

from core.agent_loop import BaseTool
from core.logger import get_logger

logger = get_logger("todo_tools")


class TodoStore:
    """任务列表存储"""

    def __init__(self):
        self.tasks: List[dict] = []
        self._on_update: Optional[Callable[[List[dict]], None]] = None

    def set_callback(self, callback: Callable[[List[dict]], None]):
        self._on_update = callback

    def _notify(self):
        if self._on_update:
            try:
                self._on_update(self.tasks)
            except Exception:
                pass

    def to_list(self) -> List[dict]:
        return [
            {"id": t["id"], "content": t["content"], "status": t["status"]}
            for t in self.tasks
        ]


class TodoWriteTool(BaseTool):
    """
    📘 任务列表管理工具（借鉴 Claude Code）

    Agent 用这个工具创建和更新任务列表。
    每次调用传入完整的任务列表（全量替换，不是增量）。
    """

    name = "todo_write"
    description = (
        "创建或更新任务列表。传入完整的任务列表（全量替换）。"
        "用于复杂任务的进度追踪。每个任务有 id、content、status。"
        "status: pending（待做）、in_progress（正在做）、completed（已完成）。"
        "同一时间只能有一个任务是 in_progress。"
        "完成一个任务后立即标记 completed，再把下一个标记 in_progress。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "任务唯一 ID"},
                        "content": {"type": "string", "description": "任务描述"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "任务状态",
                        },
                    },
                    "required": ["id", "content", "status"],
                },
                "description": "完整的任务列表（全量替换）",
            },
        },
        "required": ["tasks"],
    }

    def __init__(self, store: TodoStore):
        self.store = store

    def execute(self, params: dict) -> str:
        tasks = params.get("tasks", [])

        # 验证：同一时间只能有一个 in_progress
        in_progress = [t for t in tasks if t.get("status") == "in_progress"]
        if len(in_progress) > 1:
            logger.warning(f"多个 in_progress 任务: {len(in_progress)}，只保留第一个")
            first_ip = True
            for t in tasks:
                if t.get("status") == "in_progress":
                    if first_ip:
                        first_ip = False
                    else:
                        t["status"] = "pending"

        self.store.tasks = tasks
        self.store._notify()

        total = len(tasks)
        completed = sum(1 for t in tasks if t.get("status") == "completed")
        in_prog = sum(1 for t in tasks if t.get("status") == "in_progress")
        pending = sum(1 for t in tasks if t.get("status") == "pending")

        logger.info(
            f"任务列表更新: {total} 个任务 "
            f"(完成 {completed}, 进行中 {in_prog}, 待做 {pending})"
        )

        return json.dumps({
            "total": total,
            "completed": completed,
            "in_progress": in_prog,
            "pending": pending,
        }, ensure_ascii=False)
