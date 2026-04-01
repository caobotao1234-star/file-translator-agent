# agent_main.py
# =============================================================
# 📘 教学笔记：新 Agent 架构入口（Phase 1 验证）
# =============================================================
# 这是新 Agent Loop 的命令行入口，用于验证 Phase 1 能跑通。
# 用法: volcengine\Scripts\python.exe agent_main.py <文件路径> [目标语言]
#
# 与旧架构的区别：
#   旧: translator_main.py → TranslatorAgent → Pipeline/ScanAgent
#   新: agent_main.py → AgentLoop（模型自己决定怎么翻译）
# =============================================================

import os
import sys
import json

from config.settings import Config
from core.logger import get_logger
from core.llm_router import LLMRouter
from core.agent_loop import AgentLoop
from translator.translate_pipeline import TranslatePipeline
from translator.format_engine import FormatEngine

from tools.doc_tools import ParseDocumentTool, GetPageContentTool, WriteDocumentTool
from tools.translate_tools import TranslatePageTool
from tools.memory_tools import MemoryStore, ReadMemoryTool, UpdateMemoryTool
from tools.interaction_tools import AskUserTool, ReportProgressTool
from tools.format_tools import InspectOutputTool, AdjustFormatTool
from tools.vision_tools import GetPageImageTool, create_scan_tools
from tools.layout_tools_v2 import RenderSlideTool, EnableAutofitTool, CompareLayoutTool, SmartResizeTool
from prompts.agent_prompts import TRANSLATION_AGENT_PROMPT

logger = get_logger("agent_main")
OUTPUT_DIR = "output"


def on_message(role: str, content: str):
    """Agent 输出回调"""
    if role == "assistant":
        print(f"\n🤖 Agent: {content}\n", flush=True)
    elif role == "user":
        print(f"\n👤 用户: {content}\n", flush=True)


def on_tool_call(tool_name: str, params: dict):
    """工具调用回调"""
    # 简化显示
    params_short = str(params)[:100]
    print(f"  🔧 {tool_name}({params_short})", flush=True)


def on_progress(current: int, total: int, message: str):
    """进度回调"""
    if total > 0:
        print(f"  📊 [{current}/{total}] {message}", flush=True)
    else:
        print(f"  📊 {message}", flush=True)


