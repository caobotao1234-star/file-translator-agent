# core/skill_loader.py
# =============================================================
# 📘 教学笔记：Skill 按需加载器
# =============================================================
# 参考 Anthropic Claude Code 的 Skill 设计：
# - Skill 是 Markdown 文件，包含特定任务的专业经验
# - 按需加载：只在匹配到相关任务时注入 context
# - 省 token：不加载无关的 Skill
# - 提升注意力：模型只看到当前任务需要的指令
# =============================================================

import os
import re
from typing import Dict, List, Optional

from core.logger import get_logger

logger = get_logger("skill_loader")

SKILLS_DIR = "skills"


class Skill:
    """一个 Skill 的数据结构"""

    def __init__(self, name: str, trigger: str, description: str, content: str, filepath: str):
        self.name = name
        self.trigger = trigger  # 触发条件表达式
        self.description = description
        self.content = content  # Markdown 正文（不含 frontmatter）
        self.filepath = filepath

    def __repr__(self):
        return f"Skill({self.name}, trigger={self.trigger})"


class SkillLoader:
    """
    📘 Skill 按需加载器

    扫描 skills/ 目录，解析 Markdown frontmatter，
    根据触发条件动态加载相关 Skill 到 Agent 的 context 中。
    """

    def __init__(self, skills_dir: str = SKILLS_DIR):
        self.skills_dir = skills_dir
        self.skills: List[Skill] = []
        self._loaded_skills: set = set()  # 已加载的 Skill 名称（避免重复）
        self._scan_skills()

    def _scan_skills(self):
        """扫描 skills/ 目录，加载所有 Skill 定义"""
        if not os.path.isdir(self.skills_dir):
            logger.info(f"Skills 目录不存在: {self.skills_dir}")
            return

        for filename in os.listdir(self.skills_dir):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(self.skills_dir, filename)
            try:
                skill = self._parse_skill(filepath)
                if skill:
                    self.skills.append(skill)
            except Exception as e:
                logger.warning(f"解析 Skill 失败: {filepath}: {e}")

        logger.info(f"已扫描 {len(self.skills)} 个 Skills: {[s.name for s in self.skills]}")

    def _parse_skill(self, filepath: str) -> Optional[Skill]:
        """解析 Skill Markdown 文件（YAML frontmatter + 正文）"""
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # 解析 YAML frontmatter（简单实现，不依赖 pyyaml）
        frontmatter = {}
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                fm_text = parts[1].strip()
                body = parts[2].strip()
                for line in fm_text.split("\n"):
                    if ":" in line:
                        key, val = line.split(":", 1)
                        frontmatter[key.strip()] = val.strip()

        name = frontmatter.get("name", os.path.splitext(os.path.basename(filepath))[0])
        trigger = frontmatter.get("trigger", "")
        description = frontmatter.get("description", "")

        if not body:
            return None

        return Skill(
            name=name,
            trigger=trigger,
            description=description,
            content=body,
            filepath=filepath,
        )

    def match_skills(self, context: Dict[str, str]) -> List[Skill]:
        """
        📘 根据上下文匹配相关 Skill

        context 示例: {"doc_type": "PPT", "phase": "translate"}
        trigger 示例: 'doc_type == "PPT"', 'after_write'

        返回匹配的 Skill 列表（排除已加载的）。
        """
        matched = []
        for skill in self.skills:
            if skill.name in self._loaded_skills:
                continue
            if self._evaluate_trigger(skill.trigger, context):
                matched.append(skill)
        return matched

    def _evaluate_trigger(self, trigger: str, context: Dict[str, str]) -> bool:
        """简单的触发条件评估"""
        if not trigger:
            return False

        # 支持 key == "value" 格式
        match = re.match(r'(\w+)\s*==\s*"([^"]*)"', trigger)
        if match:
            key, value = match.groups()
            return context.get(key) == value

        # 支持简单关键词匹配
        for key, value in context.items():
            if trigger in str(value):
                return True

        return False

    def load_skill(self, skill: Skill) -> str:
        """加载一个 Skill，返回要注入到 context 的文本"""
        self._loaded_skills.add(skill.name)
        logger.info(f"加载 Skill: {skill.name}")
        return f"\n## Skill: {skill.name}\n{skill.content}\n"

    def get_skill_descriptions(self) -> str:
        """返回所有 Skill 的简短描述（用于 system prompt）"""
        if not self.skills:
            return ""
        lines = ["你有以下专业技能包（按需加载，不需要全部使用）："]
        for s in self.skills:
            lines.append(f"- {s.name}: {s.description}")
        return "\n".join(lines)

    def reset(self):
        """重置已加载状态（新任务时调用）"""
        self._loaded_skills.clear()
