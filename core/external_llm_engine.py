# core/external_llm_engine.py
# =============================================================
# 📘 教学笔记：统一 LLM 引擎（Unified LLM Engine）
# =============================================================
# v6 统一架构：所有模型都走这一个引擎。
#
# 📘 核心理念：模型不分内部外部，能力等价
#   火山引擎（doubao）、Gemini、Claude、GPT、NanoBanana
#   都是 OpenAI 兼容协议，只是 base_url 和 api_key 不同。
#   用同一套代码调用，所有模型都支持：
#   - 流式输出（streaming）
#   - 工具调用（tool_call）
#   - 多模态输入（vision / image_url）
#   - extra_content 透传（Gemini thought_signature）
#
# 📘 与旧架构的区别：
#   旧: ArkLLMEngine（火山引擎专用）+ ExternalLLMEngine（外部模型）
#   新: ExternalLLMEngine 统一处理所有 provider（包括 ark）
#   ArkLLMEngine 保留但不再使用，仅作为向后兼容的类型引用。
# =============================================================

import os
import time
from typing import List, Dict, Generator, Optional

import httpx
from openai import OpenAI, APITimeoutError, APIConnectionError
from core.logger import get_logger
from core.llm_engine import LLMRetryError

logger = get_logger("external_llm_engine")


def _sanitize_for_log(messages):
    """
    📘 清理 messages 中的 base64 图片数据，避免污染日志。
    图片内容替换为 "[image base64, {size}KB]" 占位符。
    """
    cleaned = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            # 📘 多模态消息：content 是 [{type: "text"}, {type: "image_url"}] 数组
            new_parts = []
            for part in content:
                if (isinstance(part, dict)
                        and part.get("type") == "image_url"
                        and isinstance(part.get("image_url"), dict)):
                    url = part["image_url"].get("url", "")
                    if url.startswith("data:") and ";base64," in url:
                        b64_data = url.split(";base64,", 1)[1]
                        size_kb = round(len(b64_data) * 3 / 4 / 1024, 1)
                        new_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"[image base64, {size_kb}KB]"},
                        })
                    else:
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            cleaned.append({**msg, "content": new_parts})
        else:
            cleaned.append(msg)
    return cleaned


