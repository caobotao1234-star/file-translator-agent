from config.settings import Config
from prompts.system_prompts import DEFAULT_ASSISTANT_PROMPT
from core.llm_engine import ArkLLMEngine
from core.memory import ConversationMemory
from core.agent import BaseAgent

def main():
    print("--- 🚀 正在初始化 Agent 系统 ---")
    
    # 1. 初始化底层组件
    llm = ArkLLMEngine(api_key=Config.ARK_API_KEY, model_id=Config.DEFAULT_MODEL_ID)
    memory = ConversationMemory(system_prompt=DEFAULT_ASSISTANT_PROMPT)
    
    # 2. 组装 Agent
    agent = BaseAgent(llm_engine=llm, memory=memory)

    print("--- ✅ Agent 启动完毕 (输入 exit 退出) ---")

    # 3. 进入主循环
    while True:
        user_input = input("\n[🧑 用户]: ").strip()
        if not user_input:
            continue
        if user_input.lower() in['exit', 'quit']:
            print("对话结束。")
            break

        print("[🤖 助手]: ", end="", flush=True)
        
        # 从 Agent 接收流式返回并打印
        for chunk in agent.chat(user_input):
            print(chunk, end="", flush=True)
            
        print() # 换行收尾
        
        # === 取消下面两行的注释，可随时调试查看内部记忆状态 ===
        # print("\n[🧠 内部记忆 Debug]:")
        # print(memory.get_debug_info())

if __name__ == "__main__":
    main()