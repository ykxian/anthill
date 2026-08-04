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
from anthill.agent.tools.base import Confirmer
from anthill.cli.common import console, fail, is_running, load
from anthill.core.config import AgentSection
from anthill.core.errors import AntHillError
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.providers.registry import TapeMode
from anthill.security.approvals import ApprovalStore, approval_confirmer
from anthill.security.confirm import terminal_confirmer

agent_app = typer.Typer(no_args_is_help=True, help="Agent 守护进程")


@agent_app.command("start")
def start(
    name: str = typer.Argument(..., help="Agent 名（需在 node.toml 中已配置）"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="只写日志文件，不在终端回显"),
    record: Path | None = typer.Option(
        None, "--record", help="把每次模型调用录进这个 jsonl，供回放/排查"
    ),
    replay: Path | None = typer.Option(
        None, "--replay", help="用录制带当假模型跑，不产生 API 费用"
    ),
    unattended: bool = typer.Option(
        False,
        "--unattended",
        "-u",
        help="无人值守：不弹确认，需要确认的高风险操作一律拒绝（不是「全部同意」）",
    ),
    approvals: bool = typer.Option(
        False,
        "--approvals",
        help="危险操作写进 .anthill/approvals/ 等人批（跨机场景：本机 `anthill approve --peer`）",
    ),
) -> None:
    """启动一个 agentd：监控自己的邮箱，处理消息，写回执。Ctrl-C 优雅退出。"""
    layout, config = load(workspace)
    if record and replay:
        fail("--record 与 --replay 不能同时使用")
    mode = TapeMode.REPLAY if replay else (TapeMode.RECORD if record else TapeMode.LIVE)
    try:
        runtime = AgentRuntime(
            layout=layout,
            config=config,
            agent_name=name,
            echo=not quiet,
            mode=mode,
            tape=replay or record,
            confirm=_confirmer(layout, name, unattended=unattended, approvals=approvals),
        )
    except AntHillError as exc:
        fail(str(exc))

    try:
        asyncio.run(_run(runtime))
    except KeyboardInterrupt:  # asyncio.run 在信号处理外仍可能抛
        console.print("\n[dim]已停止[/dim]")


def _confirmer(
    layout: NodeLayout, name: str, *, unattended: bool, approvals: bool
) -> Confirmer | None:
    """三种确认方式，按显式程度排序。

    --unattended  谁也不问，需要确认的一律拒绝
    --approvals   写进 approvals 目录等人批（远端无人值守但仍要人点头时用）
    默认          在本终端上问（没有 tty 时退化为「没人能确认」→ 拒绝）
    """
    if unattended:
        return None
    if approvals:
        return approval_confirmer(ApprovalStore(layout.root), agent=name)
    return terminal_confirmer(console)


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
    for column in ("Agent", "角色", "大脑", "状态", "待处理", "watcher"):
        table.add_column(column)

    for name, agent in sorted(config.agents.items()):
        status_file = layout.agent_dir(name) / "runtime.json"
        running, mode = _runtime_state(status_file)
        mailbox = Mailbox(layout.mailbox_dir(name))
        table.add_row(
            name,
            agent.role,
            _brain(agent),
            "[green]running[/green]" if running else "[dim]stopped[/dim]",
            str(len(mailbox.list_new())),
            mode,
        )
    console.print(table)


def _brain(agent: AgentSection) -> str:
    """这个 Agent 的大脑是什么。

    桥接 Agent 显示成 `echo` 会让人以为它不干活 —— 实际上它背后是一个人
    （或一个常驻会话）。「在跑什么」这张表是排查时第一眼看的东西，不能误导。
    """
    if agent.bridge:
        return "[cyan]bridge[/cyan]"
    if agent.command:
        return f"[cyan]{agent.command[0]}[/cyan]"
    return agent.provider or "[dim]echo[/dim]"


def _runtime_state(status_file: Path) -> tuple[bool, str]:
    if not status_file.is_file():
        return False, "-"
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "-"
    return is_running(int(data.get("pid", -1))), str(data.get("watch_mode", "-"))
