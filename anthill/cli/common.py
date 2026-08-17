"""CLI 各命令共用的加载逻辑与渲染工具。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console

from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout
from anthill.core.procs import process_alive

console = Console()
err_console = Console(stderr=True)


def load(workspace: Path | None = None) -> tuple[NodeLayout, Config]:
    """定位工作区并加载配置。任何失败都变成一条人类可读的错误 + 非零退出码。"""
    try:
        layout = NodeLayout(workspace.resolve()) if workspace else NodeLayout.discover()
        config = Config.load_from(layout)
    except AntHillError as exc:
        fail(str(exc))
    return layout, config


def fail(message: str, code: int = 1) -> NoReturn:
    """打印错误并退出。标成 NoReturn，调用点后面的代码就不会被当成「可能执行到」。"""
    # soft_wrap：错误里常常带路径和命令，被从中间折断就复制不了。
    # 项目在 peers invite 的令牌上已经这么做过，同样的道理这里一直没做。
    err_console.print(f"[bold red]✗[/bold red] {message}", soft_wrap=True)
    raise typer.Exit(code)


def ok(message: str) -> None:
    console.print(f"[bold green]✓[/bold green] {message}")


def is_running(pid: int) -> bool:
    """runtime.json 可能是上次崩溃留下的，得真去确认进程还在。

    必须走 process_alive —— 裸的 os.kill(pid, 0) 在 Windows 上是
    Ctrl+C 广播，会把共用控制台的 serve 一起打断（见 core/procs.py）。
    """
    return process_alive(pid)


STDIN_MARKER = "-"
FILE_PREFIX = "@"


def read_body(value: str) -> str:
    """任务正文/消息正文：支持 `-`（读 stdin）与 `@路径`（读文件）。

    正文以前只能当位置参数传，于是一段稍长的 prompt 要么被 shell 的引号规则
    折磨，要么根本没法带换行。`-` 和 `@file` 是 curl/kubectl 那一套约定，
    不用学。真正以 `-`/`@` 开头的正文：`@` 打两个（`@@`），或者走 stdin。
    """
    if value == STDIN_MARKER:
        return sys.stdin.read().strip()
    if value.startswith(FILE_PREFIX * 2):
        return value[1:]  # 转义：`@@x` 就是字面量 `@x`
    if value.startswith(FILE_PREFIX):
        path = Path(value[1:]).expanduser()
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            fail(f"读不了 {path}：{exc}")
    return value