def build_agent(translate_model_id: str = None, brain_model_id: str = None,
                image_model_id: str = None):
    """
    📘 构建 Agent：初始化模型 + 工具 + Agent Loop

    brain_model_id: Agent 主模型（负责理解、规划、决策）
    translate_model_id: 翻译工具内部用的便宜模型
    image_model_id: 图片生成模型（扫描件保留背景用）
    """
    # ── 1. 模型初始化 ──
    router = LLMRouter(api_key=Config.ARK_API_KEY)

    # 翻译模型（工具内部用）
    t_model = translate_model_id or Config.DEFAULT_MODEL_ID
    router.register_model("translate", model_str=t_model)
    print(f"翻译模型: {t_model}", flush=True)

    # Agent 主模型（Brain）
    b_model = brain_model_id
    if not b_model:
        brain_cfg = Config.get_agent_brain_config()
        if brain_cfg:
            b_model = f"{brain_cfg['provider']}:{brain_cfg['model']}"
    if b_model:
        router.register_model("agent_brain", model_str=b_model)
        print(f"Agent 主模型: {b_model}", flush=True)
    else:
        router.register_model("agent_brain", model_str=t_model)
        print(f"Agent 主模型: {t_model}（与翻译模型相同）", flush=True)

    brain_engine = router.get("agent_brain")

    # 图片生成模型（可选）
    image_gen_engine = None
    if image_model_id:
        router.register_model("image_gen", model_str=image_model_id)
        image_gen_engine = router.get("image_gen")
        print(f"图片生成模型: {image_model_id}", flush=True)

    # ── 2. 翻译 Pipeline（工具内部用） ──
    pipeline = TranslatePipeline(
        translate_llm=router.get("translate"),
        batch_size=20,
        max_workers=1,
    )

    # ── 3. 格式引擎 ──
    format_engine = FormatEngine()

    # ── 4. 工具初始化 ──
    parse_tool = ParseDocumentTool(format_engine=format_engine)
    page_image_tool = GetPageImageTool()
    memory = MemoryStore()

    # 📘 parse_document 完成后，如果是 PDF，自动渲染页面图片
    # 通过回调机制让 parse_tool 触发 page_image_tool 的加载
    parse_tool._page_image_tool = page_image_tool

    tools = [
        parse_tool,
        GetPageContentTool(parse_tool),
        page_image_tool,
        WriteDocumentTool(parse_tool, format_engine),
        TranslatePageTool(translate_pipeline=pipeline),
        InspectOutputTool(),
        AdjustFormatTool(),
        RenderSlideTool(),
        EnableAutofitTool(),
        CompareLayoutTool(),
        SmartResizeTool(),
        ReadMemoryTool(memory),
        UpdateMemoryTool(memory),
        AskUserTool(),
        ReportProgressTool(on_progress=on_progress),
    ]

    # 📘 扫描件工具（OCR/CV/图片生成/文字覆盖/裁剪）
    # 这些工具复用旧架构的实现，Agent 自己决定是否使用
    scan_tools, scan_context = create_scan_tools(
        page_image_tool=page_image_tool,
        image_gen_engine=image_gen_engine,
    )
    tools.extend(scan_tools)
    # 📘 把 scan_context 挂到 parse_tool 上，供 write_document 使用
    parse_tool._scan_context = scan_context

    # ── 5. Agent Loop ──
    agent = AgentLoop(
        llm_engine=brain_engine,
        tools=tools,
        system_prompt=TRANSLATION_AGENT_PROMPT,
        on_message=on_message,
        on_tool_call=on_tool_call,
    )

    return agent


def main():
    if len(sys.argv) < 2:
        print("用法: python agent_main.py <文件路径> [目标语言] [--brain MODEL]")
        print("示例: python agent_main.py test.pptx 英文")
        print("示例: python agent_main.py test.pptx 英文 --brain doubao-seed-2-0-pro-260215")
        sys.exit(1)

    filepath = sys.argv[1]
    target_lang = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "英文"

    # 📘 --brain 参数：指定 Agent 主模型（默认从 .env 读取）
    brain_override = None
    image_override = None
    for i, arg in enumerate(sys.argv):
        if arg == "--brain" and i + 1 < len(sys.argv):
            brain_override = sys.argv[i + 1]
        if arg == "--image" and i + 1 < len(sys.argv):
            image_override = sys.argv[i + 1]

    if not os.path.exists(filepath):
        print(f"文件不存在: {filepath}")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 构建输出路径
    basename = os.path.splitext(os.path.basename(filepath))[0]
    ext = os.path.splitext(filepath)[1]
    output_path = os.path.join(OUTPUT_DIR, f"{basename}_agent{ext}")

    print("=" * 50)
    print("🤖 翻译 Agent（新架构）")
    print("=" * 50)
    print(f"输入: {filepath}")
    print(f"输出: {output_path}")
    print(f"目标语言: {target_lang}")
    print("=" * 50)

    agent = build_agent(brain_model_id=brain_override, image_model_id=image_override)

    # 📘 给 Agent 一条自然语言指令，让它自己干活
    user_message = (
        f"请翻译这个文档。\n"
        f"文件路径: {os.path.abspath(filepath)}\n"
        f"目标语言: {target_lang}\n"
        f"输出路径: {os.path.abspath(output_path)}\n"
        f"要求: 翻译准确地道，排版与原文一致。"
    )

    result = agent.run(user_message)

    print("\n" + "=" * 50)
    print(f"🤖 Agent 完成")
    print(f"轮次: {agent.stats['turns']}")
    print(f"工具调用: {agent.stats['tool_calls']}")
    print(f"Tokens: {agent.stats['prompt_tokens']} + {agent.stats['completion_tokens']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
