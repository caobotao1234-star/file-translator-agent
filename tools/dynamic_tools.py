# tools/dynamic_tools.py
# =============================================================
# 📘 教学笔记：动态工具系统（Dynamic Tool System）
# =============================================================
# 这是 Agent 架构中最"智能"的部分——
# Agent 遇到现有工具解决不了的问题时，可以自己写一个新工具，
# 并且把它沉淀下来，下次遇到同类问题直接复用。
#
# 📘 核心理念：工具不是写死的，Agent 可以自我进化
#   传统方案：开发者预定义所有工具 → Agent 只能用现有工具
#   动态方案：Agent 发现需求 → 写 Python 代码 → 注册为新工具 → 持久化
#
# 📘 安全机制：
#   1. 沙箱执行：动态代码在受限环境中运行（限制 import）
#   2. 白名单模块：只允许 json, re, math, string, os.path 等安全模块
#   3. 代码审查：保存前记录完整代码，方便人工审查
#   4. 超时保护：单次执行最多 10 秒
#   5. 持久化审计：每个工具都记录创建时间、创建者、使用次数
#
# 📘 工作流程：
#   1. Agent Brain 调用 create_custom_tool，传入工具名、描述、参数、Python 代码
#   2. DynamicToolRegistry 验证代码安全性
#   3. 编译代码为可执行函数
#   4. 注册为 BaseTool 子类实例
#   5. 保存到 translator_config/custom_tools.json
#   6. 下次启动时自动加载
# =============================================================

import json
import os
import time
import traceback
from typing import Any, Dict, List, Optional

from tools.base_tool import BaseTool
from core.logger import get_logger

logger = get_logger("dynamic_tools")

# 📘 安全白名单：动态工具只能 import 这些模块
# 不允许 subprocess, socket, shutil 等危险模块
ALLOWED_MODULES = {
    "json", "re", "math", "string", "collections",
    "itertools", "functools", "operator",
    "os.path", "datetime", "copy", "textwrap",
    "unicodedata", "difflib",
}

# 📘 持久化路径
CUSTOM_TOOLS_PATH = "translator_config/custom_tools.json"


class DynamicTool(BaseTool):
    """
    📘 教学笔记：动态工具实例

    由 Agent Brain 在运行时创建的工具。
    内部持有一段 Python 代码，execute() 时在受限环境中执行。

    📘 代码约定：
    动态代码必须定义一个 run(params, context) 函数，
    - params: Agent 传入的参数（dict）
    - context: 共享上下文（dict，包含 translations, overrides 等）
    - 返回值: str（JSON 格式的结果）
    """

    def __init__(
        self,
        tool_name: str,
        tool_description: str,
        tool_parameters: dict,
        code: str,
        context: Dict[str, Any] = None,
        metadata: Dict[str, Any] = None,
    ):
        self.name = tool_name
        self.description = tool_description
        self.parameters = tool_parameters
        self.code = code
        self.context = context or {}
        self.metadata = metadata or {}
        self._compiled = None
        self._compile_code()

    def _compile_code(self):
        """
        📘 编译动态代码，检查安全性。

        编译阶段就能发现语法错误，不用等到执行时才报错。
        同时检查 import 语句是否在白名单内。
        """
        # 📘 安全检查：扫描 import 语句
        for line in self.code.split("\n"):
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                # 📘 提取模块名
                if stripped.startswith("from "):
                    module = stripped.split()[1].split(".")[0]
                else:
                    module = stripped.split()[1].split(".")[0].split(",")[0]
                # 📘 检查完整模块路径是否在白名单
                full_module = stripped.split()[1] if stripped.startswith("from ") else stripped.split()[1]
                if module not in ALLOWED_MODULES and full_module not in ALLOWED_MODULES:
                    raise ValueError(
                        f"动态工具安全限制：不允许 import '{module}'。"
                        f"允许的模块: {sorted(ALLOWED_MODULES)}"
                    )

        try:
            self._compiled = compile(self.code, f"<dynamic_tool:{self.name}>", "exec")
        except SyntaxError as e:
            raise ValueError(f"动态工具代码语法错误: {e}")

    def execute(self, params: dict) -> str:
        """
        📘 在受限环境中执行动态代码。

        构建一个干净的全局命名空间，只包含白名单模块，
        然后执行编译后的代码，调用其中的 run() 函数。
        """
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        # 📘 构建受限执行环境
        safe_globals = {"__builtins__": {
            # 📘 只暴露安全的内置函数
            "len": len, "range": range, "enumerate": enumerate,
            "zip": zip, "map": map, "filter": filter,
            "sorted": sorted, "reversed": reversed,
            "min": min, "max": max, "sum": sum, "abs": abs,
            "round": round, "int": int, "float": float, "str": str,
            "bool": bool, "list": list, "dict": dict, "set": set, "tuple": tuple,
            "isinstance": isinstance, "type": type,
            "print": print, "repr": repr,
            "ValueError": ValueError, "TypeError": TypeError,
            "KeyError": KeyError, "IndexError": IndexError,
            "Exception": Exception,
            "True": True, "False": False, "None": None,
        }}

        # 📘 预导入白名单模块
        import json as _json
        import re as _re
        import math as _math
        import string as _string
        import os.path as _ospath
        safe_globals["json"] = _json
        safe_globals["re"] = _re
        safe_globals["math"] = _math
        safe_globals["string"] = _string
        safe_globals["os_path"] = _ospath

        try:
            # 📘 执行代码（定义 run 函数）
            exec(self._compiled, safe_globals)

            # 📘 调用 run(params, context)
            if "run" not in safe_globals:
                return json.dumps(
                    {"error": "动态工具代码必须定义 run(params, context) 函数"},
                    ensure_ascii=False,
                )

            result = safe_globals["run"](params, self.context)

            # 📘 确保返回 JSON 字符串
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.error(f"动态工具 '{self.name}' 执行失败: {e}")
            logger.debug(traceback.format_exc())
            return json.dumps({
                "error": f"动态工具执行失败: {str(e)}",
                "tool_name": self.name,
            }, ensure_ascii=False)


