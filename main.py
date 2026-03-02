import os
from volcenginesdkarkruntime import Ark

# 从环境变量中获取您的API KEY，配置方法见：https://www.volcengine.com/docs/82379/1399008
# api_key = os.getenv('ARK_API_KEY')
api_key = "e2537c57-6d35-4b3b-a718-a57b8a15dd21"

client = Ark(
    base_url='https://ark.cn-beijing.volces.com/api/v3',
    api_key=api_key,
)

# tools = [{
#     "type": "web_search",
#     "max_keyword": 2,
# }]

# 创建一个对话请求
response = client.responses.create(
    model="ep-20260225180145-r528v",
    input=[{"role": "user", "content": "我叫什么名字？"}],
    # tools=tools,
)

print(response)