import os
import json
from volcenginesdkarkruntime import Ark

# 设置 API Key
api_key = "e2537c57-6d35-4b3b-a718-a57b8a15dd21"

client = Ark(
    base_url='https://ark.cn-beijing.volces.com/api/v3',
    api_key=api_key,
)

print("--- 已进入对话模式（支持上下文，输入 'exit' 或 'quit' 退出） ---")

while True:
    # 1. 获取终端输入
    user_input = input("\n用户: ")
    
    # 退出条件
    if user_input.lower() in ['exit', 'quit']:
        print("对话结束。")
        break

    # --- 核心修改：将用户输入添加到历史记录 ---
    history.append({"role": "user", "content": user_input})

    # --- 【新增：监控区】打印发送给模型的所有数据 ---
    print("\n" + "="*30 + " 发送给模型的原始数据 " + "="*30)
    # 使用 json.dumps 让输出带缩进和颜色（如果终端支持），ensure_ascii=False 保证中文正常显示
    print(json.dumps(history, indent=2, ensure_ascii=False))
    print("=" * 80 + "\n")
    # ----------------------------------------------

    # 2. 创建流式对话请求
    # 注意：这里 messages 直接传我们的 history 列表
    stream = client.chat.completions.create(
        model="ep-20260225180145-r528v",
        messages=history, 
        stream=True,
    )

    # 3. 处理流式返回的内容
    print("助手: ", end="")
    
    # 用于记录 AI 本次回答的完整内容，以便存入历史
    full_assistant_response = ""
    
    for chunk in stream:
        if not chunk.choices:
            continue
        
        content = chunk.choices[0].delta.content
        if content:
            print(content, end="", flush=True)
            full_assistant_response += content # 累加每一块文字
    
    print() # 换行

    # --- 核心修改：将 AI 的完整回答也添加到历史记录中 ---
    history.append({"role": "assistant", "content": full_assistant_response})

    # (可选) 进阶操作：如果对话非常长，建议限制历史记录长度，防止 Token 消耗过快
    # if len(history) > 11:  # 只保留最近 5 轮对话 (1个system + 5个user + 5个assistant)
    #     history = [history[0]] + history[-10:]