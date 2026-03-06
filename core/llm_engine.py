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

    def stream_chat(self, messages: List[Dict], tools: List[Dict] = None) -> Generator[Dict, None, None]:
        try:
            stream = self.client.chat.completions.create(
                model=self.model_id,
                messages=messages,
                tools=tools,
                stream=True,
                # 【新增】：强制要求 API 在流的最后返回 Token 使用量
                stream_options={"include_usage": True} 
            )
            
            tool_calls_dict = {}

            for chunk in stream:
                # 【新增】：如果这一块包含了账单信息，我们就单独把它抛出去
                if chunk.usage:
                    yield {
                        "type": "usage", 
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens
                    }
                
                # 注意：带有 usage 的最后一块 chunk，通常不包含对话内容 choices，所以我们要跳过，防止报错
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                
                # 1. 正常文本输出
                if delta.content:
                    yield {"type": "text", "content": delta.content}
                    
                # 2. 工具调用输出
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_dict:
                            tool_calls_dict[idx] = {"name": "", "arguments": ""}
                        if tc.function.name:
                            tool_calls_dict[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_dict[idx]["arguments"] += tc.function.arguments

            # 3. 数据流接收完毕后，把收集到的多个工具全部抛出
            for idx, tc_data in tool_calls_dict.items():
                yield {"type": "tool_call", "name": tc_data["name"], "arguments": tc_data["arguments"]}
                
        except Exception as e:
            yield {"type": "text", "content": f"\n[LLM 请求错误]: {e}"}