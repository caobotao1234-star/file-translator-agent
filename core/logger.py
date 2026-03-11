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
# Python 标准库的 logging 模块就够用了：
#   - 支持日志级别过滤
#   - 支持同时输出到终端和文件
#   - 支持格式化（时间戳、模块名、级别）
#   - 每个 Agent 可以有自己的 logger 实例（通过 name 区分）
# =============================================================

LOG_DIR = "logs"


def get_logger(
    name: str = "agent",
    level: str = "DEBUG",
    log_to_file: bool = True,
) -> logging.Logger:
    """
    获取一个配置好的 logger 实例。

    参数：
        name: logger 名称，建议用 Agent 名字
              （如 "orchestrator", "translator"）
        level: 日志级别，DEBUG/INFO/WARNING/ERROR
        log_to_file: 是否同时写入日志文件

    用法：
        logger = get_logger("translator")
        logger.info("开始翻译任务")
        logger.debug(f"LLM 返回: {response}")
        logger.error(f"翻译失败: {e}")
    """
    logger = logging.getLogger(name)

    # 避免重复添加 handler（多次调用 get_logger 时）
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.DEBUG))

    # 日志格式：时间 | 级别 | 模块名 | 消息
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 终端输出（只显示 INFO 及以上，避免 DEBUG 刷屏）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出（记录所有级别，方便排查问题）
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
