"""`anthill status` —— 一屏看清节点、Agent、邮箱积压与 watcher 模式。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from anthill.cli.common import console, is_running, load
from anthill.core.config import Config
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRegistry


def status_command(
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """节点总览。排查「为什么收不到消息」时先看这里。"""
    layout, config = load(workspace)

    discovery = (
        "[green]enabled（同网段可见；互投消息仍需配对）[/green]"
        if config.discovery.enabled
        else "[dim]disabled（完全静默：不发包不监听）[/dim]"
    )
    console.print(
        Panel(
            f"节点 [b]{config.node.name}[/b]\n"
            f"工作区 {layout.workspace}\n"
            f"发现   {discovery}\n"
            f"peers  {_peers(layout, config)}",
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


def _peers(layout: NodeLayout, config: Config) -> str:
    """对端有两个来源，只报一个会骗人。

    `[peers.*]` 是手写在 node.toml 里的；`peers invite/trust` 建立的关系落在
    `peers.json`。只看前者的话，配对好的节点在这里显示成「未配置」——
    而这条命令恰恰是「为什么收不到消息」时第一个要看的地方。
    """
    labels = {node: "配置" for node in config.peers}
    for record in PeerRegistry(layout.root).all():
        labels[record.node] = "已信任" if record.trusted else "[dim]仅发现[/dim]"
    if not labels:
        return "（未配置）"
    return "，".join(f"{node}（{labels[node]}）" for node in sorted(labels))


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
