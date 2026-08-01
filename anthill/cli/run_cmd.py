"""`anthill run "<任务>"` —— 一条命令跑通多 Agent 协同。

它做的事很薄：把任务投给 coordinator，然后盯着两个数据源渲染实时画面 ——
黑板上的 `state.json`（每步进展）与自己的收件箱（消息流）。
**编排逻辑一行都不在这里**，全在 coordinator 里；CLI 挂掉不影响协作继续跑。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import typer
from rich.console import ConsoleRenderable, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from anthill.agent.sender import Sender
from anthill.cli.common import console, fail, load
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import AntHillError, ConfigError
from anthill.core.ids import new_thread_id, now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import (
    MessageType,
    TaskErrorPayload,
    TaskRequestPayload,
    TaskResultPayload,
)
from anthill.core.router import Router
from anthill.core.states import DeliveryTracker
from anthill.orchestrator.state import RunState, RunStore, StepState
from anthill.transport.registry import TransportRegistry

COORDINATOR_ROLE = "coordinator"
DEFAULT_CLI_AGENT = "cli"
POLL_INTERVAL = 0.3
DEFAULT_TIMEOUT = 600.0
MAX_STREAM_LINES = 12

STATE_STYLE = {
    StepState.PENDING: ("…", "dim"),
    StepState.RUNNING: ("▶", "yellow"),
    StepState.DONE: ("✓", "green"),
    StepState.FAILED: ("✗", "bold red"),
    StepState.SKIPPED: ("—", "dim red"),
}


def run_command(
    task: str = typer.Argument(..., help="要完成的任务，用自然语言描述"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    to: str = typer.Option("", "--to", help="指定 coordinator，默认自动找 role=coordinator 的"),
    timeout: float = typer.Option(DEFAULT_TIMEOUT, "--timeout", help="最长等待秒数"),
    plain: bool = typer.Option(False, "--plain", help="不用实时画面，只按行打印（便于重定向）"),
) -> None:
    """把任务交给 coordinator，实时看它拆解、派活、汇总。"""
    layout, config = load(workspace)
    try:
        coordinator = to or _find_coordinator(config)
    except ConfigError as exc:
        fail(str(exc))

    try:
        asyncio.run(
            _run(
                layout=layout,
                config=config,
                task=task,
                coordinator=coordinator,
                timeout=timeout,
                plain=plain,
            )
        )
    except AntHillError as exc:
        fail(str(exc))
    except KeyboardInterrupt:
        console.print("\n[dim]已退出观察；协作仍在后台继续，用 `anthill status` 查看[/dim]")


def _find_coordinator(config: Config) -> str:
    candidates = [a.name for a in config.agents_with_role(COORDINATOR_ROLE)]
    if not candidates:
        raise ConfigError(
            'node.toml 里没有 role = "coordinator" 的 Agent；'
            "先配一个（并给它 provider），或用 --to 指定"
        )
    return sorted(candidates)[0]


async def _run(
    *,
    layout: NodeLayout,
    config: Config,
    task: str,
    coordinator: str,
    timeout: float,
    plain: bool,
) -> None:
    identity = Address(node=config.node.name, agent=DEFAULT_CLI_AGENT)
    mailbox = Mailbox(layout.mailbox_dir(DEFAULT_CLI_AGENT)).ensure()
    log = EventLog(layout.log_file(DEFAULT_CLI_AGENT), agent=DEFAULT_CLI_AGENT, echo=False)
    sender = Sender(
        identity=identity,
        mailbox=mailbox,
        router=Router(config, layout),
        transports=TransportRegistry(config, layout),
        tracker=DeliveryTracker(),
        log=log,
    )

    thread = new_thread_id()
    try:
        await sender.send_new(
            to=Address(node=config.node.name, agent=coordinator),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title=_clip(task), body=task),
            thread=thread,
        )
    finally:
        log.close()

    console.print(f"[bold green]→[/bold green] {coordinator} [dim]thread={thread[-6:]}[/dim]")
    watcher = RunWatcher(layout=layout, mailbox=mailbox, thread=thread, task=task, timeout=timeout)
    await (watcher.follow_plain() if plain else watcher.follow_live())


class RunWatcher:
    """只读观察者：轮询黑板与收件箱，不参与任何编排决策。"""

    def __init__(
        self,
        *,
        layout: NodeLayout,
        mailbox: Mailbox,
        thread: str,
        task: str,
        timeout: float,
    ) -> None:
        self._store = RunStore(layout.blackboard)
        self._mailbox = mailbox
        self._thread = thread
        self._task = task
        self._timeout = timeout
        self._stream: list[str] = []
        self._started = now()
        self._final: Envelope | None = None

    # ---------- 两种渲染 ----------

    async def follow_live(self) -> None:
        with Live(self._render(), console=console, refresh_per_second=4) as live:
            while await self._step():
                live.update(self._render())
            live.update(self._render())
        self._print_outcome()

    async def follow_plain(self) -> None:
        while await self._step():
            pass
        for line in self._stream:
            console.print(line)
        self._print_outcome()

    async def _step(self) -> bool:
        """推进一轮观察。返回 False 表示可以收工了。"""
        if now() - self._started > timedelta(seconds=self._timeout):
            self._stream.append("[yellow]![/yellow] 等待超时，协作可能仍在后台继续")
            return False
        self._drain_inbox()
        if self._final is not None:
            return False
        await asyncio.sleep(POLL_INTERVAL)
        return True

    def _drain_inbox(self) -> None:
        for path in self._mailbox.list_new():
            try:
                env = Mailbox.read_envelope(path)
            except AntHillError:
                continue
            if env.thread != self._thread:
                continue
            self._mailbox.archive(self._mailbox.claim(path))
            self._stream.append(_render_message(env))
            if env.type in (MessageType.TASK_RESULT, MessageType.TASK_ERROR):
                self._final = env

    # ---------- 画面 ----------

    def _state(self) -> RunState | None:
        for state in self._store.all():
            if state.root_thread == self._thread:
                return state
        return None

    def _render(self) -> Panel:
        state = self._state()
        elapsed = int((now() - self._started).total_seconds())
        header = Text.from_markup(
            f"[bold]{_clip(self._task, 80)}[/bold]\n"
            f"[dim]{elapsed}s · {'计划中…' if state is None else state.plan.goal}[/dim]"
        )
        body: list[ConsoleRenderable] = [header]
        if state is not None:
            body.append(_steps_table(state))
        if self._stream:
            body.append(Text.from_markup("\n".join(self._stream[-MAX_STREAM_LINES:])))
        return Panel(Group(*body), title="anthill run", border_style="cyan")

    def _print_outcome(self) -> None:
        if self._final is None:
            console.print("[yellow]![/yellow] 没等到最终结果；用 `anthill status` 继续查看")
            return
        payload = self._final.payload
        if isinstance(payload, TaskErrorPayload):
            console.print(f"\n[bold red]失败[/bold red] {payload.error}")
            raise typer.Exit(code=1)
        if not isinstance(payload, TaskResultPayload):
            return
        console.print(f"\n[bold green]完成[/bold green]（{payload.status}）\n{payload.summary}")
        if payload.artifacts:
            console.print("\n[dim]产物：[/dim]" + ", ".join(payload.artifacts))


def _steps_table(state: RunState) -> Table:
    table = Table(box=None, pad_edge=False, header_style="dim")
    for column in ("", "步骤", "执行者", "进展"):
        table.add_column(column)
    for record in state.steps:
        mark, style = STATE_STYLE[record.state]
        table.add_row(
            Text(mark, style=style),
            record.id,
            record.assignee,
            Text(_clip(record.summary or record.error or record.task, 60), style=style),
        )
    return table


def _render_message(env: Envelope) -> str:
    label = {
        MessageType.RECEIPT_ACCEPTED: "已受理",
        MessageType.TASK_RESULT: "结果",
        MessageType.TASK_ERROR: "失败",
    }.get(env.type, str(env.type))
    body = (
        getattr(env.payload, "summary", None)
        or getattr(env.payload, "body", None)
        or getattr(env.payload, "error", None)
        or ""
    )
    color = "red" if env.type is MessageType.TASK_ERROR else "cyan"
    return f"[{color}]←[/{color}] [{label}] [dim]{env.from_}[/dim] {_clip(body, 100)}"


def _clip(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"
