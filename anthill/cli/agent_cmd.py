"""`anthill agent start / list`。"""

from __future__ import annotations

import asyncio
import json
import signal
from contextlib import suppress
from pathlib import Path

import typer
from rich.table import Table

from anthill.agent.runtime import AgentRuntime
from anthill.cli.common import console, fail, is_running, load
from anthill.core.errors import AntHillError
from anthill.core.mailbox import Mailbox

agent_app = typer.Typer(no_args_is_help=True, help="Agent 守护进程")


@agent_app.command("start")
def start(
    name: str = typer.Argument(..., help="Agent 名（需在 node.toml 中已配置）"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="只写日志文件，不在终端回显"),
) -> None:
    """启动一个 agentd：监控自己的邮箱，处理消息，写回执。Ctrl-C 优雅退出。"""
    layout, config = load(workspace)
    try:
        runtime = AgentRuntime(layout=layout, config=config, agent_name=name, echo=not quiet)
    except AntHillError as exc:
        fail(str(exc))

    try:
        asyncio.run(_run(runtime))
    except KeyboardInterrupt:  # asyncio.run 在信号处理外仍可能抛
        console.print("\n[dim]已停止[/dim]")


async def _run(runtime: AgentRuntime) -> None:
    """Ctrl-C / SIGTERM 只是 set 一个事件，让 agentd 把手上的消息处理完再退。"""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):  # Windows 上不支持
            loop.add_signal_handler(sig, stop.set)
    await runtime.run(stop)


@agent_app.command("list")
def list_agents(
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """列出本节点配置的 Agent 及其运行状态、积压条数。"""
    layout, config = load(workspace)

    table = Table(title=f"节点 {config.node.name}", header_style="bold cyan")
    for column in ("Agent", "角色", "Provider", "状态", "待处理", "watcher"):
        table.add_column(column)

    for name, agent in sorted(config.agents.items()):
        status_file = layout.agent_dir(name) / "runtime.json"
        running, mode = _runtime_state(status_file)
        mailbox = Mailbox(layout.mailbox_dir(name))
        table.add_row(
            name,
            agent.role,
            agent.provider or "[dim]echo[/dim]",
            "[green]running[/green]" if running else "[dim]stopped[/dim]",
            str(len(mailbox.list_new())),
            mode,
        )
    console.print(table)


def _runtime_state(status_file: Path) -> tuple[bool, str]:
    if not status_file.is_file():
        return False, "-"
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "-"
    return is_running(int(data.get("pid", -1))), str(data.get("watch_mode", "-"))
