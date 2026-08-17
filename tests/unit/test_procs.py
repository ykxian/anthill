"""进程活性探测必须跨平台。

`os.kill(pid, 0)` 是 POSIX 的标准探测写法，但在 Windows 上
`signal.CTRL_C_EVENT == 0`，同一行代码变成**向控制台进程组广播 Ctrl+C**：
面板上启动一个 agentd（Windows 上 `start_new_session` 被忽略，它和 serve
共用控制台），第一次周期性探测一响，serve 整个被自己的探针打断退出 ——
「启动 Agent 后约 30 秒 serve 自动停止」，Windows 实机踩出来的。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from anthill.core.procs import process_alive

SRC = Path(__file__).resolve().parents[2] / "anthill"


def test_own_process_is_alive() -> None:
    import os

    assert process_alive(os.getpid()) is True


def test_a_finished_process_is_not_alive() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert process_alive(child.pid) is False


def test_nonsense_pids_are_not_alive() -> None:
    assert process_alive(0) is False
    assert process_alive(-1) is False


def test_no_raw_kill_zero_probe_outside_procs() -> None:
    """探测一律走 process_alive —— 裸写 `os.kill(pid, 0)` 在 Windows 上是
    Ctrl+C 广播，这颗雷排过一次就不许再埋回来。按 AST 找真实调用，
    注释和文档里提到它没关系。"""
    import ast

    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "procs.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and len(node.args) >= 2):
                continue
            func = node.func
            named_kill = (
                isinstance(func, ast.Attribute)
                and func.attr == "kill"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            second_is_zero = (
                isinstance(node.args[1], ast.Constant) and node.args[1].value == 0
            )
            if named_kill and second_is_zero:
                offenders.append(f"{path.relative_to(SRC.parent)}:{node.lineno}")
    assert not offenders, f"裸的 os.kill(pid, 0) 探测又出现了：{offenders}"
