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
#     LOG_LEVEL=DEBUG   → 终端显示所有日志（含 LLM 输入输出摘要）
#     LOG_LEVEL=INFO    → 终端只显示关键流程信息（默认）
#     LOG_LEVEL=WARNING → 终端只显示警告和错误
#   文件日志始终记录 DEBUG 级别，方便事后排查。
# =============================================================

LOG_DIR = "logs"

# 📘 教学笔记：从环境变量读取日志级别
# 这样不需要改代码，只改 .env 就能切换调试模式。
# 生产环境用 INFO，排查问题时临时改成 DEBUG。
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

    logger.setLevel(getattr(logging, level.upper(), logging.DEBUG))

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 终端输出（级别由 LOG_LEVEL 环境变量控制）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, _CONSOLE_LEVEL, logging.INFO))
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出（始终 DEBUG）
    if log_to_file:
        os.makedirs(LOG_DIR, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        file_handler = logging.FileHandler(
            os.path.join(LOG_DIR, f"{today}.log"),
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
