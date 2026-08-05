"""`anthill agent start / stop / list / ps`。"""

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
from anthill.core.config import AgentSection, Config, brain_of
from anthill.core.errors import AntHillError
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.providers.registry import TapeMode
from anthill.security import secrets
from anthill.security.approvals import ApprovalStore, approval_confirmer
from anthill.security.confirm import terminal_confirmer
from anthill.web.agents import running_pid, stop_agent
from anthill.web.workspaces import listing as known_workspaces

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
        help="无人值守：不弹确认。需要确认的高风险操作一律 拒绝 —— 不是「全部同意」",
    ),
    approvals: bool = typer.Option(
        False,
        "--approvals",
        help="危险操作写进 .anthill/approvals/ 等人批（跨机场景：本机 `anthill approve --peer`）",
    ),
) -> None:
    """启动一个 agentd：监控自己的邮箱，处理消息，写回执。Ctrl-C 优雅退出。"""
    # 面板上设的密钥在这儿进环境。直接开 agentd（不经 serve）的人也得能用上，
    # 否则「在面板上配好」和「在终端里起」两条路会得出不同的结论。
    secrets.load_into_env()
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
    as_json: bool = typer.Option(False, "--json", help="输出 JSON，便于接进脚本"),
) -> None:
    """列出本节点配置的 Agent 及其运行状态、积压条数。"""
    layout, config = load(workspace)
    if as_json:
        console.print_json(
            data={
                "node": config.node.name,
                "agents": [
                    {
                        "name": name,
                        "role": agent.role,
                        "brain": brain_of(agent),
                        "running": _runtime_state(layout.agent_dir(name) / "runtime.json")[0],
                        "backlog": len(Mailbox(layout.mailbox_dir(name)).list_new()),
                    }
                    for name, agent in sorted(config.agents.items())
                ],
            }
        )
        return

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
    """加点颜色；判定逻辑在 `core/config.brain_of`，和面板共用一份。"""
    label = brain_of(agent)
    return f"[dim]{label}[/dim]" if label == "echo" else f"[cyan]{label}[/cyan]"


def _runtime_state(status_file: Path) -> tuple[bool, str]:
    if not status_file.is_file():
        return False, "-"
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "-"
    return is_running(int(data.get("pid", -1))), str(data.get("watch_mode", "-"))


@agent_app.command("stop")
def stop(
    name: str = typer.Argument(..., help="要停的 Agent 名"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """停掉一个 agentd（SIGTERM，它会把手上那条消息处理完再退）。

    这条命令以前只有面板上有。后果不是理论上的：`start` 是脱离终端独活的
    （`start_new_session=True`），所以不给 `stop` 就意味着**只能 kill**，
    而 `anthill status` 只看当前工作区 —— 别的工作区里遗留的 agentd
    在任何界面里都是不可见的。真跑起来是会攒的。
    """
    layout, config = load(workspace)
    if name not in config.agents:
        fail(f"本节点没有 Agent {name!r}；有的是：{', '.join(sorted(config.agents))}")
    try:
        result = stop_agent(layout, name)
    except AntHillError as exc:
        fail(str(exc))
    if result.get("already"):
        console.print(f"[dim]{name} 本来就没在跑。[/dim]")
    else:
        console.print(f"[green]已停止[/green] {name}（pid {result['pid']}）")


@agent_app.command("ps")
def ps(
    everywhere: bool = typer.Option(
        True, "--all/--here", help="看这台机器上全部工作区，还是只看当前这个"
    ),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """**这台机器上到底跑着哪些 agentd。**

    `anthill status` 只看当前工作区，于是别的工作区里遗留的进程在任何界面里
    都看不见 —— 实测过一次：五个上几次会话遗留的 agentd 跑了近两小时，
    分散在几个工作区里，没有任何地方能发现它们。
    """
    rows: list[tuple[str, str, str, str]] = []
    for path in _workspaces_to_scan(workspace, everywhere):
        layout = NodeLayout(path)
        try:
            config = Config.load_from(layout)
        except AntHillError:
            continue  # 目录还在、配置坏了或没了：跳过，不是这条命令该管的
        for agent in sorted(config.agents):
            pid = running_pid(layout, agent)
            if pid is not None:
                rows.append((config.node.name, agent, str(pid), str(path)))

    if not rows:
        console.print("[dim]这台机器上没有在跑的 agentd。[/dim]")
        return
    table = Table(title="在跑的 agentd", header_style="bold cyan")
    for column in ("节点", "Agent", "pid", "工作区"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(*row)
    console.print(table)
    console.print("[dim]停一个：anthill agent stop <名字> -w <工作区>[/dim]")


def _workspaces_to_scan(workspace: Path | None, everywhere: bool) -> list[Path]:
    """当前这个 + 机器级清单里记着的那些。清单在 `~/.anthill/workspaces.json`。"""
    seen: list[Path] = []
    # 当前目录没有工作区也不该让这条命令报错 —— 它问的是「这台机器上」。
    # 所以这里不走 load()（那条路找不到就 fail），自己安静地找一次。
    with suppress(AntHillError):
        layout = NodeLayout(workspace.resolve()) if workspace else NodeLayout.discover()
        seen.append(layout.workspace)
    if everywhere:
        for entry in known_workspaces():
            candidate = Path(entry["path"])
            if candidate not in seen:
                seen.append(candidate)
    return seen
