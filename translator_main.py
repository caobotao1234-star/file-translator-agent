# translator_main.py — 翻译 Agent 入口
from translator.translator_agent import TranslatorAgent

# =============================================================
# 📘 教学笔记：翻译 Agent 的交互设计
# =============================================================
# 翻译 Agent 的交互模式和通用 Agent 不同：
#   - 通用 Agent：纯对话，用户打字，AI 回复
#   - 翻译 Agent：文件驱动，用户给文件路径，AI 翻译并输出文件
#
# 但我们也保留了对话能力：
#   - 用户可以通过命令修改格式规则
#   - 用户可以查看当前配置
#   - 未来可以接入通用 Agent 做更复杂的交互
# =============================================================


SUPPORTED_LANGS = [
    ("中文", "Chinese"), ("英文", "English"), ("日文", "Japanese"), ("韩文", "Korean"),
    ("法文", "French"), ("德文", "German"), ("西班牙文", "Spanish"), ("俄文", "Russian"),
]


def print_help(target_lang: str = None):
    lang_display = target_lang or "未设置"
    print(f"""
╔═══════════════════════════════════════════════════════╗
║              📖 翻译 Agent 使用指南                   ║
╠═══════════════════════════════════════════════════════╣
║                                                       ║
║  翻译文档：                                           ║
║    直接输入文件路径即可开始翻译                       ║
║    支持: .docx (Word)  .pptx (PowerPoint)            ║
║    例: test.docx / slides.pptx                        ║
║                                                       ║
║  语言设置：                                           ║
║    /lang               查看/切换目标语言              ║
║    /lang 英文          直接设置目标语言               ║
║    当前目标语言: {lang_display:<36s}║
║                                                       ║
║  格式规则命令：                                       ║
║    /rules              查看当前格式映射规则           ║
║    /font 宋体 Arial    设置字体映射                   ║
║    /style Heading 1 Times New Roman                   ║
║                        设置样式字体                   ║
║                                                       ║
║  其他：                                               ║
║    /help               显示此帮助                     ║
║    exit                退出程序                       ║
║                                                       ║
╚═══════════════════════════════════════════════════════╝
""")


def _select_target_lang() -> str:
    """交互式选择目标语言"""
    print("\n[🌐 请选择目标语言]")
    for i, (cn, en) in enumerate(SUPPORTED_LANGS, 1):
        print(f"  {i}. {cn} ({en})")
    print(f"  或直接输入其他语言名称")

    while True:
        choice = input("  请选择 (数字或语言名): ").strip()
        if not choice:
            continue
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(SUPPORTED_LANGS):
                return SUPPORTED_LANGS[idx][0]
            print("  [⚠️] 序号超出范围，请重新选择")
            continue
        return choice


def main():
    print("--- 🚀 正在初始化翻译 Agent ---")

    agent = TranslatorAgent(
        batch_size=20,
        debug=True,
    )

    print("--- ✅ 翻译 Agent 启动完毕 ---")

    if agent.com_enabled:
        print("--- 🖥️ COM 增强模式: ✅ 已开启（支持图表/文本框/SmartArt 翻译）---")
    else:
        print("--- 🖥️ COM 增强模式: ❌ 未开启（仅翻译段落和表格文字）---")
        print("---    提示: 安装 Microsoft Office 并安装 pywin32 可开启增强模式 ---")

    # 📘 教学笔记：目标语言由用户决定
    # 源语言不需要用户选——LLM 自己能识别原文是什么语言。
    # 但目标语言必须用户指定，因为中英混合文档无法自动判断"翻成什么"。
    target_lang = None

    print_help(target_lang)

    while True:
        user_input = input("\n[🧑 用户]: ").strip()
        if not user_input:
            continue

        if user_input.lower() in ["exit", "quit"]:
            print("再见！")
            break

        if user_input == "/help":
            print_help(target_lang)
            continue

        if user_input == "/rules":
            agent.show_format_rules()
            continue

        # /lang 或 /lang 英文
        if user_input == "/lang" or user_input.startswith("/lang "):
            arg = user_input[5:].strip() if len(user_input) > 5 else ""
            if arg:
                target_lang = arg
                print(f"[🌐 目标语言] 已设置为: {target_lang}")
            else:
                if target_lang:
                    print(f"[🌐 目标语言] 当前: {target_lang}")
                target_lang = _select_target_lang()
                print(f"[🌐 目标语言] 已设置为: {target_lang}")
            continue

        # /font 宋体 Arial
        if user_input.startswith("/font "):
            parts = user_input[6:].strip().split(maxsplit=1)
            if len(parts) == 2:
                agent.update_font_rule(parts[0], parts[1])
            else:
                print("[⚠️ 用法] /font <源字体> <目标字体>")
            continue

        # /style Heading 1 Times New Roman
        if user_input.startswith("/style "):
            parts = user_input[7:].strip().rsplit(maxsplit=1)
            if len(parts) == 2:
                agent.update_style_rule(parts[0].strip(), font_name=parts[1].strip())
            else:
                print("[⚠️ 用法] /style <样式名> <字体名>")
            continue

        if user_input.startswith("/"):
            print("[⚠️ 提示] 未知命令，输入 /help 查看帮助")
            continue

        # ---- 文件翻译 ----
        filepath = user_input.strip('"').strip("'")
        if filepath.endswith((".docx", ".pptx")):
            # 首次翻译时必须先选目标语言
            if target_lang is None:
                print("[🌐 提示] 首次翻译，请先选择目标语言")
                target_lang = _select_target_lang()
                print(f"[🌐 目标语言] 已设置为: {target_lang}")

            try:
                agent.translate_file(filepath, target_lang=target_lang)
            except FileNotFoundError as e:
                print(f"[❌ 错误] {e}")
            except Exception as e:
                print(f"[❌ 错误] 翻译失败: {e}")
        else:
            print("[⚠️ 提示] 请输入 .docx 或 .pptx 文件路径，或输入 /help 查看帮助")


if __name__ == "__main__":
    main()
