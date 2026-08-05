"""`anthill approve` 与 `anthill fetch` —— 跨机场景里「人」的那两个动作。

- **approve**：远端 agentd 遇到危险操作会停下来等人点头。你在本机批，
  答复经 SFTP 写回远端。服务器上不需要有人，也不需要开任何端口。
- **fetch**：远端 result 里只带产物路径（消息体上限 64KB），要看内容就按需拉。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import asyncssh
import typer
from rich.prompt import Confirm
from rich.table import Table

from anthill.cli.common import console, fail, load
from anthill.core.config import Config, PeerSection
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.security.approvals import (
    ANSWER_SUFFIX,
    APPROVALS_DIR,
    ApprovalRequest,
    ApprovalStore,
)
from anthill.transport.pull import pull_once
from anthill.transport.ssh import SshTransport

REMOTE_ARTIFACTS_DIR = "remote"


def _ssh_peer(config: Config, node: str) -> PeerSection:
    peer = config.peers.get(node)
    if peer is None:
        known = ", ".join(sorted(config.peers)) or "（node.toml 里没有配置任何 peer）"
        fail(f"未知 peer {node!r}；已配置：{known}")
    if peer.transport.value != "ssh":
        fail(f"peer {node!r} 的 transport 是 {peer.transport}，这个命令只支持 ssh")
    return peer


def _transport(config: Config) -> SshTransport:
    return SshTransport(
        node_name=config.node.name, log=EventLog(None, agent=config.node.name, echo=False)
    )


# ---------- approve ----------


def approve_command(
    peer: str = typer.Option("", "--peer", help="远端节点名；留空表示批本机的"),
    request_id: str = typer.Option("", "--id", help="只处理这一条；留空表示逐条问"),
    yes: bool = typer.Option(False, "--yes", "-y", help="全部批准，不逐条问"),
    no: bool = typer.Option(False, "--no", "-n", help="全部拒绝"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """处理待审批的危险操作。远端的 agentd 正停在那里等这个答复。"""
    layout, config = load(workspace)
    if yes and no:
        fail("--yes 与 --no 不能同时用")
    try:
        if peer:
            asyncio.run(_approve_remote(config, peer, request_id, yes, no))
        else:
            _approve_local(layout, request_id, yes, no)
    except AntHillError as exc:
        fail(str(exc))


def _decide(request: ApprovalRequest, yes: bool, no: bool) -> bool | None:
    """返回 None 表示这条先跳过。"""
    if yes:
        return True
    if no:
        return False
    console.print(f"\n[bold yellow]待审批[/bold yellow] [dim]{request.id[-6:]}[/dim]")
    console.print(f"[dim]提出者[/dim] {request.agent}  [dim]时间[/dim] {request.created_at[11:19]}")
    console.print(request.prompt)
    return bool(Confirm.ask("批准？", default=False))


def _approve_local(layout: NodeLayout, request_id: str, yes: bool, no: bool) -> None:
    store = ApprovalStore(layout.root)
    pending = [r for r in store.pending() if not request_id or r.id == request_id]
    if not pending:
        console.print("[dim]没有待审批的操作[/dim]")
        return
    for request in pending:
        decision = _decide(request, yes, no)
        if decision is None:
            continue
        store.answer(request.id, approved=decision, by="local")
        _report(request, decision)


async def _approve_remote(config: Config, node: str, request_id: str, yes: bool, no: bool) -> None:
    peer = _ssh_peer(config, node)
    transport = _transport(config)
    remote_dir = f".anthill/{APPROVALS_DIR}"
    try:
        try:
            names = await transport.listdir(node, peer, remote_dir)
        except asyncssh.SFTPError:
            console.print(f"[dim]{node} 上没有待审批的操作[/dim]")
            return

        requests = await _load_remote(transport, node, peer, remote_dir, names, request_id)
        if not requests:
            console.print(f"[dim]{node} 上没有待审批的操作[/dim]")
            return
        for request in requests:
            decision = _decide(request, yes, no)
            if decision is None:
                continue
            payload = json.dumps(
                {"id": request.id, "approved": decision, "by": config.node.name},
                ensure_ascii=False,
            ).encode()
            await transport.write_bytes(
                node, peer, f"{remote_dir}/{request.id}{ANSWER_SUFFIX}", payload
            )
            _report(request, decision)
    finally:
        await transport.close()


async def _load_remote(
    transport: SshTransport,
    node: str,
    peer: PeerSection,
    remote_dir: str,
    names: list[str],
    request_id: str,
) -> list[ApprovalRequest]:
    answered = {n[: -len(ANSWER_SUFFIX)] for n in names if n.endswith(ANSWER_SUFFIX)}
    out: list[ApprovalRequest] = []
    for name in names:
        if not name.endswith(".json") or name.endswith(ANSWER_SUFFIX):
            continue
        ident = name[: -len(".json")]
        if ident in answered or (request_id and ident != request_id):
            continue
        try:
            raw = await transport.read_bytes(node, peer, f"{remote_dir}/{name}")
            out.append(ApprovalRequest.model_validate(json.loads(raw)))
        except (asyncssh.SFTPError, json.JSONDecodeError, ValueError):
            continue  # 半写状态或坏文件：跳过，下次再看
    return out


def _report(request: ApprovalRequest, approved: bool) -> None:
    mark = "[bold green]✓ 已批准[/bold green]" if approved else "[bold red]✗ 已拒绝[/bold red]"
    console.print(f"{mark} {request.id[-6:]}（{request.agent}）")


# ---------- fetch ----------


def fetch_command(
    node: str = typer.Argument(..., help="远端节点名（node.toml 里的 peer 名）"),
    paths: list[str] = typer.Argument(..., help="远端路径，相对 remote_workspace"),
    into: Path | None = typer.Option(
        None, "--into", help="落地目录，默认 blackboard/remote/<节点>"
    ),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """按需把远端产物拉回本机。"""
    layout, config = load(workspace)
    peer = _ssh_peer(config, node)
    target = into or (layout.blackboard / REMOTE_ARTIFACTS_DIR / node)
    try:
        asyncio.run(_fetch(config, node, peer, paths, target))
    except (AntHillError, OSError) as exc:
        fail(str(exc))


async def _fetch(
    config: Config, node: str, peer: PeerSection, paths: list[str], target: Path
) -> None:
    transport = _transport(config)
    try:
        for remote in paths:
            local = target / Path(remote).name
            size = await transport.fetch(node, peer, remote, local)
            console.print(f"[bold green]←[/bold green] {remote} [dim]{size} 字节[/dim] → {local}")
    finally:
        await transport.close()


def render_pending(requests: list[ApprovalRequest]) -> Any:
    table = Table(title="待审批", header_style="bold yellow")
    for column in ("id", "提出者", "操作", "时间"):
        table.add_column(column)
    for request in requests:
        table.add_row(
            request.id[-6:],
            request.agent,
            " ".join(request.prompt.split())[:60],
            request.created_at[11:19],
        )
    return table


# ---------- pull ----------


def pull_command(
    node: str = typer.Argument(..., help="要拉取的远端节点名"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """把远端替我们暂存的回信取回本机邮箱。

    SSH 是单向的：服务器连不回你的笔记本（NAT 后面、也没跑 sshd），
    所以它把回信暂存在 `.anthill/spool/<你的节点>/`，由你来拉 —— 和 `git pull` 一个道理。
    """
    layout, config = load(workspace)
    peer = _ssh_peer(config, node)
    try:
        count = asyncio.run(_pull(layout, config, node, peer))
    except (AntHillError, OSError) as exc:
        fail(str(exc))
    console.print(
        f"[bold green]←[/bold green] 从 {node} 取回 {count} 条"
        if count
        else f"[dim]{node} 上没有待取的回信[/dim]"
    )


async def _pull(layout: NodeLayout, config: Config, node: str, peer: PeerSection) -> int:
    """拉一轮并把结果打给人看。真正的拉取逻辑在 transport/pull.py ——
    `serve` 的自动拉取用的是同一份，免得两条路各写一遍、再各错一次。"""
    report = await pull_once(layout, config, node, peer)
    for note in report.skipped:
        console.print(f"[yellow]![/yellow] {note}")
    for line in report.taken:
        console.print(f"  [dim]{line}[/dim]")
    return report.count
