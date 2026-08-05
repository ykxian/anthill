"""`anthill peers list / invite / trust / forget`。

配对有两条路，都要人在两边各看一眼（TOFU 的核对点）：

    # 推荐：六位 PIN，密钥双方各自推导，不上网线（见 security/pairing.py）
    A 机  anthill peers pair
    B 机  anthill peers pair --to A --pin 428193

    # 老办法：令牌里含密钥明文，必须走一条你已经信任的通道传
    A 机  anthill peers invite laptop          → 打印一串配对令牌
    B 机  anthill peers trust --token <令牌>
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from anthill.cli.common import console, fail, load
from anthill.cli.pair_cmd import pair_command
from anthill.core.errors import AntHillError
from anthill.core.workspace import local_ip
from anthill.discovery.registry import PeerRecord, PeerRegistry
from anthill.security.keys import PairingToken, new_key

peers_app = typer.Typer(no_args_is_help=True, help="对端节点与信任关系")
peers_app.command("pair")(pair_command)

DEFAULT_HTTP_PORT = 45778


def _registry(workspace: Path | None) -> PeerRegistry:
    layout, _ = load(workspace)
    try:
        return PeerRegistry(layout.root)
    except AntHillError as exc:
        fail(str(exc))


@peers_app.command("list")
def list_peers(
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """列出已知节点。`discovered` 表示只是见过，还不能互投消息。"""
    registry = _registry(workspace)
    records = registry.all()
    if not records:
        console.print(
            "[dim]还没有任何对端。开启 discovery 可以被动发现，"
            "或直接用 `anthill peers invite` 配对。[/dim]"
        )
        return

    table = Table(title="对端节点", header_style="bold cyan")
    # 指纹存在的唯一目的就是和对方逐字符核对，被窄终端截成「f1:c2:…」就废了。
    # 项目在 peers invite 的令牌上专门加过 soft_wrap 并写了理由，同样的道理适用于这里。
    for column in ("节点", "状态", "端点", "Agent", "指纹", "最近可见"):
        # overflow="fold" 换行而不是截断 —— 指纹少一段就没法核对
        table.add_column(column, overflow="fold")
    for peer in records:
        style = "green" if peer.trusted else "yellow"
        table.add_row(
            peer.node,
            f"[{style}]{peer.status}[/{style}]",
            _endpoint_cell(peer),
            ", ".join(peer.agents) or "-",
            peer.fingerprint or "-",
            peer.last_seen[11:19] if peer.last_seen else "-",
        )
    console.print(table)
    if any(peer.endpoint_conflict for peer in records):
        console.print(
            "[yellow]有对端在广播里自称的地址和配对时那个对不上。[/yellow]\n"
            "投递仍然只走配对时那个地址（广播没有认证，不能让它改路由）。\n"
            "如果对方确实换了 IP，重新配对一次；否则就是有人在冒充。"
        )


def _endpoint_cell(peer: PeerRecord) -> str:
    """已信任的对端地址被广播「顶」过时，两个都显示 —— 这事得让人看见。"""
    if not peer.endpoint_conflict:
        return peer.endpoint or "-"
    return f"{peer.endpoint}\n[yellow]广播自称 {peer.seen_endpoint}[/yellow]"


@peers_app.command("invite")
def invite(
    node: str = typer.Argument(..., help="对方的节点名（他们 node.toml 里的 [node] name）"),
    endpoint: str = typer.Option("", "--endpoint", help="本机对外地址，默认 http://<本机IP>:45778"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """生成一次配对令牌，交给对方去 trust。同时把对方也加入本机信任列表。"""
    layout, config = load(workspace)
    registry = _registry(workspace)
    key = new_key()
    mine = endpoint or f"http://{local_ip()}:{DEFAULT_HTTP_PORT}"

    # 双方用同一把共享密钥（HMAC 是对称的），所以这边也要把对方记上
    registry.trust(PairingToken(node=node, endpoint="", key=key), replace=True)
    token = PairingToken(node=config.node.name, endpoint=mine, key=key)

    console.print(f"[bold]给 {node} 的配对令牌[/bold]（指纹 [cyan]{token.fingerprint}[/cyan]）：\n")
    # soft_wrap：不让 rich 按终端宽度插换行，否则用户复制过去的是断成几段的令牌
    console.print(token.encode(), soft_wrap=True, highlight=False)
    console.print(
        "\n[yellow]令牌里含共享密钥明文[/yellow]，只走你已经信任的通道传递"
        "（当面拷贝 / SSH / 私聊），别贴到公开群里。"
    )
    console.print("\n对方执行：[bold]anthill peers trust --token <上面这串>[/bold]")
    console.print(f"[dim]对方 trust 后，核对双方指纹是否都是 {token.fingerprint}[/dim]")
    console.print(f"[dim]本机 peers 已写入：{layout.root / 'peers.json'}（权限 0600）[/dim]")


@peers_app.command("trust")
def trust(
    token: str = typer.Option(..., "--token", help="对方 `peers invite` 打印的配对令牌"),
    replace: bool = typer.Option(
        False, "--replace", help="指纹与上次不一致时强制覆盖（确认对方确实重装了再用）"
    ),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """信任一个对端。指纹与首次信任时不一致会拒绝 —— 这是 TOFU 的告警点。"""
    registry = _registry(workspace)
    try:
        parsed = PairingToken.decode(token)
    except ValueError as exc:
        fail(str(exc))
    try:
        peer = registry.trust(parsed)
    except AntHillError as exc:
        fail(str(exc))

    console.print(
        f"[bold green]✓[/bold green] 已信任 [bold]{peer.node}[/bold]"
        f"（{peer.endpoint or '端点待发现'}）"
    )
    console.print(f"  指纹 [cyan]{peer.fingerprint}[/cyan] —— 请与对方屏幕上的那串核对一致")


@peers_app.command("forget")
def forget(
    node: str = typer.Argument(..., help="要移除的节点名"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """移除一个对端及其密钥。之后它发来的消息一律拒收。"""
    registry = _registry(workspace)
    if registry.forget(node):
        console.print(f"[bold green]✓[/bold green] 已移除 {node}（连同共享密钥）")
    else:
        console.print(f"[dim]peers 列表里本来就没有 {node}[/dim]")
