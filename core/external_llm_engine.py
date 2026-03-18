# core/external_llm_engine.py
# =============================================================
# 📘 教学笔记：外部模型引擎（ExternalLLMEngine）
# =============================================================
# 为什么需要这个？
#   现有的 ArkLLMEngine 只能调用火山引擎（Volcengine）的模型。
#   但 Agent 大脑需要更强的模型（Gemini/Claude/GPT/NanoBanana）。
#
# 📘 关键设计：用 openai 包统一调用所有外部模型
#   Gemini、Claude、GPT、NanoBanana 都支持 OpenAI 兼容协议。
#   只需要不同的 base_url + api_key，就能用同一套代码调用。
#
# 📘 与 ArkLLMEngine 的关系：
#   stream_chat() 的输出格式完全一致（鸭子类型），
#   所以 LLMRouter 不关心引擎类型，get() 返回的引擎都能用。
# =============================================================

import os
import time
from typing import List, Dict, Generator, Optional
from openai import OpenAI
from core.logger import get_logger
from core.llm_engine import LLMRetryError

logger = get_logger("external_llm_engine")

# 📘 教学笔记：Provider 配置映射
# 每个 provider 有自己的 API 地址和环境变量名。
# 新增 provider 只需要在这里加一行，不用改其他代码。
PROVIDER_CONFIG = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GEMINI_API_KEY",
    },
    "claude": {
        "base_url": "https://api.anthropic.com/v1/",
        "env_key": "CLAUDE_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1/",
        "env_key": "OPENAI_API_KEY",
    },
    "nanobanana": {
        "base_url": "https://api.nanobanana.com/v1/",
        "env_key": "NANOBANANA_API_KEY",
    },
}


class ExternalLLMEngine:
    """
    📘 教学笔记：基于 openai 包的外部模型引擎

    与 ArkLLMEngine 保持完全相同的 stream_chat 输出格式：
    - {"type": "text", "content": "..."}
    - {"type": "tool_call", "id": "...", "name": "...", "arguments": "..."}
    - {"type": "usage", "prompt_tokens": N, "completion_tokens": N, "total_tokens": N}

    这样 LLMRouter 和 ScanAgent 不需要关心底层是火山引擎还是外部模型。
    """

    def __init__(
        self,
        api_key: str,
        model_id: str,
        base_url: str,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ):
        if not api_key:
            raise ValueError(
                f"外部模型 API 密钥未配置。请在 .env 中设置对应的 API Key。"
            )
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model_id = model_id
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.logger = get_logger("external_llm_engine")

    def _is_retryable(self, error: Exception) -> bool:
        """
        📘 复用 ArkLLMEngine 相同的重试判断逻辑
        网络错误 / 429 限流 / 5xx 服务端故障 → 重试
        参数错误 400 → 不重试
        """
        error_str = str(error).lower()
        network_keywords = ["timeout", "connection", "network", "reset", "broken pipe"]
        if any(kw in error_str for kw in network_keywords):
            return True
        if hasattr(error, "status_code"):
            return error.status_code in (429, 500, 502, 503, 504)
        retryable_codes = ["429", "500", "502", "503", "504"]
        if any(code in error_str for code in retryable_codes):
            return True
        return False

    def stream_chat(
        self, messages: List[Dict], tools: List[Dict] = None
    ) -> Generator[Dict, None, None]:
        """
        📘 带重试机制的流式对话（与 ArkLLMEngine.stream_chat 完全相同的接口）

        流程和 ArkLLMEngine 一模一样：
        1. 尝试调用 API
        2. 成功 → yield 数据
        3. 失败 → 判断是否可重试 → 指数退避
        4. 所有重试失败 → 抛出 LLMRetryError
        """
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    delay = self.retry_base_delay * (2 ** (attempt - 1))
                    yield {
                        "type": "text",
                        "content": f"\n[⏳ 第{attempt}次重试，等待{delay:.1f}秒...]\n",
                    }
                    time.sleep(delay)
                yield from self._do_stream_chat(messages, tools)
                return
            except LLMRetryError:
                raise
            except Exception as e:
                last_error = e
                if not self._is_retryable(e):
                    raise LLMRetryError(
                        f"外部模型调用失败（不可重试）: {e}", last_error=e
                    )
        raise LLMRetryError(
            f"外部模型调用在 {self.max_retries} 次重试后仍然失败: {last_error}",
            last_error=last_error,
        )

    def _do_stream_chat(
        self, messages: List[Dict], tools: List[Dict] = None
    ) -> Generator[Dict, None, None]:
        """
        📘 实际执行一次流式 API 调用

        openai 包的 stream 接口和火山引擎几乎一样（因为都是 OpenAI 协议），
        所以解析逻辑也几乎一样。
        """
        import json as _json

        self.logger.trace(
            f"外部模型请求 [model={self.model_id}]\n"
            f"messages={_json.dumps(messages, ensure_ascii=False, indent=2)}"
        )

        kwargs = {
            "model": self.model_id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            self.logger.trace(
                f"tools={_json.dumps(tools, ensure_ascii=False, indent=2)}"
            )

        stream = self.client.chat.completions.create(**kwargs)

        tool_calls_dict = {}
        full_text = ""

        for chunk in stream:
            # 📘 usage 信息（token 消耗统计）
            if chunk.usage:
                yield {
                    "type": "usage",
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # 📘 文本输出
            if delta.content:
                full_text += delta.content
                yield {"type": "text", "content": delta.content}

            # 📘 工具调用输出
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls_dict[idx]["id"] += tc.id
                    if tc.function and tc.function.name:
                        tool_calls_dict[idx]["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls_dict[idx]["arguments"] += tc.function.arguments

        if full_text:
            self.logger.trace(f"外部模型响应 [text]\n{full_text}")
        if tool_calls_dict:
            self.logger.trace(
                f"外部模型响应 [tool_calls]\n"
                f"{_json.dumps(dict(tool_calls_dict), ensure_ascii=False, indent=2)}"
            )

        # 📘 流结束后，yield 收集到的工具调用
        for idx, tc_data in tool_calls_dict.items():
            yield {
                "type": "tool_call",
                "id": tc_data["id"],
                "name": tc_data["name"],
                "arguments": tc_data["arguments"],
            }


def create_external_engine(
    provider: str,
    model_id: str,
    api_key: str = None,
    max_retries: int = 3,
    retry_base_delay: float = 1.0,
) -> ExternalLLMEngine:
    """
    📘 工厂函数：根据 provider 名称创建 ExternalLLMEngine

    用法：
        engine = create_external_engine("gemini", "gemini-2.5-pro")
        engine = create_external_engine("nanobanana", "nanobanana-pro", api_key="xxx")
    """
    provider = provider.lower().strip()
    if provider not in PROVIDER_CONFIG:
        raise ValueError(
            f"不支持的 provider: '{provider}'。"
            f"支持的 provider: {list(PROVIDER_CONFIG.keys())}"
        )

    config = PROVIDER_CONFIG[provider]
    if not api_key:
        api_key = os.getenv(config["env_key"], "").strip()
    if not api_key:
        raise ValueError(
            f"外部模型 API 密钥未配置。"
            f"请在 .env 中设置 {config['env_key']} 或传入 api_key 参数。"
        )

    return ExternalLLMEngine(
        api_key=api_key,
        model_id=model_id,
        base_url=config["base_url"],
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
    )
