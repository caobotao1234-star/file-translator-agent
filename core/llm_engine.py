# core/llm_engine.py
import time
from volcenginesdkarkruntime import Ark
from typing import List, Dict, Generator
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：重试机制（Retry with Exponential Backoff）
# =============================================================
# 为什么需要重试？
#   - LLM API 是远程服务，网络不稳定、服务端限流（429）、临时故障（500）都很常见
#   - 一个生产级 Agent 不能因为一次偶发错误就直接崩溃
#
# 什么是指数退避（Exponential Backoff）？
#   - 第1次失败 → 等1秒再试
#   - 第2次失败 → 等2秒再试
#   - 第3次失败 → 等4秒再试
#   - 每次等待时间翻倍，避免短时间内疯狂重试把服务端打爆
#
# 哪些错误值得重试？
#   - 网络超时（ConnectionError, Timeout）→ 值得重试，可能是临时网络抖动
#   - 服务端限流（HTTP 429）→ 值得重试，等一会儿配额就恢复了
#   - 服务端内部错误（HTTP 500/502/503）→ 值得重试，可能是临时故障
#   - 参数错误（HTTP 400）→ 不值得重试，重试100次也是一样的错
# =============================================================


class LLMRetryError(Exception):
    """当所有重试都失败后抛出的自定义异常"""
    def __init__(self, message: str, last_error: Exception = None):
        super().__init__(message)
        self.last_error = last_error


class ArkLLMEngine:
    def __init__(
        self,
        api_key: str,
        model_id: str,
        max_retries: int = 3,          # 最大重试次数
        retry_base_delay: float = 1.0,  # 初始等待秒数
    ):
        self.client = Ark(
            base_url='https://ark.cn-beijing.volces.com/api/v3',
            api_key=api_key,
        )
        self.model_id = model_id
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.logger = get_logger("llm_engine")

    def _is_retryable(self, error: Exception) -> bool:
        """
        判断一个错误是否值得重试。
        
        思路：
        - 网络层面的错误（连接超时等）→ 重试
        - HTTP 429（限流）/ 5xx（服务端故障）→ 重试
        - 其他错误（如参数错误 400）→ 不重试，直接报错
        """
        error_str = str(error).lower()

        # 网络类错误关键词
        network_keywords = ["timeout", "connection", "network", "reset", "broken pipe"]
        if any(kw in error_str for kw in network_keywords):
            return True

        # HTTP 状态码判断：从异常对象或错误信息中提取
        if hasattr(error, "status_code"):
            return error.status_code in (429, 500, 502, 503, 504)
        
        # 兜底：从错误文本中匹配状态码
        retryable_codes = ["429", "500", "502", "503", "504"]
        if any(code in error_str for code in retryable_codes):
            return True

        return False

    def stream_chat(self, messages: List[Dict], tools: List[Dict] = None) -> Generator[Dict, None, None]:
        """
        带重试机制的流式对话。
        
        流程：
        1. 尝试调用 API
        2. 如果成功 → 正常 yield 数据
        3. 如果失败 → 判断是否可重试
           - 可重试 → 等待后重试（指数退避）
           - 不可重试 → 直接抛出错误
        4. 所有重试都失败 → 抛出 LLMRetryError
        """
        last_error = None

        for attempt in range(self.max_retries + 1):  # 0 = 首次调用，1~N = 重试
            try:
                # 如果是重试，先等待
                if attempt > 0:
                    delay = self.retry_base_delay * (2 ** (attempt - 1))  # 1s, 2s, 4s...
                    yield {
                        "type": "text",
                        "content": f"\n[⏳ 第{attempt}次重试，等待{delay:.1f}秒...]\n"
                    }
                    time.sleep(delay)

                # 调用 API 并逐块 yield 结果
                yield from self._do_stream_chat(messages, tools)
                return  # 成功了，直接返回，不再重试

            except LLMRetryError:
                # _do_stream_chat 内部已经判断过不可重试，直接往上抛
                raise
            except Exception as e:
                last_error = e
                if not self._is_retryable(e):
                    # 不可重试的错误（如参数错误），直接报错
                    raise LLMRetryError(
                        f"LLM 调用失败（不可重试）: {e}", last_error=e
                    )
                # 可重试的错误，继续循环

        # 所有重试都用完了
        raise LLMRetryError(
            f"LLM 调用在 {self.max_retries} 次重试后仍然失败: {last_error}",
            last_error=last_error,
        )

    def _do_stream_chat(self, messages: List[Dict], tools: List[Dict] = None) -> Generator[Dict, None, None]:
        """实际执行一次流式 API 调用（不含重试逻辑）"""
        # 📘 教学笔记：TRACE 级别 — 完整 LLM 请求
        # 这里输出发给模型的完整 messages，包括 system prompt 和用户输入。
        # 排查"模型不听话"问题时，第一步就是看它到底收到了什么。
        import json as _json
        from core.external_llm_engine import _sanitize_for_log
        self.logger.trace(
            f"LLM 请求 [model={self.model_id}]\n"
            f"messages={_json.dumps(_sanitize_for_log(messages), ensure_ascii=False, indent=2)}"
        )
        if tools:
            self.logger.trace(f"tools={_json.dumps(tools, ensure_ascii=False, indent=2)}")

        stream = self.client.chat.completions.create(
            model=self.model_id,
            messages=messages,
            tools=tools,
            stream=True,
            stream_options={"include_usage": True}
        )

        tool_calls_dict = {}
        full_text = ""  # 收集完整响应文本，用于 TRACE 输出

        for chunk in stream:
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

            # 1. 正常文本输出
            if delta.content:
                full_text += delta.content
                yield {"type": "text", "content": delta.content}

            # 2. 工具调用输出
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls_dict[idx]["id"] += tc.id
                    if tc.function.name:
                        tool_calls_dict[idx]["name"] += tc.function.name
                    if tc.function.arguments:
                        tool_calls_dict[idx]["arguments"] += tc.function.arguments

        # 📘 教学笔记：TRACE 级别 — 完整 LLM 响应
        # 输出模型返回的完整文本，一字不漏。
        # 排查翻译质量问题时，看这个就知道模型到底输出了什么。
        if full_text:
            self.logger.trace(f"LLM 响应 [text]\n{full_text}")
        if tool_calls_dict:
            self.logger.trace(f"LLM 响应 [tool_calls]\n{_json.dumps(dict(tool_calls_dict), ensure_ascii=False, indent=2)}")

        # 3. 流结束后，抛出收集到的工具调用
        for idx, tc_data in tool_calls_dict.items():
            yield {
                "type": "tool_call",
                "id": tc_data["id"],
                "name": tc_data["name"],
                "arguments": tc_data["arguments"],
            }