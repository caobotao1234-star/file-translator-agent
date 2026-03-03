from volcenginesdkarkruntime import Ark
from typing import List, Dict, Generator

class ArkLLMEngine:
    """封装火山方舟 API，未来若新增 OpenAIEngine，只需实现同名方法即可"""
    
    def __init__(self, api_key: str, model_id: str):
        self.client = Ark(
            base_url='https://ark.cn-beijing.volces.com/api/v3',
            api_key=api_key,
        )
        self.model_id = model_id

    def stream_chat(self, messages: List[Dict]) -> Generator[str, None, None]:
        """发起请求并产生流式文本"""
        try:
            stream = self.client.chat.completions.create(
                model=self.model_id,
                messages=messages,
                stream=True,
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            yield f"\n[LLM 请求错误]: {e}"