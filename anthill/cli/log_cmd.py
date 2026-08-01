"""`anthill log` —— 看结构化日志，支持 -f 跟随。"""

from __future__ import annotations

from pathlib import Path

import typer

from anthill.cli.common import console, fail, load
from anthill.core.logging import follow_log, format_record, read_log


def log_command(
    agent: str = typer.Argument("", help="Agent 名；留空则列出可用日志"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续跟随新日志"),
    limit: int = typer.Option(50, "--limit", "-n", help="回看多少行"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """查看 agentd 的结构化日志（JSON Lines）。"""
    layout, _ = load(workspace)

    if not agent:
        available = sorted(
            p.stem.removeprefix("agentd-") for p in layout.logs.glob("agentd-*.jsonl")
        )
        console.print("可用日志：" + (", ".join(available) or "[dim]（还没有）[/dim]"))
        return

    path = layout.log_file(agent)
    if not path.is_file() and not follow:
        fail(f"没有 {agent} 的日志：{path}（它还没启动过？）")

    for record in read_log(path, limit=limit):
        console.print(format_record(record))

    if not follow:
        return
    console.print(f"[dim]— 跟随 {path} —[/dim]")
    try:
        for record in follow_log(path):
            console.print(format_record(record))
    except KeyboardInterrupt:
        console.print("\n[dim]已停止跟随[/dim]")
