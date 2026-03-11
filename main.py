from config.settings import Config
from core.llm_engine import ArkLLMEngine
from core.agent import BaseAgent
from core.agent_config import AgentConfig
from core.storage import ChatStorage
from tools.basic_tools import TimeTool, WeatherTool, CalculatorTool, NewsTool, WebSearchTool


def render_event(event):
    if event.type == "debug":
        print("\n" + "▼" * 20 + "[Debug Context]" + "▼" * 20)
        print(event.data["messages"])
        print("▲" * 60)

    elif event.type == "text_delta":
        print(event.data["content"], end="", flush=True)

    elif event.type == "tool_call":
        print(
            f"\n\n[Tool Call] {event.data['name']} args={event.data['arguments']}",
            flush=True
        )

    elif event.type == "tool_result":
        print(f"[Tool Result] {event.data['result']}", flush=True)

    elif event.type == "status":
        print(f"\n[Status] {event.data['message']}", flush=True)

    elif event.type == "warning":
        print(f"\n[Warning] {event.data['message']}", flush=True)

    elif event.type == "error":
        print(f"\n[❌ Error] {event.data['message']}", flush=True)

    elif event.type == "usage":
        print(
            f"\n[Usage] turn={event.data['turn_tokens']} "
            f"(prompt={event.data['turn_prompt_tokens']}, completion={event.data['turn_completion_tokens']}) "
            f"| total={event.data['total_tokens']}",
            flush=True
        )


# =============================================================
# 📘 教学笔记：会话管理命令
# =============================================================
# 我们给终端交互加了几个特殊命令：
#   /new        — 开始一个全新的会话
#   /list       — 查看所有历史会话
#   /load <id>  — 加载一个历史会话，断点续聊
#   /delete <id>— 删除一个历史会话
#   exit        — 退出程序
#
# 这些命令以 "/" 开头，和正常对话区分开，不会发给 LLM。
# =============================================================


def create_agent(llm, tools, config, session_id=None):
    """工厂函数：创建一个 Agent 实例"""
    return BaseAgent(
        llm_engine=llm,
        tools=tools,
        config=config,
        session_id=session_id,
    )


def print_help():
    print("""
╔══════════════════════════════════════╗
║         📋 会话管理命令              ║
╠══════════════════════════════════════╣
║  /new         开始新会话             ║
║  /list        查看历史会话列表       ║
║  /load <id>   加载历史会话           ║
║  /delete <id> 删除历史会话           ║
║  /help        显示此帮助             ║
║  exit         退出程序               ║
╚══════════════════════════════════════╝
""")


def main():
    print("--- 🚀 正在初始化 Agent 系统 ---")

    llm = ArkLLMEngine(
        api_key=Config.ARK_API_KEY,
        model_id=Config.DEFAULT_MODEL_ID
    )

    my_tools = [
        TimeTool(),
        WeatherTool(),
        CalculatorTool(),
        NewsTool(),
        WebSearchTool(),
    ]

    config = AgentConfig(
        max_loops=8,
        debug=True,
        show_usage=True,
        enable_memory_summary=True,
        enable_persistence=True,  # 📘 开启持久化
    )

    agent = create_agent(llm, my_tools, config)
    print(f"--- ✅ Agent 启动完毕 | 会话ID: {agent.session_id} ---")
    print("--- 输入 /help 查看会话管理命令，输入 exit 退出 ---")

    while True:
        user_input = input("\n[🧑 用户]: ").strip()
        if not user_input:
            continue

        if user_input.lower() in ["exit", "quit"]:
            print("对话结束。")
            break

        # ---- 会话管理命令 ----
        if user_input == "/help":
            print_help()
            continue

        if user_input == "/new":
            agent = create_agent(llm, my_tools, config)
            print(f"[📂 会话管理]: 已创建新会话 | ID: {agent.session_id}")
            continue

        if user_input == "/list":
            storage = ChatStorage(storage_dir=config.storage_dir)
            sessions = storage.list_sessions()
            if not sessions:
                print("[📂 会话管理]: 暂无历史会话")
            else:
                print(f"[📂 会话管理]: 共 {len(sessions)} 个历史会话：")
                for s in sessions:
                    marker = " ← 当前" if s["session_id"] == agent.session_id else ""
                    print(f"  📝 {s['session_id']}  |  更新于 {s['updated_at']}  |  {s['message_count']} 条消息{marker}")
            continue

        if user_input.startswith("/load "):
            target_id = user_input[6:].strip()
            agent = create_agent(llm, my_tools, config, session_id=target_id)
            if agent.memory.messages:
                print(f"[📂 会话管理]: 已加载会话 {target_id}（{len(agent.memory.messages)} 条消息）")
            else:
                print(f"[📂 会话管理]: 会话 {target_id} 不存在或为空，已作为新会话创建")
            continue

        if user_input.startswith("/delete "):
            target_id = user_input[8:].strip()
            storage = ChatStorage(storage_dir=config.storage_dir)
            if storage.delete(target_id):
                print(f"[📂 会话管理]: 已删除会话 {target_id}")
                if target_id == agent.session_id:
                    agent = create_agent(llm, my_tools, config)
                    print(f"[📂 会话管理]: 当前会话已被删除，自动创建新会话 | ID: {agent.session_id}")
            else:
                print(f"[📂 会话管理]: 会话 {target_id} 不存在")
            continue

        if user_input.startswith("/"):
            print("[⚠️ 提示]: 未知命令，输入 /help 查看可用命令")
            continue

        # ---- 正常对话 ----
        print("[🤖 助手]: ", end="", flush=True)
        for event in agent.chat(user_input):
            render_event(event)
        print()


if __name__ == "__main__":
    main()
