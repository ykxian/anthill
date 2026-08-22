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
import signal
import subprocess
import sys
from contextlib import suppress
from typing import TypedDict

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


def kill_tree(pid: int, *, force: bool) -> None:
    """结束 pid 及其整棵子进程树，跨平台。孙子进程也跑不掉。

    POSIX：进程组信号（要求派生时 start_new_session=True —— 本项目的两个
    派生点都开着）。TERM 给对方收尾的机会，force=True 换 KILL。

    Windows：os.killpg / os.getpgid 都是 POSIX 专属（AttributeError），
    改走 taskkill /T（按树）；宽限阶段不带 /F，强杀带上。进程已经没了、
    pid 非法，都静默返回 —— 清理路径上抛错只会把 agentd 自己带下水。
    """
    if pid <= 0:
        return
    if sys.platform == "win32":
        flags = ["/T", "/F"] if force else ["/T"]
        with suppress(OSError):
            subprocess.run(
                ["taskkill", "/PID", str(pid), *flags],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
            )
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), sig)


class DetachKwargs(TypedDict, total=False):
    """`detach_kwargs()` 的形状：两个键**按平台二选一**，所以都是可选的。

    以前这里写的是 `dict[str, object]`。`object` splat 进 `Popen` /
    `create_subprocess_exec` 就对不上它们那一长串精确的关键字类型，
    mypy 会把每个重载变体都报一遍 —— 三个调用点合起来 36 个错误里占了 26 个。
    换成 TypedDict 之后类型是真的对上了，而不是拿 `Any` 把检查关掉。
    """

    creationflags: int
    start_new_session: bool


def detach_kwargs() -> DetachKwargs:
    """让子进程「脱离本进程独活」的 Popen 参数，按平台给对的那套。

    POSIX 是 start_new_session（setsid，自成进程组，超时才能整组杀干净）；
    Windows 上那个参数被静默忽略，改用 CREATE_NEW_PROCESS_GROUP 自成一组 +
    CREATE_NO_WINDOW 隐藏控制台（uv 蹦床在无控制台下会闪黑框，见 web/agents）。
    """
    if sys.platform == "win32":
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        }
    return {"start_new_session": True}
