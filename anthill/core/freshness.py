"""「磁盘上的代码」和「跑着的进程」是不是同一个版本。

editable 安装下改代码、同步新版，磁盘立刻是新的，**进程还是旧的** ——
serve 与每只 agentd 各自为政，谁没重启谁就带病工作。这个错位今天一天
双侧合计踩了三次（serve 一次、agentd 两次），全是「明明修了怎么还坏」。
机器知道答案却不说，就是缺陷：比一比进程的启动时刻和代码的最新改动时刻。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
"""anthill 包目录。editable 装下就是源码树；普通安装下是 site-packages
里的拷贝 —— 升级包会刷新 mtime，语义同样成立。"""

_TTL_SECONDS = 15.0
_cache: tuple[float, float] | None = None

_GRACE_SECONDS = 1.0
"""启动那一刻的写入（pyc 之外偶有触碰）别误报成「代码更新了」。"""


def newest_code_mtime(*, refresh: bool = False) -> float:
    """包里最新一个 .py 的 mtime。带 15s 缓存 —— 状态循环每 30s 问一次，
    但 WebSocket 每 2s 也会路过，别让它每次都走一遍文件树。

    自动重启监控要准确知道一组编辑什么时候真正安静下来，所以它显式传
    ``refresh=True`` 绕过缓存；普通状态展示仍走便宜的缓存路径。
    """
    global _cache
    now = time.monotonic()
    if not refresh and _cache is not None and now - _cache[0] < _TTL_SECONDS:
        return _cache[1]
    newest = 0.0
    for path in CODE_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    _cache = (now, newest)
    return newest


def stale_since(started_iso: str, *, code_mtime: float | None = None) -> bool:
    """`started_iso` 之后代码又改过 = 这个进程跑的是旧版，该重启了。"""
    try:
        started = datetime.fromisoformat(started_iso).timestamp()
    except (ValueError, TypeError):
        return False
    newest = newest_code_mtime() if code_mtime is None else code_mtime
    return newest > started + _GRACE_SECONDS
