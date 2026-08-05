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
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import typer
import uvicorn

from anthill.cli.common import console, fail
from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.workspace import load_or_create as load_workspace
from anthill.core.workspace import local_ip
from anthill.discovery.beacon import Announcement, Beacon
from anthill.security import secrets
from anthill.security.panel_token import load_or_create, token_path
from anthill.transport.pull import pull_once, ssh_peers
from anthill.web.actions import RunRequest, start_run
from anthill.web.app import create_app
from anthill.web.cluster import write_status
from anthill.web.context import NodeContext, NodeRegistry
from anthill.web.workspaces import listing as known_workspaces

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 45778
LOOPBACK = ("127.0.0.1", "localhost", "::1", "")
WILDCARD_HOSTS = ("0.0.0.0", "::", "*")
STATUS_INTERVAL = 5.0
SHUTDOWN_GRACE = 5.0
"""Ctrl-C 之后留给 uvicorn 自己收尾的时间。"""
SCHEDULE_TICK = 10.0
"""定时任务循环多久醒一次。间隔本身由每条 schedule 的 `every` 决定。"""
IDLE_PULL_INTERVAL = 3600.0
"""所有节点都关了自动拉取时的空转节奏 —— 不退出循环，好让人改了配置之后不用重启。"""


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
    panel_token: bool = typer.Option(
        False,
        "--panel-token",
        help="给面板发一个访问令牌，好从别的机器上操作这台（没有显示器的机器需要）",
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

    **找不到工作区也不会报错**，但也不会擅自建：

    - 给了 `-w`，就是「用这个目录，没有就建」；
    - 没给 `-w`，它只在当前目录往上找。找不到就进「未配置」状态照常起面板，
      让你在页面上挑一个目录 —— 免得 `.anthill` 落在你没想要的地方（M11 改的）。

    这段话以前写的是「找不到就在当前目录建一个」，和同一个文件里
    `_load_or_setup` 的实现正好相反 —— 而这句才是用户在 `--help` 里看到的那句。
    """
    layout, config, created = _load_or_setup(workspace)
    nodes = _registry(layout, config, quiet=quiet)
    # 还没配工作区时没地方写日志，只回显
    log = EventLog(
        layout.log_file("serve") if layout else None,
        agent=f"serve:{config.node.name if config else '未配置'}",
        echo=not quiet,
    )

    busy = port_taken(host, port)
    if busy:
        log.close()
        fail(f"{busy}\n  多半是上一个 anthill serve 还开着；换个端口：--port {port + 1}")

    # 绑通配符是「监听哪儿」，不是「别人怎么找到我」—— 广播 0.0.0.0 出去，
    # 对端记下来之后永远连不上，报错还很难懂
    # 优先级：命令行 > node.toml 的 [node] endpoint > 自动猜。
    # 自动猜**一定会有猜错的时候**（多网卡、隧道、VPN），所以两条覆盖路都留着，
    # 并且把猜的结果打出来 —— 猜错的代价是「别人连不上你」，不该让人靠猜发现。
    configured = nodes.primary.config.node.endpoint.strip() if nodes.ready else ""
    guessed = local_ip() if host in WILDCARD_HOSTS else host
    endpoint = advertise or configured or f"http://{guessed}:{port}"
    picked_by = "--advertise" if advertise else ("node.toml" if configured else "自动识别")
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
        + (f" [dim]（{picked_by}）[/dim]" if picked_by != "--advertise" else "")
        + ("  [dim]（discovery 未开启，不广播）[/dim]" if quiet_discovery else "")
    )
    if host == DEFAULT_HOST:
        console.print("[dim]只绑回环；要让同网段的机器投递进来，用 --host 0.0.0.0[/dim]")
    elif picked_by == "自动识别":
        # 多网卡的机器上这一步**一定会有猜错的时候**（Docker 网桥、隧道、VPN），
        # 而猜错的表现是「别人连不上你」—— 一个很难往回追的症状。先摆出来。
        console.print(
            f"[dim]  这个地址是自动识别的（本机有多块网卡时可能挑错）。"
            f"不对就 --advertise http://<你的地址>:{port}，"
            # rich 把中括号当样式标记吃掉，得转义 —— 这个坑在 help 文本里踩过一次了
            r"或在 node.toml 的 \[node] 里写 endpoint[/dim]"
        )
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
    token = load_or_create() if panel_token else ""
    if token:
        console.print(
            f"\n[bold]面板令牌[/bold]  [cyan]{token}[/cyan]\n"
            f"[dim]  在别的机器上打开 {endpoint}/panel?token={token}\n"
            f"  令牌存在 {token_path()}（0600）；换一个就删掉它重启\n"
            "  它等价于「能在这台机器上执行命令」——"
            "局域网不可信时请改用 ssh -L 转发端口，别让它在明文里裸奔[/dim]\n"
        )
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
                nodes,
                log,
                host=host,
                port=port,
                endpoint=endpoint,
                panel=show_panel,
                panel_write=panel_write,
                summary=summary,
                remote_admin=remote_admin,
                panel_token=token,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[dim]已停止[/dim]")
    except AntHillError as exc:
        log.close()
        fail(f"{exc}\n  端口被占的话，换一个：--port 45779")
    finally:
        log.close()


def _registry(layout: NodeLayout | None, config: Config | None, *, quiet: bool) -> NodeRegistry:
    """本进程要照看哪些节点：命令行指的那个 + 清单里登记过的其它工作区。

    **一台机器一个 serve 就够了** —— 路由键 `to.node` 就在信封里，
    一个进程按名字分派到不同工作区的邮箱即可，不必一个工作区一个进程、一个端口。
    """
    nodes = NodeRegistry()
    if layout is not None:
        nodes.add(NodeContext(layout, config), primary=True)
    for entry in known_workspaces():
        path = Path(str(entry["path"]))
        if not entry["exists"] or (layout is not None and path == layout.workspace):
            continue
        try:
            nodes.attach(NodeLayout(path))
        except AntHillError as exc:
            # 清单里一条坏记录不该让整个 serve 起不来
            if not quiet:
                console.print(f"[yellow]![/yellow] 跳过工作区 {path}：{exc}")
    return nodes


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
            config, created = load_workspace(layout)
        except AntHillError as exc:
            fail(str(exc))
        return layout, config, created
    try:
        layout = NodeLayout.discover()
    except AntHillError:
        return None, None, False
    return layout, Config.load_from(layout), False


async def _serve(
    nodes: NodeRegistry,
    log: EventLog,
    *,
    host: str,
    port: int,
    endpoint: str,
    panel: bool = False,
    panel_write: bool = False,
    summary: bool = True,
    remote_admin: bool = False,
    panel_token: str = "",
) -> None:
    app = create_app(
        registry=nodes,
        log=log,
        panel=panel,
        panel_writable=panel_write,
        summary=summary,
        advertise=endpoint,
        remote_admin=remote_admin or any(c.config.security.remote_admin for c in nodes.all()),
        panel_token=panel_token,
    )
    server = _OwnedServer(
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
    # 照看几个节点就广播几个身份 —— 同网段的人该看见的是「节点」，不是「进程」
    beacons = [
        Beacon(
            settings=ctx.config.discovery,
            announcement=Announcement(
                node=ctx.name, endpoint=endpoint, agents=tuple(sorted(ctx.config.agents))
            ),
            peers=ctx.peers,
            log=log,
        )
        for ctx in nodes.all()
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    # 面板上设的密钥在这儿进环境 —— 之后它 spawn 的 agentd 会继承，
    # 于是「在面板上配好一个能干活的 Agent」不再需要先去终端 export
    injected = secrets.load_into_env()
    if injected:
        log.info("secrets.loaded", count=injected)
    log.info(
        "serve.start",
        node=nodes.primary_name,
        nodes="、".join(nodes.names()) or "（无）",
        endpoint=endpoint,
        host=host,
        port=port,
    )
    tasks = [
        asyncio.create_task(server.serve(), name="http"),
        *(asyncio.create_task(b.run(stop), name=f"beacon:{i}") for i, b in enumerate(beacons)),
        asyncio.create_task(_status_loop(nodes, log, stop, enabled=summary), name="status"),
        asyncio.create_task(_pull_loop(nodes, log, stop), name="pull"),
        asyncio.create_task(_schedule_loop(nodes, log, stop), name="schedule"),
        asyncio.create_task(stop.wait(), name="stop"),
    ]
    http = tasks[0]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        # **先让 uvicorn 自己收，再谈 cancel。** 直接 cancel 掉 serve()
        # 会打断它的 lifespan，于是 Ctrl-C 时终端上糊一大段
        # `asyncio.exceptions.CancelledError` 的 traceback —— 看着像崩了，其实是正常退出。
        server.should_exit = True
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(http), timeout=SHUTDOWN_GRACE)
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        crashed = _report_crashes(tasks, results, log)
        log.info("serve.stop")
    if crashed:
        # 起不来却退 0，脚本和 systemd 都会以为成功了 —— 端口被占是最常见的那种
        raise AntHillError("；".join(crashed))


class _OwnedServer(uvicorn.Server):
    """信号由**我们**接，uvicorn 不要自己抢。

    uvicorn 默认在 `serve()` 里换掉 SIGINT/SIGTERM 的处理器。那样 Ctrl-C 会同时
    触发两条关闭路径（它自己的，和我们 `stop` 事件那条），互相打断，结果是终端上
    糊一段 `asyncio.exceptions.CancelledError` 的 traceback —— 看着像崩了，
    其实只是正常退出被打断了 lifespan。

    这个进程还管着信标、状态、拉取、定时四个循环，本来就该由我们统一收尾。
    """

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


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
    nodes: NodeRegistry,
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
        for ctx in nodes.all():
            try:
                write_status(ctx.layout, ctx.config, ctx.peers)
            except Exception as exc:
                log.warn("status.write_failed", node=ctx.name, error=f"{type(exc).__name__}: {exc}")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return


async def _pull_loop(nodes: NodeRegistry, log: EventLog, stop: asyncio.Event) -> None:
    """定时替你去 SSH 对端取回信。

    SSH 是单向的（服务器连不回 NAT 后面的笔记本），所以回信只能靠这边拉。
    以前 `anthill pull` 是纯手工的一次性命令 —— **人不敲命令，对端的回信就永远不回来**，
    跨机协作的回程等于靠人肉驱动。

    **拉不动绝不能把 serve 带走**：对端关机、密钥换了、网络断了，
    都只该让「这一轮没拉到」，不该让这台机器停止收消息。所以这里接住所有异常。
    """
    while not stop.is_set():
        for ctx in nodes.all():
            interval = ctx.config.runtime.auto_pull_seconds
            if interval <= 0:
                continue
            for name, peer in ssh_peers(ctx.config):
                try:
                    report = await pull_once(ctx.layout, ctx.config, name, peer, log=log)
                except Exception as exc:
                    log.warn(
                        "pull.failed",
                        node=ctx.name,
                        peer=name,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                if report.count:
                    log.info("pull.done", node=ctx.name, peer=name, taken=report.count)
                for note in report.skipped:
                    log.warn("pull.skipped", node=ctx.name, peer=name, detail=note)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_pull_interval(nodes))
            return


def _pull_interval(nodes: NodeRegistry) -> float:
    """多个节点各配各的，取最小的那个当循环节奏；都关了就退化成长睡。"""
    intervals = [
        ctx.config.runtime.auto_pull_seconds
        for ctx in nodes.all()
        if ctx.config.runtime.auto_pull_seconds > 0
    ]
    return min(intervals) if intervals else IDLE_PULL_INTERVAL


async def _schedule_loop(nodes: NodeRegistry, log: EventLog, stop: asyncio.Event) -> None:
    """按 `[schedules.*]` 定时把任务交给 coordinator。

    没做 cron 表达式 —— 那是一门要解析、要测、要处理时区与夏令时的小语言，
    而「每隔多久」覆盖了绝大多数需要，用户写错的余地也小得多。

    **派不出去只记日志。** 定时任务失败不该让 serve 停止收消息 ——
    和状态循环、拉取循环同一条原则。
    """
    last: dict[str, float] = {}
    while not stop.is_set():
        moment = asyncio.get_running_loop().time()
        for ctx in nodes.all():
            for name, schedule in sorted(ctx.config.schedules.items()):
                if not schedule.enabled:
                    continue
                key = f"{ctx.name}/{name}"
                if moment - last.get(key, 0.0) < schedule.every:
                    continue
                last[key] = moment
                try:
                    await _fire(ctx, name, schedule, log)
                except Exception as exc:
                    log.warn(
                        "schedule.failed",
                        node=ctx.name,
                        schedule=name,
                        error=f"{type(exc).__name__}: {exc}",
                    )
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SCHEDULE_TICK)
            return


async def _fire(ctx: Any, name: str, schedule: Any, log: EventLog) -> None:
    """把一条定时任务投给 coordinator。走的是和面板「发起任务」同一条路。"""
    goal = schedule.task
    if schedule.template:
        goal = ctx.config.templates[schedule.template].goal.replace("{arg}", "")
    request = RunRequest(task=goal, to=schedule.to or "")
    result = await start_run(ctx.layout, ctx.config, request, log)
    log.info("schedule.fired", node=ctx.name, schedule=name, msg=result.get("id", ""))
