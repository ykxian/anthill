"""`anthill status` —— 一屏看清节点、Agent、邮箱积压与 watcher 模式。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from anthill.cli.common import console, is_running, load
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox


def status_command(
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """节点总览。排查「为什么收不到消息」时先看这里。"""
    layout, config = load(workspace)

    discovery = (
        "[yellow]enabled[/yellow]"
        if config.discovery.enabled
        else "[green]disabled（默认静默）[/green]"
    )
    console.print(
        Panel(
            f"节点 [b]{config.node.name}[/b]\n"
            f"工作区 {layout.workspace}\n"
            f"发现   {discovery}\n"
            f"peers  {', '.join(config.peers) or '（未配置）'}",
            title="AntHill",
            border_style="cyan",
        )
    )

    table = Table(header_style="bold cyan")
    for column in ("Agent", "角色", "状态", "watcher", "待处理", "处理中", "待发送", "死信"):
        table.add_column(column)

    for name in sorted(config.agents):
        mailbox = Mailbox(layout.mailbox_dir(name))
        running, mode, reason = _runtime_state(layout.agent_dir(name) / "runtime.json")
        dead = len(Outbox(mailbox).dead_letters())
        table.add_row(
            name,
            config.agents[name].role,
            "[green]running[/green]" if running else "[dim]stopped[/dim]",
            f"{mode} [dim]{reason}[/dim]" if reason else mode,
            str(len(mailbox.list_new())),
            str(_count(mailbox.cur)),
            str(_count(mailbox.pending, suffix=".json", skip_meta=True)),
            f"[red]{dead}[/red]" if dead else "0",
        )
    console.print(table)


def _runtime_state(status_file: Path) -> tuple[bool, str, str]:
    if not status_file.is_file():
        return False, "-", ""
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "-", ""
    return (
        is_running(int(data.get("pid", -1))),
        str(data.get("watch_mode", "-")),
        str(data.get("watch_reason", "")),
    )


def _count(directory: Path, *, suffix: str = "", skip_meta: bool = False) -> int:
    if not directory.is_dir():
        return 0
    items = [p for p in directory.iterdir() if p.is_file()]
    if suffix:
        items = [p for p in items if p.name.endswith(suffix)]
    if skip_meta:
        items = [p for p in items if not p.name.endswith(".meta.json")]
    return len(items)
