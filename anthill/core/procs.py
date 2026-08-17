"""跨平台的「这个进程还活着吗」。

POSIX 的标准写法是 `os.kill(pid, 0)`：不真发信号，只做权限与存在性检查。
但 Windows 上 `signal.CTRL_C_EVENT == 0`，同一行代码的含义变成
**向控制台进程组广播 Ctrl+C** —— 而面板拉起的 agentd 在 Windows 上和
serve 共用控制台（`start_new_session` 是 POSIX 专属，Windows 直接忽略）。
于是第一次周期性活性探测就把 serve 自己打断退出：「启动 Agent 后
约 30 秒 serve 自动停止」，Windows 实机踩出来的。

探测一律走这里的 `process_alive`；裸写 `os.kill(pid, 0)` 被
tests/unit/test_procs.py 里的扫描钉死。
"""

from __future__ import annotations

import os
import sys

_STILL_ACTIVE = 259
"""GetExitCodeProcess 对还在跑的进程返回的哨兵值（winbase.h 的 STILL_ACTIVE）。

已知理论盲点（MS 文档明示）：进程恰好以退出码 259 退出会被误判存活。
概率可忽略，但别用 259 当自己程序的退出码。"""


def process_alive(pid: int) -> bool:
    """`pid` 对应的进程是否还在。别人的进程也算「在」。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 别人的进程，但确实在
    return True


def _alive_windows(pid: int) -> bool:
    """OpenProcess + GetExitCodeProcess，不发任何控制台事件。

    只要 PROCESS_QUERY_LIMITED_INFORMATION —— 权限最低，连别的用户的
    进程也查得到。打不开句柄就当不在（进程没了，或真的一点权限都没有）。
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
