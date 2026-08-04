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
import socket
from contextlib import suppress
from pathlib import Path
from typing import Any

import typer
import uvicorn

from anthill.cli.common import console, fail
from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.workspace import load_or_create
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


def port_taken(host: str, port: int) -> str:
    """开跑之前先探一下端口，占了就给一句人话。

    不探的话，uvicorn 会自己 `sys.exit(3)` 并甩一行 `[Errno 98]` ——
    退出码和其他命令对不上，也不会告诉人「换个 --port 就行」。
    而「上一个 serve 还开着」恰恰是最常见的一种启动失败。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
    except OSError as exc:
        return f"{host}:{port} 绑不上（{exc.strerror or exc}）"
    finally:
        probe.close()
    return ""


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
    remote_admin: bool = typer.Option(
        False,
        "--remote-admin",
        help="允许已信任的对端在它的面板上直接改本机 node.toml（≈ 让它能在本机执行命令）",
    ),
    summary: bool = typer.Option(
        True,
        "--summary/--no-summary",
        help="是否把本机状态共享给已信任的对端（别人的总控面板要靠它）",
    ),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="只写日志文件，不在终端回显"),
) -> None:
    """启动本节点的接收端。Ctrl-C 优雅退出。

    **找不到工作区不会报错**：它会在当前目录（或 `-w` 指的目录）建一个然后继续 ——
    新机器上装好就能直接开面板，不必先在终端跑一次 `anthill init`。
    建在哪、叫什么名字都会打印出来。
    """
    layout, config, created = _load_or_setup(workspace)
    # 还没配工作区时没地方写日志，只回显
    log = EventLog(
        layout.log_file("serve") if layout else None,
        agent=f"serve:{config.node.name if config else '未配置'}",
        echo=not quiet,
    )
    peers = None
    if layout is not None:
        try:
            peers = PeerRegistry(layout.root)
        except AntHillError as exc:
            log.close()
            fail(str(exc))

    busy = port_taken(host, port)
    if busy:
        log.close()
        fail(f"{busy}\n  多半是上一个 anthill serve 还开着；换个端口：--port {port + 1}")

    endpoint = advertise or f"http://{host}:{port}"
    # 面板默认只在回环上开：一旦 --host 0.0.0.0（为了让同网段投递进来），
    # 面板就会跟着暴露给整个网段。要那样必须显式 --panel，不给默认踩坑的机会。
    # --panel-write 本身就是个比 --panel 更明确的表态，不必再让人多写一个 --panel
    show_panel = (is_loopback(host) or panel_write) if panel is None else panel
    if panel_write and not show_panel:
        log.close()
        fail("--panel-write 需要面板是开着的；去掉 --no-panel")
    if created and layout is not None and config is not None:
        console.print(
            f"[bold green]✓[/bold green] 没找到工作区，已按 -w 建了一个：[b]{layout.root}[/b]\n"
            f"[dim]  节点名 {config.node.name}（取自主机名）· "
            f"Agent {', '.join(config.agents)} · 之后可以在面板上加[/dim]"
        )
    elif layout is None:
        console.print(
            "[yellow]![/yellow] 这里还没有工作区 —— [b]先不建[/b]，"
            "免得 .anthill 落在你没想要的目录里。\n"
            "[dim]  打开面板挑一个目录（或者 anthill init 显式指定）[/dim]"
        )
    name = config.node.name if config else "（未配置）"
    quiet_discovery = config is not None and not config.discovery.enabled
    console.print(
        f"[bold green]▶[/bold green] {name} 接收端 [dim]{endpoint}[/dim]"
        + ("  [dim]（discovery 未开启，不广播）[/dim]" if quiet_discovery else "")
    )
    if host == DEFAULT_HOST:
        console.print("[dim]只绑回环；要让同网段的机器投递进来，用 --host 0.0.0.0[/dim]")
    if show_panel:
        mode = "可发起任务与改配置" if panel_write else "只读"
        console.print(f"[bold]面板[/bold] {endpoint}/panel [dim]（{mode}）[/dim]")
        if panel_write and not is_loopback(host):
            # 绑对外是跨机投递的需要，写权限仍然只对本机开放 —— 说出来，别让人猜
            console.print(
                "[dim]      写操作只对本机开放：网段上的人访问这个地址会收到 403，"
                "只有你在这台机器上开浏览器才通得过[/dim]"
            )
    elif panel is None:
        console.print("[dim]面板已关闭：绑的不是回环地址；确实要开就加 --panel[/dim]")
    if not summary:
        console.print("[dim]不共享状态：别人的总控面板会把本机显示成不可用[/dim]")
    if remote_admin or (config is not None and config.security.remote_admin):
        console.print(
            "[yellow]远端管理已开[/yellow]：已信任的对端可以直接改本机 node.toml "
            "[dim]—— 这约等于让它能在本机执行命令[/dim]"
        )

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
                remote_admin=remote_admin,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[dim]已停止[/dim]")
    except AntHillError as exc:
        log.close()
        fail(f"{exc}\n  端口被占的话，换一个：--port 45779")
    finally:
        log.close()


def _load_or_setup(workspace: Path | None) -> tuple[NodeLayout | None, Config | None, bool]:
    """有工作区就用；没有就**什么都不建**，返回 None 进「未配置」状态。

    上一版会就地建一个。装好就能用是对的，但「就地」这个决定不该由程序替人做 ——
    你可能只是 cd 到了别处，结果 `.anthill` 撒在一个根本不想要的目录里。
    现在改成：面板给个目录浏览器，你挑好地方再建。在那之前磁盘一个字都不写。

    显式给了 `-w` 就用那个目录（那是明确的意思表示，没有就建）。
    """
    if workspace is not None:
        layout = NodeLayout(workspace.resolve())
        try:
            config, created = load_or_create(layout)
        except AntHillError as exc:
            fail(str(exc))
        return layout, config, created
    try:
        layout = NodeLayout.discover()
    except AntHillError:
        return None, None, False
    return layout, Config.load_from(layout), False


async def _serve(
    layout: NodeLayout | None,
    config: Config | None,
    peers: PeerRegistry | None,
    log: EventLog,
    *,
    host: str,
    port: int,
    endpoint: str,
    panel: bool = False,
    panel_write: bool = False,
    summary: bool = True,
    remote_admin: bool = False,
) -> None:
    app = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=log,
        panel=panel,
        panel_writable=panel_write,
        summary=summary,
        advertise=endpoint,
        remote_admin=remote_admin or (config is not None and config.security.remote_admin),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            # 面板的写权限是按「连接来自本机」判的。proxy_headers 会让
            # X-Forwarded-For 有机会改写那个值 —— 我们不在反向代理后面跑，
            # 就别给这条路留口子。默认值恰好是安全的，但那是巧合，不该靠。
            proxy_headers=False,
        )
    )
    # 没配工作区就没有身份可广播、也没有状态可写 —— 那两条循环空转即可
    beacon = (
        Beacon(
            settings=config.discovery,
            announcement=Announcement(
                node=config.node.name, endpoint=endpoint, agents=tuple(sorted(config.agents))
            ),
            peers=peers,
            log=log,
        )
        if config is not None and peers is not None
        else None
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    log.info(
        "serve.start",
        node=config.node.name if config else "未配置",
        endpoint=endpoint,
        host=host,
        port=port,
    )
    tasks = [
        asyncio.create_task(server.serve(), name="http"),
        asyncio.create_task(beacon.run(stop) if beacon is not None else stop.wait(), name="beacon"),
        asyncio.create_task(
            _status_loop(layout, config, peers, log, stop, enabled=summary),
            name="status",
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
        crashed = _report_crashes(tasks, results, log)
        log.info("serve.stop")
    if crashed:
        # 起不来却退 0，脚本和 systemd 都会以为成功了 —— 端口被占是最常见的那种
        raise AntHillError("；".join(crashed))


def _report_crashes(tasks: list[asyncio.Task[Any]], results: list[Any], log: EventLog) -> list[str]:
    """哪个后台任务是**炸掉**才停的，要说出来，并且要让退出码反映出来。

    `gather(return_exceptions=True)` 会把异常连同 traceback 一起吞掉 ——
    不补这一句的话，某个循环崩了会表现成「serve 正常退出，退出码 0」，
    盯着日志也看不出发生过什么。端口被占就是最常见的那一种。
    """
    crashed = []
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            detail = f"{task.get_name()} 任务异常退出：{type(result).__name__}: {result}"
            log.error(
                "serve.task_crashed",
                task=task.get_name(),
                error=f"{type(result).__name__}: {result}",
            )
            crashed.append(detail)
    return crashed


async def _status_loop(
    layout: NodeLayout | None,
    config: Config | None,
    peers: PeerRegistry | None,
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
    if not enabled or layout is None or config is None or peers is None:
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
