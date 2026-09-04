"""`anthill state` —— 不经过模型的版本化状态发布与 replica 读取入口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import typer
from pydantic import JsonValue, ValidationError

from anthill.agent.sender import Sender
from anthill.cli.common import console, fail, load, read_body
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType, StateUpdatePayload
from anthill.core.router import Router, parse_address
from anthill.core.state_sync import StateRecord, StateStore
from anthill.core.states import DeliveryTracker
from anthill.transport.registry import TransportRegistry

state_app = typer.Typer(no_args_is_help=True, help="版本化状态：发布并读取 Agent 本地 replica")


def _replica(layout: NodeLayout, config: Config, agent_name: str) -> StateStore:
    if agent_name not in config.agents:
        fail(
            f"本节点没有 Agent {agent_name!r}；可读取 replica 的有："
            + ", ".join(sorted(config.agents))
        )
    root = layout.state_dir(agent_name)
    try:
        contained = root.resolve().is_relative_to(layout.workspace.resolve())
    except OSError as exc:
        fail(f"无法解析 Agent {agent_name!r} 的 replica 路径：{exc}")
    if not contained:
        fail(f"Agent {agent_name!r} 的 replica 路径离开了当前 workspace，拒绝读取")
    return StateStore(root)


def _record_metadata(record: StateRecord) -> dict[str, object]:
    return {
        "key": record.key,
        "source": record.source,
        "revision": record.revision,
        "digest": record.digest,
        "summary": record.summary,
    }


def _record_json(record: StateRecord) -> dict[str, object]:
    return {
        **_record_metadata(record),
        "snapshot": record.snapshot,
    }


@state_app.command("list")
def list_state(
    agent_name: str = typer.Option(..., "--agent", help="读取哪个已配置 Agent 的 replica"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """列出本 workspace 内一个 Agent 已校验的状态 replica。"""
    layout, config = load(workspace)
    try:
        records = _replica(layout, config, agent_name).list()
    except (AntHillError, OSError, ValueError) as exc:
        fail(str(exc))
    typer.echo(
        json.dumps(
            {
                "replica": {"workspace": str(layout.workspace), "agent": agent_name},
                "states": [_record_metadata(record) for record in records],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


@state_app.command("show")
def show_state(
    key: str = typer.Argument(..., help="状态 key，例如 project.board"),
    agent_name: str = typer.Option(..., "--agent", help="读取哪个已配置 Agent 的 replica"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """以 JSON 输出本 workspace 内一份已校验的状态 replica。"""
    layout, config = load(workspace)
    try:
        record = _replica(layout, config, agent_name).load(key)
    except (AntHillError, OSError, ValueError) as exc:
        fail(str(exc))
    if record is None:
        fail(f"Agent {agent_name!r} 的 replica 中没有 state key {key!r}")
    typer.echo(
        json.dumps(
            {
                "replica": {"workspace": str(layout.workspace), "agent": agent_name},
                "state": _record_json(record),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


@state_app.command("publish")
def publish_state(
    key: str = typer.Argument(..., help="状态 key，例如 project.board"),
    snapshot: str = typer.Argument(..., help="JSON object；`-` 读 stdin，`@文件` 读文件"),
    revision: int = typer.Option(..., "--revision", min=1, help="明确的单调 revision，不自动猜"),
    summary: str = typer.Option(..., "--summary", help="本版短摘要（最多 500 字）"),
    to: str = typer.Option("all", "--to", help="收件人，默认广播 all"),
    sender_name: str = typer.Option("cli", "--from", "-f", help="以哪个已配置 Agent 发布"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """发布一份完整 JSON 状态快照；发送和接收都不调用模型。"""
    layout, config = load(workspace)
    if sender_name not in config.agents:
        fail(
            f"本节点没有 Agent {sender_name!r}；可作为发布者的有："
            + ", ".join(sorted(config.agents))
        )
    raw = read_body(snapshot)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"snapshot 不是合法 JSON：{exc}")
    if not isinstance(parsed, dict):
        fail("snapshot 必须是 JSON object，不能是数组、字符串或标量")
    try:
        payload = StateUpdatePayload.from_snapshot(
            key=key,
            revision=revision,
            summary=summary,
            snapshot=cast(dict[str, JsonValue], parsed),
        )
        recipient = parse_address(to, default_node=config.node.name)
        asyncio.run(
            _publish(
                layout=layout,
                config=config,
                sender_name=sender_name,
                recipient=recipient,
                payload=payload,
            )
        )
    except (AntHillError, ValidationError, ValueError) as exc:
        fail(str(exc))


async def _publish(
    *,
    layout: NodeLayout,
    config: Config,
    sender_name: str,
    recipient: Address,
    payload: StateUpdatePayload,
) -> None:
    # 拆成异步 helper 是为了确保短命 CLI 的 TransportRegistry 明确关闭。
    identity = Address(node=config.node.name, agent=sender_name)
    mailbox = Mailbox(layout.mailbox_dir(sender_name)).ensure()
    log = EventLog(layout.log_file(sender_name), agent=sender_name, echo=False)
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
        recipient=recipient,
        type=MessageType.STATE_UPDATE,
        payload=payload,
    )
    try:
        results = await sender.send(env)
    finally:
        try:
            await transports.close()
        finally:
            log.close()

    for result in results:
        mark = "[green]→[/green]" if result.ok else "[red]✗[/red]"
        outcome = "已投递" if result.ok else "投递失败"
        console.print(f"{mark} {outcome} {result.destination} {payload.key}@{payload.revision}")
    if not any(result.ok for result in results):
        raise AntHillError("state.update 没有投递到任何收件人")
    console.print(
        f"[dim]state.update #{env.id[-6:]} source={identity} digest={payload.digest}[/dim]"
    )
    console.print("[dim]仅表示 transport 已投递；尚未确认 replica 已应用。[/dim]")
