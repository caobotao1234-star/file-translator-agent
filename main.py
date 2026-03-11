from config.settings import Config
from core.llm_engine import ArkLLMEngine
from core.agent import BaseAgent
from core.agent_config import AgentConfig
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

    agent = BaseAgent(
        llm_engine=llm,
        tools=my_tools,
        config=AgentConfig(
            max_loops=8,
            debug=True,
            show_usage=True,
            enable_memory_summary=True,
        ),
    )

    print("--- ✅ Agent 启动完毕 (输入 exit 退出) ---")

    while True:
        user_input = input("\n[🧑 用户]: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ["exit", "quit"]:
            print("对话结束。")
            break

        print("[🤖 助手]: ", end="", flush=True)
        for event in agent.chat(user_input):
            render_event(event)
        print()


if __name__ == "__main__":
    main()