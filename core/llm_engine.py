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
            )
            
            # 【核心修改】：用字典通过 index 区分不同的并发工具调用
            tool_calls_dict = {}

            for chunk in stream:
                delta = chunk.choices[0].delta
                
                # 1. 正常文本输出
                if delta.content:
                    yield {"type": "text", "content": delta.content}
                    
                # 2. 工具调用输出
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index # 获取当前工具的并发编号
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