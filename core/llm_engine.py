# core/llm_engine.py
from volcenginesdkarkruntime import Ark
from typing import List, Dict, Generator

class ArkLLMEngine:
    def __init__(self, api_key: str, model_id: str):
        self.client = Ark(
            base_url='https://ark.cn-beijing.volces.com/api/v3',
            api_key=api_key,
        )
        self.model_id = model_id

    # 注意这里的入参多了一个 tools
    def stream_chat(self, messages: List[Dict], tools: List[Dict] = None) -> Generator[Dict, None, None]:
        try:
            stream = self.client.chat.completions.create(
                model=self.model_id,
                messages=messages,
                tools=tools, # 真正的核心：把工具列表发给火山 API！
                stream=True,
            )
            
            tool_call_name = ""
            tool_call_args = ""
            is_tool_call = False

            for chunk in stream:
                delta = chunk.choices[0].delta
                
                # 1. 如果模型正常说话（文本）
                if delta.content:
                    yield {"type": "text", "content": delta.content}
                    
                # 2. 如果模型决定调用工具（流式模式下，API 返回的 JSON 字符串是被切碎的，需要我们拼起来）
                elif delta.tool_calls:
                    is_tool_call = True
                    tc = delta.tool_calls[0]
                    if tc.function.name:
                        tool_call_name = tc.function.name
                    if tc.function.arguments:
                        tool_call_args += tc.function.arguments

            # 3. 循环结束后，如果是工具调用，就把拼接好的结果抛出
            if is_tool_call:
                yield {"type": "tool_call", "name": tool_call_name, "arguments": tool_call_args}
                
        except Exception as e:
            yield {"type": "text", "content": f"\n[LLM 请求错误]: {e}"}