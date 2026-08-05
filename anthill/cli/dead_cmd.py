"""`anthill dead` —— 死信的查看、重投与清理。

以前死信只有一条出路：没有出路。`dead_letters()` 只被用来在 `status` 里数个数，
既看不到内容，也没有重投或清理的命令 —— 唯一的恢复手段是手动 `mv` 文件。

这很要命，因为进死信最常见的原因恰恰是**修好之后就该重投**的那种：
对端 agentd 晚起了十秒。所以这一组命令是必需品，不是锦上添花。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from anthill.cli.common import console, fail, load
from anthill.core.errors import AntHillError
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import DeadLetter, Outbox
from anthill.core.paths import NodeLayout

dead_app = typer.Typer(help="死信：看看有什么没送出去，修好之后重投")

REASON_WIDTH = 60


def _outbox(workspace: Path | None, agent: str) -> tuple[Outbox, NodeLayout]:
    layout, config = load(workspace)
    if agent not in config.agents:
        fail(f"本节点没有 Agent {agent!r}；有的是：{', '.join(sorted(config.agents))}")
    return Outbox(Mailbox(layout.mailbox_dir(agent))), layout


@dead_app.command("list")
def list_dead(
    agent: str = typer.Argument("cli", help="看哪个 Agent 的发件箱"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """列出死信 —— 发给谁、为什么死的。"""
    outbox, _ = _outbox(workspace, agent)
    letters = outbox.dead_letter_list()
    if not letters:
        console.print(f"[dim]{agent} 没有死信。[/dim]")
        return

    table = Table(title=f"{agent} 的死信", header_style="bold red")
    for column in ("消息", "发给", "试了几次", "为什么"):
        table.add_column(column)
    for letter in letters:
        table.add_row(
            letter.msg_id[-6:],
            letter.to or "-",
            letter.attempts or "-",
            _clip(letter.reason),
        )
    console.print(table)
    console.print(
        f"[dim]重投：anthill dead retry {agent} <消息>（`--all` 全部）"
        f"；不要了：anthill dead drop {agent} <消息>[/dim]"
    )


@dead_app.command("retry")
def retry_dead(
    agent: str = typer.Argument("cli", help="哪个 Agent 的发件箱"),
    msg_id: str = typer.Argument("", help="消息 ID（可只给末尾几位）"),
    retry_all: bool = typer.Option(False, "--all", help="重投全部死信"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """把死信放回发件箱重新投递。

    重投只是把信放回 pending —— **真正发出去要 agentd 在跑**，
    它的重试循环会捡起来。这一点值得说清楚，免得看到「已重投」却什么都没发生。
    """
    outbox, _ = _outbox(workspace, agent)
    targets = _targets(outbox, msg_id, retry_all)
    for letter in targets:
        try:
            outbox.requeue_dead(letter.msg_id)
        except AntHillError as exc:
            fail(str(exc))
        console.print(f"[green]已放回发件箱[/green] {letter.msg_id[-6:]} → {letter.to}")
    console.print(f"[dim]共 {len(targets)} 条。agentd 在跑的话，下一轮重试就会发出去。[/dim]")


@dead_app.command("drop")
def drop_dead(
    agent: str = typer.Argument("cli", help="哪个 Agent 的发件箱"),
    msg_id: str = typer.Argument("", help="消息 ID（可只给末尾几位）"),
    drop_all: bool = typer.Option(False, "--all", help="删掉全部死信"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """确认不要了，删掉。"""
    outbox, _ = _outbox(workspace, agent)
    targets = _targets(outbox, msg_id, drop_all)
    dropped = sum(1 for letter in targets if outbox.drop_dead(letter.msg_id))
    console.print(f"[green]删掉了 {dropped} 条死信。[/green]")


def _targets(outbox: Outbox, msg_id: str, everything: bool) -> list[DeadLetter]:
    """`--all` 或者一个（可以只给末尾几位的）ID。匹配到多条就让人说清楚，不猜。"""
    letters = outbox.dead_letter_list()
    if everything:
        if msg_id:
            fail("--all 和指定消息 ID 只能选一个")
        return letters
    if not msg_id:
        fail("给一个消息 ID，或者用 --all")
    matched = [x for x in letters if x.msg_id == msg_id or x.msg_id.endswith(msg_id)]
    if not matched:
        fail(f"没有匹配 {msg_id!r} 的死信；先跑 `anthill dead list` 看看")
    if len(matched) > 1:
        fail(
            f"{msg_id!r} 匹配到 {len(matched)} 条，多给几位："
            + ", ".join(x.msg_id[-10:] for x in matched)
        )
    return matched


def _clip(text: str, limit: int = REASON_WIDTH) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
