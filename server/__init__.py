# -*- coding: utf-8 -*-
"""materials-task-sniper Web 后端包。

模块划分：
- ``server.core``   : ``SniperEngine``（实例化的狙击引擎，无模块级可变全局）
- ``server.app``    : FastAPI 应用（REST + SSE + 静态托管 + 同源校验）
- ``server.persist``: 数据目录与落盘（token/config/history/log）
- ``server.__main__``: ``python -m server`` 启动入口
"""

__all__ = ["core", "app", "persist"]
__version__ = "1.0.0"
