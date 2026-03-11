# translator/format_engine.py
import json
import os
from typing import Dict, Any, Optional
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：格式映射引擎
# =============================================================
# 翻译后最头疼的问题：格式怎么办？
#
# 中文文档常用字体：宋体、黑体、微软雅黑、楷体
# 英文文档常用字体：Times New Roman、Arial、Calibri
#
# 我们的策略是"规则映射"：
#   - 维护一张"中文字体 → 英文字体"的映射表
#   - 还可以按段落样式映射（如"标题1"用 Arial Bold，"正文"用 Times New Roman）
#   - 用户可以通过对话自定义规则，规则会持久化到 JSON 文件
#   - 如果没有匹配的规则，保持原字体不变
# =============================================================

logger = get_logger("format_engine")

# 默认的字体映射规则
DEFAULT_FONT_MAP = {
    "宋体": "Times New Roman",
    "黑体": "Arial",
    "微软雅黑": "Calibri",
    "楷体": "Georgia",
    "仿宋": "Palatino Linotype",
    "等线": "Calibri",
}

# 默认的样式映射规则（按 Word 样式名）
DEFAULT_STYLE_MAP = {
    "Heading 1": {"font_name": "Arial", "bold": True},
    "Heading 2": {"font_name": "Arial", "bold": True},
    "Heading 3": {"font_name": "Arial", "bold": True},
    "Normal": {"font_name": "Times New Roman"},
}


class FormatEngine:
    """
    格式映射引擎：管理翻译前后的字体/样式映射规则。

    规则优先级（从高到低）：
    1. 用户自定义规则（通过对话设置，持久化到文件）
    2. 默认规则（DEFAULT_FONT_MAP / DEFAULT_STYLE_MAP）
    3. 保持原格式不变（兜底）
    """

    def __init__(self, config_path: str = "translator_config/format_rules.json"):
        self.config_path = config_path
        self.font_map: Dict[str, str] = dict(DEFAULT_FONT_MAP)
        self.style_map: Dict[str, Dict] = dict(DEFAULT_STYLE_MAP)
        self._load_user_rules()

    def _load_user_rules(self):
        """从文件加载用户自定义规则"""
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 用户规则覆盖默认规则
            self.font_map.update(data.get("font_map", {}))
            self.style_map.update(data.get("style_map", {}))
            logger.info(f"已加载用户格式规则: {self.config_path}")
        except Exception as e:
            logger.warning(f"加载格式规则失败: {e}")

    def _save_user_rules(self):
        """持久化用户自定义规则"""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        data = {
            "font_map": self.font_map,
            "style_map": self.style_map,
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"格式规则已保存: {self.config_path}")

    def set_font_mapping(self, source_font: str, target_font: str):
        """设置字体映射规则（用户通过对话调用）"""
        self.font_map[source_font] = target_font
        self._save_user_rules()
        logger.info(f"字体映射已更新: {source_font} -> {target_font}")

    def set_style_mapping(self, style_name: str, format_dict: Dict[str, Any]):
        """
        设置样式映射规则。
        format_dict 示例: {"font_name": "Arial", "bold": True, "font_size": 14}
        """
        self.style_map[style_name] = format_dict
        self._save_user_rules()
        logger.info(f"样式映射已更新: {style_name} -> {format_dict}")

    def resolve_font(
        self,
        original_font: Optional[str],
        style_name: Optional[str] = None,
    ) -> Optional[str]:
        """
        根据规则解析目标字体。

        查找顺序：
        1. 样式映射（如果段落样式名匹配）
        2. 字体映射（如果原字体名匹配）
        3. 返回 None（保持原样）
        """
        # 优先看样式映射
        if style_name and style_name in self.style_map:
            style_rule = self.style_map[style_name]
            if "font_name" in style_rule:
                return style_rule["font_name"]

        # 再看字体映射
        if original_font and original_font in self.font_map:
            return self.font_map[original_font]

        return None

    def resolve_format(
        self,
        original_format: Dict[str, Any],
        style_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        解析完整的目标格式（字体 + 加粗/斜体等）。
        返回一个新的 format dict，未映射的字段保持原值。
        """
        result = dict(original_format)

        # 应用样式映射
        if style_name and style_name in self.style_map:
            style_rule = self.style_map[style_name]
            for key, value in style_rule.items():
                result[key] = value

        # 应用字体映射（如果样式映射没有覆盖字体）
        original_font = original_format.get("font_name")
        if original_font and original_font in self.font_map:
            # 只在样式映射没设置字体时才用字体映射
            if not (style_name and style_name in self.style_map
                    and "font_name" in self.style_map[style_name]):
                result["font_name"] = self.font_map[original_font]

        return result

    def get_rules_summary(self) -> str:
        """返回当前规则的可读摘要（给 LLM 或用户看）"""
        lines = ["【当前字体映射规则】"]
        for src, tgt in self.font_map.items():
            lines.append(f"  {src} -> {tgt}")
        lines.append("\n【当前样式映射规则】")
        for style, rule in self.style_map.items():
            lines.append(f"  {style} -> {rule}")
        return "\n".join(lines)
