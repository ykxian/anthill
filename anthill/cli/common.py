"""CLI 各命令共用的加载逻辑与渲染工具。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console

from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout

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
    """runtime.json 可能是上次崩溃留下的，得真去确认进程还在。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
