import os
from volcenginesdkarkruntime import Ark

# 设置 API Key
api_key = "e2537c57-6d35-4b3b-a718-a57b8a15dd21"

client = Ark(
    base_url='https://ark.cn-beijing.volces.com/api/v3',
    api_key=api_key,
)

print("--- 已进入对话模式（输入 'exit' 或 'quit' 退出） ---")

while True:
    # 1. 获取终端输入
    user_input = input("\n用户: ")
    
    # 退出条件
    if user_input.lower() in ['exit', 'quit']:
        print("对话结束。")
        break

    # 2. 创建流式对话请求
    # 注意：这里改用了 chat.completions.create，这是最稳妥的流式调用方式
    stream = client.chat.completions.create(
        model="ep-20260225180145-r528v",
        messages=[
            {"role": "user", "content": user_input},
        ],
        stream=True, # 开启流式输出
    )

    # 3. 处理流式返回的内容
    print("助手: ", end="")
    for chunk in stream:
        # 获取每一块内容并立即打印
        if not chunk.choices:
            continue
        
        content = chunk.choices[0].delta.content
        if content:
            print(content, end="", flush=True) # flush=True 确保文字立即显示在屏幕上
    
    print() # 换行