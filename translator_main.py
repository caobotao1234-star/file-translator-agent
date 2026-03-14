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


def print_help():
    print("""
╔═══════════════════════════════════════════════════════╗
║              📖 翻译 Agent 使用指南                   ║
╠═══════════════════════════════════════════════════════╣
║                                                       ║
║  翻译文档：                                           ║
║    直接输入文件路径即可开始翻译                       ║
║    支持: .docx (Word)  .pptx (PowerPoint)            ║
║    例: test.docx / slides.pptx                        ║
║    例: C:\\docs\\report.docx                           ║
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


def main():
    print("--- 🚀 正在初始化翻译 Agent ---")

    agent = TranslatorAgent(
        # draft_model_id 默认用 .env 里的模型
        # review_model_id 不指定时，审校也用同一个模型
        batch_size=20,
        debug=True,
    )

    print("--- ✅ 翻译 Agent 启动完毕 ---")

    # 显示 COM 增强模式状态
    if agent.com_enabled:
        print("--- 🖥️ COM 增强模式: ✅ 已开启（支持图表/文本框/SmartArt 翻译）---")
    else:
        print("--- 🖥️ COM 增强模式: ❌ 未开启（仅翻译段落和表格文字）---")
        print("---    提示: 安装 Microsoft Office 并安装 pywin32 可开启增强模式 ---")
    print_help()

    while True:
        user_input = input("\n[🧑 用户]: ").strip()
        if not user_input:
            continue

        if user_input.lower() in ["exit", "quit"]:
            print("再见！")
            break

        if user_input == "/help":
            print_help()
            continue

        if user_input == "/rules":
            agent.show_format_rules()
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
            # 样式名可能包含空格，用最后一个词作为字体名不太靠谱
            # 简单处理：用 | 分隔，或者让用户用引号
            # 这里先用简单逻辑：前面是样式名，最后两个词是字体名
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
            try:
                agent.translate_file(filepath)
            except FileNotFoundError as e:
                print(f"[❌ 错误] {e}")
            except Exception as e:
                print(f"[❌ 错误] 翻译失败: {e}")
        else:
            print("[⚠️ 提示] 请输入 .docx 或 .pptx 文件路径，或输入 /help 查看帮助")


if __name__ == "__main__":
    main()
