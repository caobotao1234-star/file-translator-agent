from config.settings import Config
from core.llm_engine import ArkLLMEngine
from core.agent import BaseAgent

# 导入我们的工具对象
from tools.basic_tools import TimeTool, WeatherTool, CalculatorTool

def main():
    print("--- 🚀 正在初始化 Agent 系统 ---")
    
    # 1. 初始化底层组件
    llm = ArkLLMEngine(api_key=Config.ARK_API_KEY, model_id=Config.DEFAULT_MODEL_ID)
    
    # 2. 准备工具箱 (就像给机器猫塞道具一样，以后加新工具只需在这里 append)
    my_tools =[
        TimeTool(),
        WeatherTool(),
        CalculatorTool()
    ]
    
    # 3. 组装 Agent (注意：这里不再传入 memory，Agent 内部会根据工具自动生成)
    agent = BaseAgent(llm_engine=llm, tools=my_tools)

    print("--- ✅ Agent 启动完毕 (输入 exit 退出) ---")

    # 4. 进入主循环
    while True:
        user_input = input("\n[🧑 用户]: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ['exit', 'quit']:
            print("对话结束。")
            break

        print("[🤖 助手]: ", end="", flush=True)
        
        # 从 Agent 接收流式返回并打印
        for chunk in agent.chat(user_input):
            print(chunk, end="", flush=True)
            
        print() # 换行收尾

if __name__ == "__main__":
    main()