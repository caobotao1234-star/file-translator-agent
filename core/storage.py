# core/storage.py
import json
import os
from datetime import datetime
from typing import List, Dict, Optional

# =============================================================
# 📘 教学笔记：对话持久化（Conversation Persistence）
# =============================================================
# 为什么需要持久化？
#   - 没有持久化的 Agent 就像一个"金鱼"，每次重启都失忆
#   - 用户可能花了很长时间和 Agent 建立了上下文，一关程序全没了
#   - 生产级 Agent 必须能"断点续聊"
#
# 设计思路：
#   - 每个会话（session）是一个独立的 JSON 文件
#   - 文件名 = session_id（默认用时间戳生成，保证唯一）
#   - 保存内容：消息列表 + 长期记忆摘要 + 元信息（创建时间等）
#   - 存储目录默认为项目根目录下的 chat_history/
#
# 为什么用 JSON 文件而不是数据库？
#   - 学习阶段，JSON 最直观，你可以直接打开文件看内容
#   - 后续升级到 SQLite 或 Redis 只需要替换这个 Storage 类
#   - 这就是解耦的好处：Agent 不关心数据存在哪，只调用 Storage 接口
# =============================================================


class ChatStorage:
    """对话历史的本地文件存储引擎"""

    def __init__(self, storage_dir: str = "chat_history"):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)

    def _get_filepath(self, session_id: str) -> str:
        """根据 session_id 拼出文件路径"""
        return os.path.join(self.storage_dir, f"{session_id}.json")

    def save(
        self,
        session_id: str,
        messages: List[Dict],
        memory_summary: str = "",
    ):
        """
        保存一个会话的完整状态。
        
        保存的数据结构：
        {
            "session_id": "20260311_143022",
            "updated_at": "2026-03-11 14:35:00",
            "memory_summary": "用户喜欢Python，正在学Agent开发...",
            "messages": [...]
        }
        """
        data = {
            "session_id": session_id,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "memory_summary": memory_summary,
            "messages": messages,
        }

        filepath = self._get_filepath(session_id)
        # 先写临时文件再重命名，防止写到一半断电导致文件损坏
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 原子替换（Windows 上需要先删旧文件）
        if os.path.exists(filepath):
            os.remove(filepath)
        os.rename(tmp_path, filepath)

    def load(self, session_id: str) -> Optional[Dict]:
        """加载一个会话，返回 None 表示不存在"""
        filepath = self._get_filepath(session_id)
        if not os.path.exists(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_sessions(self) -> List[Dict]:
        """
        列出所有已保存的会话，按更新时间倒序排列。
        返回格式：[{"session_id": "...", "updated_at": "...", "message_count": 5}, ...]
        """
        sessions = []
        for filename in os.listdir(self.storage_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(self.storage_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append({
                    "session_id": data.get("session_id", filename[:-5]),
                    "updated_at": data.get("updated_at", "未知"),
                    "message_count": len(data.get("messages", [])),
                })
            except (json.JSONDecodeError, KeyError):
                continue  # 跳过损坏的文件

        # 按更新时间倒序
        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        return sessions

    def delete(self, session_id: str) -> bool:
        """删除一个会话"""
        filepath = self._get_filepath(session_id)
        if os.path.exists(filepath):
            os.remove(filepath)
            return True
        return False

    @staticmethod
    def generate_session_id() -> str:
        """生成一个基于时间戳的 session_id"""
        return datetime.now().strftime("%Y%m%d_%H%M%S")
