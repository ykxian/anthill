"""`anthill init` —— 在当前目录建出工作区骨架。"""

from __future__ import annotations

import socket
from pathlib import Path

import typer

from anthill.cli.common import console, fail, ok
from anthill.core.config import Config, default_node_toml
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout


def init_command(
    path: Path = typer.Argument(Path("."), help="工作区目录，默认当前目录"),
    node_name: str = typer.Option("", "--node-name", "-n", help="节点名，默认取主机名"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 node.toml"),
) -> None:
    """初始化 .anthill 工作区（node.toml + agents/ + blackboard/ + logs/）。"""
    layout = NodeLayout(path.resolve()).ensure_base()
    name = node_name or _default_node_name()

    if layout.node_toml.is_file() and not force:
        fail(f"{layout.node_toml} 已存在；要重建请加 --force")

    layout.node_toml.write_text(default_node_toml(name), encoding="utf-8")
    config = Config.load_from(layout)
    for agent_name in config.agents:
        Mailbox(layout.mailbox_dir(agent_name)).ensure()

    (layout.blackboard / "BOARD.md").write_text(
        "# BOARD\n\n> 当前协作状态快照，由 coordinator 单写者维护。\n", encoding="utf-8"
    )

    ok(f"工作区已就绪：{layout.root}")
    console.print(f"  节点名   [b]{name}[/b]")
    console.print(f"  Agent    {', '.join(config.agents) or '（无）'}")
    console.print("\n下一步：")
    console.print("  [dim]终端 1[/dim] anthill agent start echo")
    console.print('  [dim]终端 2[/dim] anthill send echo "跑通链路" --wait')


def _default_node_name() -> str:
    raw = socket.gethostname().split(".")[0].lower()
    cleaned = "".join(c if c.isalnum() or c in "._-" else "-" for c in raw)
    return cleaned or "node"
