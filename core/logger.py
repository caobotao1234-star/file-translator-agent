# core/logger.py
import logging
import os
import sys
from datetime import datetime

# =============================================================
# 📘 教学笔记：结构化日志系统
# =============================================================
# 为什么不用 print()？
#   - print() 无法区分日志级别（DEBUG/INFO/WARNING/ERROR）
#   - print() 无法输出到文件，生产环境看不到终端
#   - print() 没有时间戳、模块名等上下文信息
#   - 多 Agent 并发时，print() 输出会混在一起，分不清谁打的
#
# 日志级别控制：
#   通过 .env 中的 LOG_LEVEL 环境变量控制终端输出级别：
#     LOG_LEVEL=TRACE   → 终端显示完整 LLM 请求/响应原文（排查用）
#     LOG_LEVEL=DEBUG   → 终端显示所有日志（含 LLM 输入输出摘要）
#     LOG_LEVEL=INFO    → 终端只显示关键流程信息（默认）
#     LOG_LEVEL=WARNING → 终端只显示警告和错误
#   文件日志始终记录 DEBUG 级别（TRACE 模式下记录 TRACE）。
# =============================================================

LOG_DIR = "logs"

# 📘 教学笔记：自定义 TRACE 级别
# Python logging 内置级别：DEBUG=10, INFO=20, WARNING=30, ERROR=40
# 我们加一个 TRACE=5，比 DEBUG 更低，用于输出完整的 LLM 对话内容。
# 日志级别层级：
#   TRACE(5)   → 完整 LLM 请求/响应原文（非常大量，仅排查时用）
#   DEBUG(10)  → 输入输出摘要、内部流程细节
#   INFO(20)   → 关键流程节点（默认）
#   WARNING(30)→ 警告和降级处理
#   ERROR(40)  → 错误
TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE):
        # 📘 注意：Logger._log() 的签名是 _log(level, msg, args, ...)
        # 第三个参数 args 是一个元组（不是 *args 展开），必须显式传递。
        self._log(TRACE, message, args, **kwargs)


# 给 Logger 类挂上 trace 方法，这样所有 logger 实例都能用 logger.trace(...)
logging.Logger.trace = _trace

_CONSOLE_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def get_logger(
    name: str = "agent",
    level: str = "DEBUG",
    log_to_file: bool = True,
) -> logging.Logger:
    """
    获取一个配置好的 logger 实例。

    终端日志级别由环境变量 LOG_LEVEL 控制（默认 INFO）。
    文件日志始终记录 DEBUG 级别。
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    # 📘 教学笔记：TRACE 级别处理
    # TRACE=5 不在 logging 内置级别里，getattr(logging, "TRACE") 会失败
    # 所以需要特殊处理：如果 level 或 _CONSOLE_LEVEL 是 TRACE，用我们自定义的常量
    def _resolve_level(name: str) -> int:
        return TRACE if name == "TRACE" else getattr(logging, name, logging.DEBUG)

    logger.setLevel(min(_resolve_level(level.upper()), _resolve_level(_CONSOLE_LEVEL)))

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 📘 教学笔记：终端输出 vs GUI 模式
    # GUI 模式下，root logger 已经有 LogInterceptor，
    # 子 logger 的消息会通过 propagate 冒泡上去。
    # 如果这里再加 StreamHandler，就会重复输出。
    # 判断方法：检查 root logger 是否已有 LogInterceptor（GUI 模式标志）。
    root_has_gui_handler = any(
        type(h).__name__ == "LogInterceptor"
        for h in logging.getLogger().handlers
    )
    if not root_has_gui_handler:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(_resolve_level(_CONSOLE_LEVEL))
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    # 文件输出（始终 DEBUG，TRACE 模式下降为 TRACE）
    if log_to_file:
        os.makedirs(LOG_DIR, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        file_handler = logging.FileHandler(
            os.path.join(LOG_DIR, f"{today}.log"),
            encoding="utf-8",
        )
        file_level = TRACE if _CONSOLE_LEVEL == "TRACE" else logging.DEBUG
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