# 📘 教学笔记：Provider 配置映射
# 每个 provider 有自己的 API 地址和环境变量名。
# 新增 provider 只需要在这里加一行，不用改其他代码。
#
# 📘 v6 统一架构：火山引擎（ark）也是一个 provider
# 之前 ark 用独立的 ArkLLMEngine（volcenginesdkarkruntime），
# 但火山引擎 API 也是 OpenAI 兼容协议，完全可以用 openai SDK 调用。
# 统一后所有模型都走 ExternalLLMEngine，能力完全等价：
# vision、tool_call、extra_content 全部支持。
PROVIDER_CONFIG = {
    "ark": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "env_key": "ARK_API_KEY",
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "DASHSCOPE_API_KEY",
    },
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
    📘 教学笔记：统一 LLM 引擎（v6）

    所有模型（火山引擎 + Gemini + Claude + GPT + NanoBanana）都走这个引擎。
    基于 openai SDK，通过不同的 base_url 区分 provider。

    输出格式：
    - {"type": "text", "content": "..."}
    - {"type": "tool_call", "id": "...", "name": "...", "arguments": "...", "extra_content": ...}
    - {"type": "usage", "prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
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

        # 📘 教学笔记：代理（Proxy）支持
        # 代理已在 config/settings.py 中全局设置（HTTP_PROXY / HTTPS_PROXY 环境变量），
        # httpx 和 openai SDK 会自动读取这些环境变量。
        # 这里只需要处理超时和重试配置。
        #
        # 📘 但有一个坑：openai SDK 内置重试会自己创建新的 httpx.Client，
        # 那个新 Client 也会读环境变量，所以全局代理方案下 SDK 重试也能走代理了。
        # 不过我们仍然禁用 SDK 重试，用自己的重试逻辑（更可控、有日志）。

        # 📘 超时配置：扫描件图片是大尺寸 base64，需要更长超时
        timeout_seconds = float(os.getenv("EXTERNAL_API_TIMEOUT", "180"))

        proxy_url = os.getenv("HTTPS_PROXY", "") or os.getenv("HTTP_PROXY", "")
        if proxy_url:
            logger.info(f"外部模型使用全局代理: {proxy_url}")

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,  # 📘 禁用 SDK 内置重试，用我们自己的
        )
        self.model_id = model_id
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.logger = get_logger("external_llm_engine")

    def _is_retryable(self, error: Exception) -> bool:
        """
        📘 教学笔记：重试判断逻辑（改进版）

        之前的问题：openai SDK 把超时包装成 APITimeoutError，
        str(error) 可能不包含 "timeout" 关键字，导致误判为"不可重试"。

        改进：先按异常类型判断（最可靠），再按字符串兜底。
        - APITimeoutError → 一定重试（网络慢/图片大）
        - APIConnectionError → 一定重试（代理/网络问题）
        - 429 / 5xx → 重试（限流/服务端故障）
        - 400 参数错误 → 不重试
        """
        # 📘 第一优先级：按异常类型判断（最可靠）
        if isinstance(error, (APITimeoutError, APIConnectionError)):
            return True
        # 📘 httpx 自己的超时和连接异常
        if isinstance(error, (httpx.TimeoutException, httpx.ConnectError)):
            return True

        # 📘 第二优先级：按 HTTP 状态码判断
        if hasattr(error, "status_code"):
            return error.status_code in (429, 500, 502, 503, 504)

        # 📘 第三优先级：字符串兜底（以防有其他包装层）
        error_str = str(error).lower()
        network_keywords = ["timeout", "connection", "network", "reset", "broken pipe"]
        if any(kw in error_str for kw in network_keywords):
            return True
        retryable_codes = ["429", "500", "502", "503", "504"]
        if any(code in error_str for code in retryable_codes):
            return True
        return False

    def stream_chat(
        self, messages: List[Dict], tools: List[Dict] = None,
        max_tokens: int = None,
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
                yield from self._do_stream_chat(messages, tools, max_tokens=max_tokens)
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
    def generate_image(
        self, messages: List[Dict], max_tokens: int = 8192,
    ) -> Dict:
        """
        📘 教学笔记：Gemini 原生 API 图片生成

        📘 为什么不用 OpenAI 兼容接口？
        Gemini 的 OpenAI 兼容层 (/v1beta/openai/chat/completions) 不支持图片输出，
        调用图片生成模型时会报 400: "Unhandled generated data mime type: image/jpeg"。
        必须用 Gemini 原生的 generateContent API，并设置
        responseModalities: ["TEXT", "IMAGE"] 才能拿到图片。

        📘 原生 API vs OpenAI 兼容层：
        - 认证方式不同：原生用 ?key=xxx 查询参数，兼容层用 Bearer token
        - 消息格式不同：原生用 contents[].parts[]，兼容层用 messages[].content
        - 图片输入：原生用 inlineData {mimeType, data}，兼容层用 image_url
        - 图片输出：原生在 parts[].inlineData 中返回，兼容层不支持

        📘 返回格式（与旧版一致，ImageGenTool 无需修改）：
        {
            "text": "文本响应（如果有）",
            "images": [bytes, ...],  # 解码后的图片 bytes 列表
            "usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N},
        }
        """
        import json as _json
        import base64 as b64

        self.logger.trace(
            f"图片生成请求 [model={self.model_id}] (Gemini native API)\n"
            f"messages={_json.dumps(_sanitize_for_log(messages), ensure_ascii=False, indent=2)}"
        )

        # ── 📘 Step 1: 把 OpenAI 格式的 messages 转换为 Gemini 原生格式 ──
        # OpenAI: [{"role": "user", "content": [{"type": "text"}, {"type": "image_url"}]}]
        # Gemini: {"contents": [{"role": "user", "parts": [{"text": "..."}, {"inlineData": {...}}]}]}
        gemini_contents = []
        for msg in messages:
            role = msg.get("role", "user")
            # 📘 Gemini 原生 API 只认 "user" 和 "model"（不认 "assistant"/"system"）
            gemini_role = "model" if role == "assistant" else "user"
            parts = []
            content = msg.get("content", "")

            if isinstance(content, str):
                # 📘 纯文本消息
                if content:
                    parts.append({"text": content})
            elif isinstance(content, list):
                # 📘 多模态消息：遍历 parts 转换格式
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text_val = part.get("text", "")
                        if text_val:
                            parts.append({"text": text_val})
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:") and ";base64," in url:
                            # 📘 data URL → inlineData
                            header, b64_data = url.split(";base64,", 1)
                            mime_type = header.replace("data:", "")
                            parts.append({
                                "inlineData": {
                                    "mimeType": mime_type,
                                    "data": b64_data,
                                }
                            })

            if parts:
                gemini_contents.append({"role": gemini_role, "parts": parts})

        # ── 📘 Step 2: 构建 Gemini 原生 API 请求体 ──
        # 📘 关键：responseModalities 必须包含 "IMAGE"，否则模型不会返回图片
        request_body = {
            "contents": gemini_contents,
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "maxOutputTokens": max_tokens,
            },
        }

        # ── 📘 Step 3: 构建请求 URL 和代理 ──
        # 📘 Gemini 原生 API 地址格式：
        # https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
        api_base = "https://generativelanguage.googleapis.com/v1beta"
        url = f"{api_base}/models/{self.model_id}:generateContent"

        # 📘 从 self.client 提取 API key（ExternalLLMEngine.__init__ 中传入的）
        api_key = self.client.api_key

        # 📘 代理配置：读取环境变量（与 __init__ 中的逻辑一致）
        proxy_url = os.getenv("HTTPS_PROXY", "") or os.getenv("HTTP_PROXY", "")
        # 📘 教学笔记：图片生成需要更长超时
        # 普通文本 API 180 秒够用，但图片生成要：
        # 1. 上传大尺寸原图 base64（几百 KB ~ 几 MB）
        # 2. 等模型生成图片（比文本慢很多）
        # 3. 下载生成的图片 base64
        # 通过代理时更慢。默认 300 秒（5分钟），可通过环境变量覆盖。
        timeout_seconds = float(os.getenv("IMAGE_GEN_TIMEOUT",
                                          os.getenv("EXTERNAL_API_TIMEOUT", "300")))

        # ── 📘 Step 4: 发送请求（带重试） ──
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    delay = self.retry_base_delay * (2 ** (attempt - 1))
                    self.logger.info(f"图片生成第 {attempt} 次重试，等待 {delay:.1f}s...")
                    time.sleep(delay)

                # 📘 httpx 直接调用，支持代理
                client_kwargs = {"timeout": timeout_seconds}
                if proxy_url:
                    client_kwargs["proxy"] = proxy_url

                with httpx.Client(**client_kwargs) as http_client:
                    resp = http_client.post(
                        url,
                        params={"key": api_key},
                        json=request_body,
                        headers={"Content-Type": "application/json"},
                    )

                # 📘 检查 HTTP 错误
                if resp.status_code != 200:
                    error_detail = resp.text[:500]
                    self.logger.warning(
                        f"Gemini 原生 API 返回 {resp.status_code}: {error_detail}"
                    )
                    raise Exception(
                        f"Gemini API error {resp.status_code}: {error_detail}"
                    )

                data = resp.json()

                # ── 📘 Step 5: 解析响应 ──
                result = {"text": "", "images": [], "usage": None}

                # 📘 提取 usage（Gemini 原生格式：usageMetadata）
                usage_meta = data.get("usageMetadata", {})
                if usage_meta:
                    result["usage"] = {
                        "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                        "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                        "total_tokens": usage_meta.get("totalTokenCount", 0),
                    }
                    self.logger.debug(
                        f"图片生成 usage: prompt={result['usage']['prompt_tokens']}, "
                        f"completion={result['usage']['completion_tokens']}"
                    )

                # 📘 提取 candidates[0].content.parts[]
                candidates = data.get("candidates", [])
                if not candidates:
                    self.logger.warning("Gemini 原生 API 返回空 candidates")
                    return result

                parts = candidates[0].get("content", {}).get("parts", [])
                text_parts = []
                for part in parts:
                    if "text" in part:
                        text_parts.append(part["text"])
                    elif "inlineData" in part:
                        # 📘 图片数据：base64 编码的图片
                        inline = part["inlineData"]
                        b64_data = inline.get("data", "")
                        if b64_data:
                            try:
                                img_bytes = b64.b64decode(b64_data)
                                result["images"].append(img_bytes)
                            except Exception as e:
                                self.logger.warning(f"base64 图片解码失败: {e}")

                result["text"] = "\n".join(text_parts)

                self.logger.info(
                    f"图片生成完成 (Gemini native): {len(result['images'])} 张图片, "
                    f"文本 {len(result['text'])} 字符"
                )
                return result

            except Exception as e:
                last_error = e
                if not self._is_retryable(e):
                    raise
                self.logger.warning(f"图片生成失败 (attempt {attempt + 1}): {e}")

        raise LLMRetryError(
            f"图片生成在 {self.max_retries} 次重试后仍然失败: {last_error}",
            last_error=last_error,
        )


    def _do_stream_chat(
        self, messages: List[Dict], tools: List[Dict] = None,
        max_tokens: int = None,
    ) -> Generator[Dict, None, None]:
        """
        📘 实际执行一次流式 API 调用

        openai 包的 stream 接口和火山引擎几乎一样（因为都是 OpenAI 协议），
        所以解析逻辑也几乎一样。
        """
        import json as _json

        self.logger.trace(
            f"外部模型请求 [model={self.model_id}]\n"
            f"messages={_json.dumps(_sanitize_for_log(messages), ensure_ascii=False, indent=2)}"
        )

        kwargs = {
            "model": self.model_id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # 📘 教学笔记：max_tokens 控制输出长度
        # Brain 处理复杂表格页时可能输出 10000+ 字符的 JSON，
        # 如果不设 max_tokens，某些模型会用默认值（如 4096）截断输出。
        # ScanAgent 传 max_tokens=16384 确保大页面不被截断。
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            self.logger.trace(
                f"tools={_json.dumps(tools, ensure_ascii=False, indent=2)}"
            )

        stream = self.client.chat.completions.create(**kwargs)

        tool_calls_dict = {}
        full_text = ""

        # 📘 教学笔记：Gemini usage 重复问题修复
        # OpenAI 规范：usage 只在最后一个 chunk 中返回。
        # 但 Gemini 的 OpenAI 兼容层在每个 chunk 都返回 usage，
        # 且值是累计的（不是增量的）。如果每个 chunk 都 += 累加，
        # 会导致 50-60 倍的 token 膨胀（用户看到 6M 实际只有 ~100K）。
        # 修复：只记录最后一次 usage，流结束后 yield 一次。
        last_usage = None

        for chunk in stream:
            # 📘 usage 信息（每个 chunk 都可能带，取最后一个）
            if chunk.usage:
                last_usage = {
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
                    idx = tc.index if tc.index is not None else 0
                    # 📘 教学笔记：Gemini 重复 index 防御
                    # Gemini 有时对多个不同的 tool_call 返回相同的 index（都是 0），
                    # 但每个 tool_call 有不同的 tc.id。如果检测到同一个 index
                    # 收到了新的 tc.id，说明这是一个全新的 tool_call，
                    # 需要分配新的 index 避免名字被拼接（如 5 个工具名连在一起）。
                    if idx in tool_calls_dict and tc.id and tool_calls_dict[idx]["id"] and tc.id != tool_calls_dict[idx]["id"]:
                        # 📘 同一个 index 但不同 id → 新工具调用，分配新 index
                        next_idx = max((k for k in tool_calls_dict if isinstance(k, int)), default=0) + 1
                        idx = next_idx
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls_dict[idx]["id"] = tc.id  # 📘 id 用赋值不用拼接
                    if tc.function and tc.function.name:
                        tool_calls_dict[idx]["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls_dict[idx]["arguments"] += tc.function.arguments
                    # 📘 教学笔记：Gemini thought_signature 支持
                    # Gemini 3.x 模型在 tool_call 中返回 extra_content，
                    # 包含 thought_signature（模型推理状态的加密快照）。
                    # 必须在下一轮对话中原样回传，否则 Gemini 返回 400 错误。
                    # openai SDK 的 pydantic model 配置了 extra='allow'，
                    # 所以 extra_content 会保留在 model_extra 中。
                    extra = getattr(tc, "model_extra", None)
                    if extra and "extra_content" in extra:
                        tool_calls_dict[idx]["extra_content"] = extra["extra_content"]

        if full_text:
            self.logger.trace(f"外部模型响应 [text]\n{full_text}")
        if tool_calls_dict:
            self.logger.trace(
                f"外部模型响应 [tool_calls]\n"
                f"{_json.dumps(dict(tool_calls_dict), ensure_ascii=False, indent=2)}"
            )

        # 📘 教学笔记：流结束后 yield 最终 usage（只 yield 一次）
        # Gemini 每个 chunk 都带 usage（累计值），我们只取最后一个。
        # 火山引擎/OpenAI 只在最后一个 chunk 带 usage，效果一样。
        if last_usage:
            self.logger.debug(
                f"usage (final): prompt={last_usage['prompt_tokens']}, "
                f"completion={last_usage['completion_tokens']}, "
                f"total={last_usage['total_tokens']}"
            )
            yield {"type": "usage", **last_usage}

        # 📘 流结束后，yield 收集到的工具调用
        for idx, tc_data in tool_calls_dict.items():
            result = {
                "type": "tool_call",
                "id": tc_data["id"],
                "name": tc_data["name"],
                "arguments": tc_data["arguments"],
            }
            # 📘 透传 Gemini thought_signature（如果有）
            if "extra_content" in tc_data:
                result["extra_content"] = tc_data["extra_content"]
            yield result


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
