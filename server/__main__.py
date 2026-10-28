# -*- coding: utf-8 -*-
"""``python -m server`` 启动入口：uvicorn 跑 127.0.0.1:8787（env PORT 可覆盖）。"""

from __future__ import annotations

import os
import sys

# Windows 控制台常为 GBK，emoji/中文替换字符会炸日志 -> 降级为替换
for _stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    except Exception:
        pass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def main() -> int:
    import uvicorn

    host = os.environ.get("HOST", DEFAULT_HOST) or DEFAULT_HOST
    try:
        port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT
    uvicorn.run("server.app:app", host=host, port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
