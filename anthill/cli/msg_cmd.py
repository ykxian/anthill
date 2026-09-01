"""`anthill send` —— 从命令行往某个 Agent 的邮箱投一条消息。

CLI 自己也有一个邮箱（默认 Agent 名 `cli`），所以回执与结果能原路回来，
`--wait` 就是在自己的 inbox 上等着收。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import typer

from anthill.agent.sender import Sender
from anthill.cli.common import console, fail, load, read_body
from anthill.core.chat_log import record_outgoing
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import AntHillError
from anthill.core.ids import new_thread_id, now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType, Payload, TaskRequestPayload
from anthill.core.router import Router, extract_mentions
from anthill.core.states import DeliveryTracker
from anthill.transport.registry import TransportRegistry

DEFAULT_CLI_AGENT = "cli"
WAIT_POLL_INTERVAL = 0.3
TERMINAL_TYPES = frozenset({MessageType.TASK_RESULT, MessageType.TASK_ERROR})


def send_command(
    to: str = typer.Argument(..., help="收件人：agent / role:xxx / node:agent"),
    text: str = typer.Argument(..., help="消息正文；`-` 读 stdin，`@文件` 读文件"),
    title: str = typer.Option("", "--title", "-t", help="任务标题，默认取正文前 60 字"),
    msg_type: str = typer.Option("task.request", "--type", help="task.request | chat"),
    sender_name: str = typer.Option(DEFAULT_CLI_AGENT, "--from", "-f", help="以哪个 Agent 身份发"),
    thread: str = typer.Option("", "--thread", help="挂到已有线程；默认新开一个"),
    wait: float = typer.Option(0.0, "--wait", help="等待回执与结果的秒数，0 表示不等"),
    # -w 在所有命令里统一是 --workspace，别让 --wait 抢走
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """投递一条消息。"""
    text = read_body(text)
    layout, config = load(workspace)
    try:
        kind = MessageType(msg_type)
    except ValueError:
        fail(f"不支持的消息类型 {msg_type!r}；CLI 目前支持 task.request 与 chat")
    if kind not in {MessageType.TASK_REQUEST, MessageType.CHAT}:
        fail(f"{msg_type} 不该由人手动发；CLI 支持 task.request 与 chat")

    try:
        asyncio.run(
            _send(
                layout=layout,
                config=config,
                to=to,
                text=text,
                title=title,
                kind=kind,
                sender_name=sender_name,
                thread=thread or new_thread_id(),
                wait=wait,
            )
        )
    except AntHillError as exc:
        fail(str(exc))


async def _send(
    *,
    layout: NodeLayout,
    config: Config,
    to: str,
    text: str,
    title: str,
    kind: MessageType,
    sender_name: str,
    thread: str,
    wait: float,
) -> None:
    identity = Address(node=config.node.name, agent=sender_name)
    mailbox = Mailbox(layout.mailbox_dir(sender_name)).ensure()
    log = EventLog(layout.log_file(sender_name), agent=sender_name, echo=False)
    # `send` 是短命命令；本地小文件投递不进默认线程池，避免受限 sandbox
    # 在 asyncio.run() 回收 executor 时永久挂住。常驻 agentd 仍使用线程投递。
    transports = TransportRegistry(config, layout, threaded_local=False)
    sender = Sender(
        identity=identity,
        mailbox=mailbox,
        router=Router(config, layout),
        transports=transports,
        tracker=DeliveryTracker(),
        log=log,
    )

    env = Envelope.new(
        sender=identity,
        recipient=_parse_address(to, config.node.name),
        type=kind,
        payload=_build_payload(kind, title, text),
        thread=thread,
    )
    try:
        results = await sender.send(env)
    finally:
        # CLI 是短命进程，不能把 httpx/ssh client 和 asyncio executor 留给
        # asyncio.run() 猜着回收；现场因此积过数十个永不退出的 `anthill send`。
        try:
            await transports.close()
        finally:
            log.close()
    if any(result.ok for result in results):
        # 面板发件会记进 chats（record_outgoing），CLI 不记的话，对话页上
        # 只见对方的回音、不见你发出去的那半句 —— 跨机联调时看着像消息丢了
        record_outgoing(layout, env, text)

    for result in results:
        if result.ok:
            console.print(
                f"[bold green]→[/bold green] {result.destination} "
                f"[dim]{env.type} #{env.id[-6:]} thread={thread[-6:]}[/dim]"
            )
        else:
            console.print(f"[bold red]✗[/bold red] {result.destination}：{result.detail}")

    if wait > 0 and not await _wait_for_replies(mailbox, thread=thread, timeout=wait):
        raise typer.Exit(code=2)  # 超时 / 对方报错：退出码要能和成功区分开


def _parse_address(raw: str, local_node: str) -> Address:
    if raw.startswith("role:") or ":" not in raw:
        return Address(node=local_node, agent=raw)
    node, _, agent = raw.partition(":")
    return Address(node=node, agent=agent)


def _build_payload(kind: MessageType, title: str, text: str) -> Payload:
    if kind is MessageType.CHAT:
        return ChatPayload(body=text, mentions=extract_mentions(text))
    return TaskRequestPayload(title=title or text[:60], body=text)


async def _wait_for_replies(mailbox: Mailbox, *, thread: str, timeout: float) -> bool:
    """在自己的 inbox 上等回执/结果。收到 result 或 error 即结束。

    返回「等到了没有」—— 调用方据此决定退出码。超时和成功都返回 0 的话，
    脚本没有任何办法区分这两件事（发给不存在的 Agent 倒是能返回 1）。
    """
    deadline = now() + timedelta(seconds=timeout)
    console.print(f"[dim]等待回执与结果（≤{timeout:g}s）…[/dim]")

    while now() < deadline:
        for path in mailbox.list_new():
            try:
                env = Mailbox.read_envelope(path)
            except AntHillError:
                continue
            if env.thread != thread:
                continue
            mailbox.archive(mailbox.claim(path))
            console.print(_render(env))
            if env.type in TERMINAL_TYPES:
                return env.type is not MessageType.TASK_ERROR
        await asyncio.sleep(WAIT_POLL_INTERVAL)

    console.print("[yellow]![/yellow] 等待超时；消息可能仍在处理，用 `anthill log --follow` 继续看")
    return False


TYPE_LABEL = {
    MessageType.RECEIPT_ACCEPTED: "已受理",
    MessageType.RECEIPT_REJECTED: "被拒绝",
    MessageType.RECEIPT_EXPIRED: "已过期",
    MessageType.RECEIPT_DELIVERED: "已送达",
    MessageType.TASK_RESULT: "结果",
    MessageType.TASK_ERROR: "失败",
    MessageType.CHAT: "消息",
}


def _render(env: Envelope) -> str:
    body = (
        getattr(env.payload, "summary", None)
        or getattr(env.payload, "body", None)
        or getattr(env.payload, "error", None)
        or getattr(env.payload, "reason", None)
        or ""
    )
    color = "red" if env.type is MessageType.TASK_ERROR else "cyan"
    label = TYPE_LABEL.get(env.type, str(env.type))
    return f"[bold {color}]←[/bold {color}] [{label}] [dim]{env.from_}[/dim] {body}"
