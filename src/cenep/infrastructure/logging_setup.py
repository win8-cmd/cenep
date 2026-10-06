"""日志配置（规范 §133）。

记录：项目加载、计算开始、计算结束、异常、导出。日志写入 ``logs/`` 目录。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "cenep"
_DEFAULT_DIR = Path("logs")
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3


def setup_logging(
    log_dir: str | Path | None = None, level: int = logging.INFO, force: bool = False
) -> logging.Logger:
    """初始化日志，返回 ``cenep`` logger。

    重复调用是安全的（不会重复添加 handler）；``force=True`` 会先移除已有 handler 并切换目录。
    """
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers and not force:
        return logger
    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    directory = Path(log_dir) if log_dir is not None else _DEFAULT_DIR
    directory.mkdir(parents=True, exist_ok=True)

    logger.setLevel(level)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        directory / "cenep.log", maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def get_logger() -> logging.Logger:
    """取日志器；未初始化时按默认路径初始化。"""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        return setup_logging()
    return logger
