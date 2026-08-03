"""`anthill serve` —— 节点的对外接收端：LAN 投递端点 + 可选的组播信标。

**一个节点一个进程、一个端口**，而不是每个 Agent 一个 —— 收下来的信封直接
原子写进对应 Agent 的 inbox/new，剩下的交给那个 agentd 的 watcher。
这就是「一切皆邮箱」在网络这一侧的体现：HTTP 只是又一种把文件送进目录的方式。

默认只绑 127.0.0.1。要让同网段的机器投进来，必须显式 `--host 0.0.0.0` ——
「默认不对外」和「默认不广播」是同一个原则。
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from pathlib import Path

import typer
import uvicorn

from anthill.cli.common import console, fail, load
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.discovery.beacon import Announcement, Beacon
from anthill.discovery.registry import PeerRegistry
from anthill.web.app import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 45778


def serve_command(
    host: str = typer.Option(DEFAULT_HOST, "--host", help="监听地址；默认只绑本机回环"),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="监听端口"),
    advertise: str = typer.Option(
        "", "--advertise", help="广播给对端的地址，默认 http://<host>:<port>"
    ),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="只写日志文件，不在终端回显"),
) -> None:
    """启动本节点的接收端。Ctrl-C 优雅退出。"""
    layout, config = load(workspace)
    log = EventLog(layout.log_file("serve"), agent=f"serve:{config.node.name}", echo=not quiet)
    try:
        peers = PeerRegistry(layout.root)
    except AntHillError as exc:
        log.close()
        fail(str(exc))

    endpoint = advertise or f"http://{host}:{port}"
    console.print(
        f"[bold green]▶[/bold green] {config.node.name} 接收端 [dim]{endpoint}[/dim]"
        + ("" if config.discovery.enabled else "  [dim]（discovery 未开启，不广播）[/dim]")
    )
    if host == DEFAULT_HOST:
        console.print("[dim]只绑回环；要让同网段的机器投递进来，用 --host 0.0.0.0[/dim]")

    try:
        asyncio.run(_serve(layout, config, peers, log, host=host, port=port, endpoint=endpoint))
    except KeyboardInterrupt:
        console.print("\n[dim]已停止[/dim]")
    finally:
        log.close()


async def _serve(
    layout: object,
    config: object,
    peers: PeerRegistry,
    log: EventLog,
    *,
    host: str,
    port: int,
    endpoint: str,
) -> None:
    app = create_app(layout=layout, config=config, peers=peers, log=log)  # type: ignore[arg-type]
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    )
    beacon = Beacon(
        settings=config.discovery,  # type: ignore[attr-defined]
        announcement=Announcement(
            node=config.node.name,  # type: ignore[attr-defined]
            endpoint=endpoint,
            agents=tuple(sorted(config.agents)),  # type: ignore[attr-defined]
        ),
        peers=peers,
        log=log,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    log.info("serve.start", node=config.node.name, endpoint=endpoint, host=host, port=port)  # type: ignore[attr-defined]
    tasks = [
        asyncio.create_task(server.serve(), name="http"),
        asyncio.create_task(beacon.run(stop), name="beacon"),
        asyncio.create_task(stop.wait(), name="stop"),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        server.should_exit = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("serve.stop")
