"""应用启动入口（规范 §130、§131、§135）。

GUI 只做三件事：建立 QApplication、建立主窗口、进入事件循环。
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..infrastructure.logging_setup import setup_logging

APP_NAME = "CENEP V1"
ORG_NAME = "CENEP"


def create_application(argv: list[str] | None = None):
    """建立 QApplication（已存在则复用，便于测试）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    return app


def create_window(db_path: str | Path = "cenep.db"):
    """建立主窗口（供测试与脚本调用，不进入事件循环）。"""
    from .main_window import MainWindow

    return MainWindow(db_path=db_path)


def run(argv: list[str] | None = None, db_path: str | Path = "cenep.db") -> int:
    """启动 GUI，返回退出码。"""
    logger = setup_logging()
    logger.info("启动 %s", APP_NAME)
    app = create_application(argv)
    window = create_window(db_path)
    window.show()
    return app.exec()


__all__ = ["run", "create_application", "create_window", "APP_NAME"]
