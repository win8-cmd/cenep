"""``python -m cenep`` 入口。

* 默认启动图形界面；
* ``--selftest [输出文件]`` 运行自检（打包后用于验证 EXE 是否完好）。

打包为 ``--windowed`` 时没有控制台，任何未捕获异常都可能弹出对话框并让进程挂住，
因此 ``--selftest`` 分支在这里再加一层兜底：**无论如何都写出报告文件**。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _write_failure_report(output: str | None, exc: BaseException) -> None:
    import json
    import traceback

    payload = {
        "ok": False,
        "stage": "entrypoint-crash",
        "errors": [f"{type(exc).__name__}: {exc}"],
        "traceback": traceback.format_exc(),
        "checks": [],
        "projects": [],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        try:
            Path(output).write_text(text, encoding="utf-8")
        except OSError:
            pass
    else:  # pragma: no cover
        try:
            sys.stdout.write(text + "\n")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--selftest" in args:
        index = args.index("--selftest")
        output = args[index + 1] if len(args) > index + 1 else None
        try:
            from .selftest import run_selftest

            return run_selftest(output)
        except BaseException as exc:  # noqa: BLE001 - 兜底，保证一定留下证据
            _write_failure_report(output, exc)
            return 1

    from .ui.app import run

    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
