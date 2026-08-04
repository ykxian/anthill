"""HTTP 路径常量。

单独一个模块是为了断开循环依赖：`transport/lan.py`（客户端）与 `web/app.py`
（服务端）都需要知道投递路径，但它俩谁也不该 import 对方。
"""

from __future__ import annotations

DELIVER_PATH = "/deliver"
HEALTH_PATH = "/health"
PANEL_PATH = "/panel"
SUMMARY_PATH = "/node/summary"
