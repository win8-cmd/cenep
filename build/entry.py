"""PyInstaller 打包入口脚本（规范 §130、§131）。

生成的可执行文件双击即运行，用户无需安装 Python 或任何依赖。
"""

from __future__ import annotations

import multiprocessing
import sys

from cenep.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # Windows 打包必需
    sys.exit(main())
