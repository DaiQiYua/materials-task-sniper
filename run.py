# -*- coding: utf-8 -*-
"""启动入口：python run.py [--reload] [--host H] [--port P] [--log-level L]

等价于用 uvicorn 跑 ``server.app:app``，并提供开发态热重载（``--reload``）。
依赖：uvicorn（建议 ``pip install "uvicorn[standard]"``，reload 需要 watchfiles）。
"""
from __future__ import annotations

import argparse
import os
import sys

# Windows 控制台常为 GBK，emoji/中文替换字符会炸日志 -> 降级为替换
for _stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    except Exception:
        pass

# 保证无论从哪个 cwd 启动都能 import 到 server 包
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

DEFAULT_HOST = os.environ.get("HOST", "127.0.0.1")
try:
    DEFAULT_PORT = int(os.environ.get("PORT", "8787"))
except ValueError:
    DEFAULT_PORT = 8787


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="任务狙击台后端启动器")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 8787）")
    parser.add_argument("--reload", action="store_true", help="启用热重载（开发模式）")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别（默认 info）")
    args = parser.parse_args()

    # reload 必须用 import string，让 uvicorn 在重载子进程里重新导入 app
    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