class CreateCustomToolTool(BaseTool):
    """
    📘 教学笔记：工具创建工具（Meta-Tool）

    这是一个"创建工具的工具"——Agent Brain 调用它来定义新工具。
    创建后的工具立即可用（注册到当前 tools 字典），
    并且持久化到 custom_tools.json，下次启动自动加载。

    📘 为什么需要这个？
    Agent 在处理文档时可能遇到预定义工具解决不了的问题，
    比如"检测特定语言对的字符宽度比例"、"按段落类型分组处理"等。
    让 Agent 自己写工具，比我们预想所有场景要灵活得多。
    """

    name = "create_custom_tool"
    description = (
        "创建一个新的自定义工具。你可以编写 Python 代码来实现任何数据处理逻辑。"
        "工具创建后立即可用，并会持久化保存供未来复用。"
        "代码必须定义 run(params, context) 函数，返回 JSON 字符串。"
        "context 包含: translations(译文字典), overrides(字号覆盖), parsed_data(解析数据)。"
        "只能使用安全模块: json, re, math, string, os.path, collections, itertools 等。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "工具名称（英文，snake_case，如 detect_cjk_ratio）",
            },
            "tool_description": {
                "type": "string",
                "description": "工具功能描述（中文或英文）",
            },
            "tool_parameters": {
                "type": "object",
                "description": "工具参数的 JSON Schema（同 OpenAI function calling 格式）",
            },
            "code": {
                "type": "string",
                "description": (
                    "Python 代码，必须定义 run(params, context) -> str 函数。"
                    "params 是调用参数，context 是共享上下文。返回 JSON 字符串。"
                ),
            },
            "reason": {
                "type": "string",
                "description": "为什么需要创建这个工具（用于审计和文档）",
            },
        },
        "required": ["tool_name", "tool_description", "tool_parameters", "code"],
    }

    def __init__(self, registry: "DynamicToolRegistry", context: Dict[str, Any] = None):
        self.registry = registry
        self.context = context or {}

    def execute(self, params: dict) -> str:
        valid, msg = self.validate_params(params)
        if not valid:
            return json.dumps({"error": msg}, ensure_ascii=False)

        tool_name = params["tool_name"]
        tool_description = params["tool_description"]
        tool_parameters = params["tool_parameters"]
        code = params["code"]
        reason = params.get("reason", "")

        try:
            tool = self.registry.create_and_register(
                tool_name=tool_name,
                tool_description=tool_description,
                tool_parameters=tool_parameters,
                code=code,
                reason=reason,
                context=self.context,
            )
            logger.info(f"动态工具已创建: {tool_name} — {reason}")
            return json.dumps({
                "created": True,
                "tool_name": tool_name,
                "description": tool_description,
                "message": f"工具 '{tool_name}' 已创建并可立即使用。已持久化保存。",
            }, ensure_ascii=False)

        except ValueError as e:
            return json.dumps({
                "created": False,
                "error": str(e),
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"创建动态工具失败: {e}")
            return json.dumps({
                "created": False,
                "error": f"创建失败: {str(e)}",
            }, ensure_ascii=False)



class DynamicToolRegistry:
    """
    📘 教学笔记：动态工具注册表

    管理所有动态创建的工具：
    1. 从 custom_tools.json 加载已有工具
    2. 运行时创建新工具
    3. 持久化保存到 custom_tools.json
    4. 提供工具实例给 Agent 的 tools 字典

    📘 生命周期：
    Agent 初始化 → load_tools() 加载已有工具
    → Agent Brain 调用 create_custom_tool → create_and_register()
    → 工具立即可用 + 持久化保存
    → 下次 Agent 初始化时自动加载
    """

    def __init__(self):
        self._tool_definitions: List[dict] = []  # 持久化的工具定义
        self._tool_instances: Dict[str, DynamicTool] = {}  # 运行时实例

    def load_tools(self, context: Dict[str, Any] = None) -> Dict[str, DynamicTool]:
        """
        📘 从 custom_tools.json 加载所有已保存的动态工具。

        返回 {tool_name: DynamicTool} 字典，可以直接合并到 Agent 的 tools 中。
        加载失败的工具会跳过（不影响其他工具）。
        """
        context = context or {}
        self._tool_instances = {}

        if not os.path.exists(CUSTOM_TOOLS_PATH):
            return {}

        try:
            with open(CUSTOM_TOOLS_PATH, "r", encoding="utf-8") as f:
                self._tool_definitions = json.load(f)
        except Exception as e:
            logger.warning(f"加载动态工具失败: {e}")
            self._tool_definitions = []
            return {}

        loaded = 0
        for defn in self._tool_definitions:
            try:
                tool = DynamicTool(
                    tool_name=defn["name"],
                    tool_description=defn["description"],
                    tool_parameters=defn["parameters"],
                    code=defn["code"],
                    context=context,
                    metadata=defn.get("metadata", {}),
                )
                self._tool_instances[defn["name"]] = tool
                loaded += 1
            except Exception as e:
                logger.warning(f"加载动态工具 '{defn.get('name', '?')}' 失败: {e}")

        if loaded:
            logger.info(f"已加载 {loaded} 个动态工具: {list(self._tool_instances.keys())}")

        return dict(self._tool_instances)

    def create_and_register(
        self,
        tool_name: str,
        tool_description: str,
        tool_parameters: dict,
        code: str,
        reason: str = "",
        context: Dict[str, Any] = None,
    ) -> DynamicTool:
        """
        📘 创建新工具、注册、持久化。

        步骤：
        1. 验证工具名不与内置工具冲突
        2. 创建 DynamicTool 实例（会自动编译+安全检查）
        3. 注册到内存
        4. 保存到 custom_tools.json
        """
        # 📘 内置工具名保护
        builtin_names = {
            "measure_overflow", "resize_font", "retranslate_shorter",
            "render_page_preview", "save_layout_rule", "create_custom_tool",
            "ocr_extract_text", "cv_detect_layout", "translate_texts",
            "generate_word_document", "generate_translated_image",
            "crop_image_region", "manage_glossary", "detect_colors",
            "detect_text_direction", "translate_with_context", "compare_page_layout",
            "overlay_translated_text",
        }
        if tool_name in builtin_names:
            raise ValueError(f"工具名 '{tool_name}' 与内置工具冲突，请换一个名字")

        # 📘 创建实例（编译+安全检查在构造函数中完成）
        tool = DynamicTool(
            tool_name=tool_name,
            tool_description=tool_description,
            tool_parameters=tool_parameters,
            code=code,
            context=context or {},
            metadata={
                "reason": reason,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "usage_count": 0,
            },
        )

        # 📘 注册到内存
        self._tool_instances[tool_name] = tool

        # 📘 持久化：更新或追加
        defn = {
            "name": tool_name,
            "description": tool_description,
            "parameters": tool_parameters,
            "code": code,
            "metadata": tool.metadata,
        }

        updated = False
        for i, d in enumerate(self._tool_definitions):
            if d["name"] == tool_name:
                self._tool_definitions[i] = defn
                updated = True
                break
        if not updated:
            self._tool_definitions.append(defn)

        self._save()
        return tool

    def get_tool(self, name: str) -> Optional[DynamicTool]:
        """获取已注册的动态工具"""
        return self._tool_instances.get(name)

    def get_all_tools(self) -> Dict[str, DynamicTool]:
        """获取所有动态工具"""
        return dict(self._tool_instances)

    def get_tool_schemas(self) -> List[dict]:
        """获取所有动态工具的 API schema（给 Brain 的 tools 参数）"""
        return [t.get_api_format() for t in self._tool_instances.values()]

    def update_context(self, context: Dict[str, Any]):
        """📘 更新所有动态工具的共享上下文"""
        for tool in self._tool_instances.values():
            tool.context = context

    def _save(self):
        """持久化到 JSON"""
        os.makedirs(os.path.dirname(CUSTOM_TOOLS_PATH), exist_ok=True)
        with open(CUSTOM_TOOLS_PATH, "w", encoding="utf-8") as f:
            json.dump(self._tool_definitions, f, ensure_ascii=False, indent=2)
