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
from typing import Any

import typer
import uvicorn

from anthill.cli.common import console, fail, load
from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.discovery.beacon import Announcement, Beacon
from anthill.discovery.registry import PeerRegistry
from anthill.web.app import create_app
from anthill.web.cluster import write_status

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 45778
LOOPBACK = ("127.0.0.1", "localhost", "::1", "")
STATUS_INTERVAL = 5.0


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK


def serve_command(
    host: str = typer.Option(DEFAULT_HOST, "--host", help="监听地址；默认只绑本机回环"),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="监听端口"),
    advertise: str = typer.Option(
        "", "--advertise", help="广播给对端的地址，默认 http://<host>:<port>"
    ),
    panel: bool | None = typer.Option(
        None, "--panel/--no-panel", help="只读面板；默认只在绑回环时开启"
    ),
    panel_write: bool = typer.Option(
        False,
        "--panel-write",
        help="允许面板发起任务与改配置；只能配合回环地址使用",
    ),
    summary: bool = typer.Option(
        True,
        "--summary/--no-summary",
        help="是否把本机状态共享给已信任的对端（别人的总控面板要靠它）",
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
    # 面板默认只在回环上开：一旦 --host 0.0.0.0（为了让同网段投递进来），
    # 面板就会跟着暴露给整个网段。要那样必须显式 --panel，不给默认踩坑的机会。
    show_panel = is_loopback(host) if panel is None else panel
    if panel_write and not is_loopback(host):
        # 能改配置 ≈ 能在这台机器上执行命令。这种权限不该跟着 --host 0.0.0.0 一起对外
        log.close()
        fail(f"--panel-write 只能配合回环地址使用，当前 --host {host}")
    if panel_write and not show_panel:
        log.close()
        fail("--panel-write 需要面板是开着的；去掉 --no-panel")
    console.print(
        f"[bold green]▶[/bold green] {config.node.name} 接收端 [dim]{endpoint}[/dim]"
        + ("" if config.discovery.enabled else "  [dim]（discovery 未开启，不广播）[/dim]")
    )
    if host == DEFAULT_HOST:
        console.print("[dim]只绑回环；要让同网段的机器投递进来，用 --host 0.0.0.0[/dim]")
    if show_panel:
        mode = "可发起任务与改配置" if panel_write else "只读"
        console.print(f"[bold]面板[/bold] {endpoint}/panel [dim]（{mode}）[/dim]")
    elif panel is None:
        console.print("[dim]面板已关闭：绑的不是回环地址；确实要开就加 --panel[/dim]")
    if not summary:
        console.print("[dim]不共享状态：别人的总控面板会把本机显示成不可用[/dim]")

    try:
        asyncio.run(
            _serve(
                layout,
                config,
                peers,
                log,
                host=host,
                port=port,
                endpoint=endpoint,
                panel=show_panel,
                panel_write=panel_write,
                summary=summary,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[dim]已停止[/dim]")
    finally:
        log.close()


async def _serve(
    layout: NodeLayout,
    config: Config,
    peers: PeerRegistry,
    log: EventLog,
    *,
    host: str,
    port: int,
    endpoint: str,
    panel: bool = False,
    panel_write: bool = False,
    summary: bool = True,
) -> None:
    app = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=log,
        panel=panel,
        panel_writable=panel_write,
        summary=summary,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    )
    beacon = Beacon(
        settings=config.discovery,
        announcement=Announcement(
            node=config.node.name,
            endpoint=endpoint,
            agents=tuple(sorted(config.agents)),
        ),
        peers=peers,
        log=log,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    log.info("serve.start", node=config.node.name, endpoint=endpoint, host=host, port=port)
    tasks = [
        asyncio.create_task(server.serve(), name="http"),
        asyncio.create_task(beacon.run(stop), name="beacon"),
        asyncio.create_task(
            _status_loop(layout, config, peers, log, stop, enabled=summary), name="status"
        ),
        asyncio.create_task(stop.wait(), name="stop"),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        server.should_exit = True
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        _report_crashes(tasks, results, log)
        log.info("serve.stop")


def _report_crashes(tasks: list[asyncio.Task[Any]], results: list[Any], log: EventLog) -> None:
    """哪个后台任务是**炸掉**才停的，要说出来。

    `gather(return_exceptions=True)` 会把异常连同 traceback 一起吞掉 ——
    不补这一句的话，某个循环崩了会表现成「serve 正常退出，退出码 0」，
    盯着日志也看不出发生过什么。
    """
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            log.error(
                "serve.task_crashed",
                task=task.get_name(),
                error=f"{type(result).__name__}: {result}",
            )


async def _status_loop(
    layout: NodeLayout,
    config: Config,
    peers: PeerRegistry,
    log: EventLog,
    stop: asyncio.Event,
    *,
    interval: float = STATUS_INTERVAL,
    enabled: bool = True,
) -> None:
    """定期把本节点快照写成 `.anthill/status.json`，供总控面板来取。

    写文件而不是让对方实时算：总控可能同时连着七八台机器，
    让每台机器各自按自己的节奏写好放那儿，比来一次算一次省事也稳得多。

    **写不出来绝不能把 serve 带走**：磁盘满、`.anthill` 只读、peers.json 被手改坏，
    都只该让「别人看不到我的状态」，不该让这台机器停止收消息。
    所以这里接住所有异常 —— 少一份状态是小事，节点掉线是大事。
    """
    if not enabled:
        await stop.wait()
        return
    while not stop.is_set():
        try:
            write_status(layout, config, peers)
        except Exception as exc:
            log.warn("status.write_failed", error=f"{type(exc).__name__}: {exc}")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
