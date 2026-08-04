"""`anthill peers pair` —— 六位 PIN 码配对。

    A 机  anthill peers pair                       → 显示 PIN，开一个 2 分钟的窗口
    B 机  anthill peers pair --to A --pin 428193   → 一次往返换完，两边各自落库

比令牌流程好在两点：短到能念出来，而且**不需要一条已经信任的通道**来传密钥 ——
密钥是双方各自推导的，从没上过网线（原理见 `security/pairing.py`）。

`--to` 可以写节点名（从 discovery 见过的对端里查地址）或直接写地址。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import typer

from anthill.cli.common import console, fail, load
from anthill.core.config import Config
from anthill.core.errors import AntHillError, PeerError
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRegistry
from anthill.security.pair_client import join, resolve
from anthill.security.pairing import WINDOW_SECONDS, PairingStore, new_pin

POLL_INTERVAL = 0.5


def pair_command(
    to: str = typer.Option("", "--to", help="对方的节点名或地址；留空则本机开窗口等对方来连"),
    pin: str = typer.Option("", "--pin", help="对方屏幕上显示的六位 PIN"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """用六位 PIN 码配对，不用再复制那串长令牌。

    不带 --to 就是开窗口的一方（PIN 显示在本机屏幕上）；
    带 --to 和 --pin 就是去连对方的一方。
    """
    layout, config = load(workspace)
    try:
        peers = PeerRegistry(layout.root)
    except AntHillError as exc:
        fail(str(exc))

    if to and not pin:
        fail("--to 要配合 --pin 使用（PIN 在对方屏幕上）")
    if pin and not to:
        fail("有 PIN 就说明你是去连对方的那一方，请一起给出 --to")

    if to:
        asyncio.run(_join(config, peers, target=to, pin=pin))
    else:
        _host(layout, peers)


def _host(layout: NodeLayout, peers: PeerRegistry) -> None:
    """开窗口的一方：显示 PIN，然后等对方连进来。

    真正处理配对请求的是 `anthill serve` —— 窗口写在文件里跨进程共享，
    所以这里只负责开窗口，再盯着 peers.json 看结果。
    """
    store = PairingStore(layout.root)
    code = new_pin()
    store.open(code)

    console.print(f"\n  [bold cyan]{code[:3]} {code[3:]}[/bold cyan]\n")
    console.print(f"[dim]{int(WINDOW_SECONDS)} 秒内有效，只能用一次。[/dim]")
    console.print(f"对方执行：[bold]anthill peers pair --to <本机> --pin {code}[/bold]")
    console.print("[dim]（本机要有 anthill serve 在跑，配对请求由它接）[/dim]\n")

    before = {peer.node for peer in peers.all() if peer.trusted}
    waited = 0.0
    with console.status("等对方输入 PIN…"):
        while waited < WINDOW_SECONDS:
            fresh = [p for p in peers.all() if p.trusted and p.node not in before]
            if fresh:
                _report(fresh[0].node, fresh[0].fingerprint)
                return
            if store.current() is None:
                break  # 窗口没了：要么过期，要么对方 PIN 打错、已被作废
            time.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL

    store.close()
    fail("配对没完成：窗口过期，或对方 PIN 输错了（一个窗口只有一次机会）。重来一遍即可。")


async def _join(config: Config, peers: PeerRegistry, *, target: str, pin: str) -> None:
    """去连对方的一方：协议实现在 security/pair_client.py，CLI 和面板共用。"""
    try:
        base = resolve(peers, target)
        record = await join(
            base=base,
            my_node=config.node.name,
            my_endpoint=config.node.endpoint,
            pin=pin,
            peers=peers,
        )
    except PeerError as exc:
        fail(str(exc))
    _report(record.node, record.fingerprint)


def _report(node: str, print_: str) -> None:
    console.print(f"[bold green]✓[/bold green] 已和 {node} 配对")
    console.print(f"  指纹 [cyan]{print_}[/cyan] —— 和对方屏幕上的那串核对一致")
