"""``anthill codex``：接入现有 Codex，或启动同 thread 的桥接 TUI。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from contextlib import suppress
from pathlib import Path

import typer

from anthill.adapters.bridge_connect import codex_session_instructions
from anthill.adapters.bridge_session import (
    claim,
    pick_agent,
    release,
)
from anthill.adapters.codex_app_server import (
    CodexAppServerError,
    CodexInboxBridge,
    CodexQueueBridge,
    CodexRpcClient,
    CodexRpcError,
    create_or_resume_thread,
    start_app_server,
    write_session,
)
from anthill.cli.common import console, fail, load
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.security.secrets import sanitized_child_env


def codex_command(
    agent: str = typer.Argument(
        "", help="桥接 Agent 名；留空 = 用 $ANTHILL_AGENT，再没有就自动认领"
    ),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    resume: str = typer.Option("", "--resume", help="恢复已有 Codex thread ID"),
    attach: str = typer.Option(
        "",
        "--attach",
        help="接入一个正在运行的 Codex thread；用原生 queue 唤醒现有前台",
    ),
    model: str = typer.Option("", "--model", "-m", help="传给 Codex TUI 的模型"),
    profile: str = typer.Option("", "--profile", "-p", help="传给 Codex TUI 的配置 profile"),
    sandbox: str = typer.Option(
        "",
        "--sandbox",
        "-s",
        help="Codex sandbox：read-only / workspace-write / danger-full-access",
    ),
    approval: str = typer.Option(
        "", "--ask-for-approval", "-a", help="Codex 审批策略：on-request / never"
    ),
    approve_for_me: bool = typer.Option(
        False, "--approve-for-me", help="让 Codex 自动复核需审批的操作"
    ),
    yolo: bool = typer.Option(
        False,
        "--yolo",
        help="不推荐：跳过全部审批并关闭沙箱（等同 Codex 的危险绕过选项）",
    ),
    search: bool = typer.Option(False, "--search", help="开启 Codex Web 搜索"),
    no_alt_screen: bool = typer.Option(
        False, "--no-alt-screen", help="不使用终端 alternate screen，保留滚动记录"
    ),
) -> None:
    """Codex 交互会话接入 AntHill：正常对话，来信时自动响应。

    ``--attach`` 通过原生 ``codex queue`` 接入已有前台；否则启动一个只监听
    127.0.0.1 的私有 app-server 和 TUI。``--resume`` 遇到 active writer 时
    也会自动转为接入，不会再启动第二个 writer。``--attach current`` 可显式
    使用当前环境的 $CODEX_THREAD_ID。
    """
    layout, config = load(workspace)
    try:
        picked = pick_agent(layout, config, agent)
        section = config.agent(picked)
        if not section.bridge:
            raise AntHillError(f"Agent {picked!r} 不是桥接 Agent；node.toml 里要写 bridge = true")
        claim(layout, picked)
        sensitive_env = frozenset(provider.api_key_env for provider in config.providers.values())
        args = _tui_options(
            model=model,
            profile=profile,
            sandbox=sandbox,
            approval=approval,
            approve_for_me=approve_for_me,
            yolo=yolo,
            search=search,
            no_alt_screen=no_alt_screen,
        )
        if attach and resume:
            raise AntHillError("--attach 和 --resume 不能同时使用")
        current_thread = os.environ.get("CODEX_THREAD_ID", "").strip()
        attach_thread = current_thread if attach == "current" else attach.strip()
        if attach == "current" and not attach_thread:
            raise AntHillError("当前环境没有 $CODEX_THREAD_ID；请给 --attach 指定 thread ID")
        if attach_thread:
            _reject_attach_tui_options(args)
            code = asyncio.run(
                run_codex_queue_session(
                    layout=layout,
                    agent=picked,
                    thread_id=attach_thread,
                    sensitive_env=sensitive_env,
                )
            )
        else:
            try:
                code = asyncio.run(
                    run_codex_session(
                        layout=layout,
                        node=config.node.name,
                        agent=picked,
                        resume=resume,
                        tui_options=args,
                        sensitive_env=sensitive_env,
                    )
                )
            except CodexRpcError as exc:
                if not resume or not is_active_writer_error(exc):
                    raise
                _reject_attach_tui_options(args)
                console.print(
                    "[yellow]该 thread 已在另一个 Codex 前台运行；"
                    "改用原生 queue 接入现有 writer。[/yellow]"
                )
                code = asyncio.run(
                    run_codex_queue_session(
                        layout=layout,
                        agent=picked,
                        thread_id=resume,
                        sensitive_env=sensitive_env,
                    )
                )
    except KeyboardInterrupt:
        console.print("\n[dim]已停止 Codex 桥接[/dim]")
        return
    except (AntHillError, OSError) as exc:
        fail(str(exc))
    finally:
        # claim 可能已被同一会话的 MCP 子进程接手。release 只会松自己
        # 或已死子进程的 claim，不会踢掉别的活会话。
        with suppress(Exception):
            if "picked" in locals():
                release(layout, picked)
    if code:
        raise typer.Exit(code)


def _tui_options(
    *,
    model: str,
    profile: str,
    sandbox: str,
    approval: str,
    approve_for_me: bool,
    yolo: bool,
    search: bool,
    no_alt_screen: bool,
) -> list[str]:
    if sandbox and sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise AntHillError("--sandbox 只能是 read-only / workspace-write / danger-full-access")
    if approval and approval not in {"on-request", "never"}:
        raise AntHillError("--ask-for-approval 只能是 on-request / never")
    args: list[str] = []
    for flag, value in (
        ("--model", model),
        ("--profile", profile),
        ("--sandbox", sandbox),
        ("--ask-for-approval", approval),
    ):
        if value:
            args.extend((flag, value))
    if approve_for_me:
        args.append("--approve-for-me")
    if yolo:
        args.append("--dangerously-bypass-approvals-and-sandbox")
    if search:
        args.append("--search")
    if no_alt_screen:
        args.append("--no-alt-screen")
    return args


def _reject_attach_tui_options(args: list[str]) -> None:
    if args:
        raise AntHillError("接入正在运行的 thread 时，模型、sandbox、审批和 TUI 选项由现有前台决定")


def is_active_writer_error(exc: CodexRpcError) -> bool:
    return exc.method == "thread/resume" and "active writer" in str(exc.error).lower()


async def run_codex_session(
    *,
    layout: NodeLayout,
    node: str,
    agent: str,
    resume: str = "",
    tui_options: list[str] | None = None,
    codex: str = "codex",
    sensitive_env: frozenset[str] = frozenset(),
) -> int:
    executable = shutil.which(codex) if not Path(codex).is_file() else codex
    if not executable:
        raise CodexAppServerError("找不到 codex CLI；先安装 Codex 并确认 `codex --version` 能运行")

    bridge_root = layout.agent_dir(agent) / "bridge"
    bridge_root.mkdir(parents=True, exist_ok=True)
    app_server_log_path = bridge_root / "codex-app-server.log"
    event_log = EventLog(layout.logs / f"codex-bridge-{agent}.jsonl", agent=agent, echo=False)
    server: asyncio.subprocess.Process | None = None
    tui: asyncio.subprocess.Process | None = None
    drain: asyncio.Task[None] | None = None
    bridge_task: asyncio.Task[None] | None = None
    client: CodexRpcClient | None = None
    session_path: Path | None = None
    stop = asyncio.Event()
    child_env = sanitized_child_env(blocked=sensitive_env)

    try:
        with app_server_log_path.open("a", encoding="utf-8") as app_server_log:
            server, endpoint, drain = await start_app_server(
                codex=str(executable),
                cwd=layout.workspace,
                log_file=app_server_log,
                env=child_env,
            )
            client = CodexRpcClient(endpoint)
            await client.connect()
            thread_id = await create_or_resume_thread(
                client,
                layout=layout,
                agent=agent,
                node=node,
                resume=resume,
                developer_instructions=codex_session_instructions(layout, agent),
            )
            session_path = write_session(
                layout,
                agent,
                endpoint=endpoint,
                thread_id=thread_id,
                server_pid=server.pid,
            )
            event_log.info(
                "codex.bridge.started",
                endpoint=endpoint,
                thread=thread_id,
                server_pid=server.pid,
            )
            console.print(
                f"[green]已接入[/green] Codex app-server · Agent [b]{agent}[/b] · "
                f"thread [dim]{thread_id}[/dim]"
            )
            console.print("[dim]你可以正常对话；AntHill 来信会在当前 turn 结束后自动处理。[/dim]")

            bridge = CodexInboxBridge(
                client=client,
                layout=layout,
                agent=agent,
                thread_id=thread_id,
                log=event_log,
            )
            bridge_task = asyncio.create_task(bridge.run(stop))
            command = [
                str(executable),
                "--remote",
                endpoint,
                "--cd",
                str(layout.workspace),
                *(tui_options or []),
                "resume",
                "--include-non-interactive",
                thread_id,
            ]
            try:
                tui = await asyncio.create_subprocess_exec(
                    *command, cwd=str(layout.workspace), env=child_env
                )
            except OSError as exc:
                raise CodexAppServerError(f"启动 Codex TUI 失败：{exc}") from exc
            code = await tui.wait()
            stop.set()
            return code
    finally:
        stop.set()
        if bridge_task is not None:
            bridge_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await bridge_task
        if client is not None:
            with suppress(Exception):
                await client.close()
        if tui is not None and tui.returncode is None:
            tui.terminate()
            with suppress(TimeoutError):
                await asyncio.wait_for(tui.wait(), timeout=3)
        if server is not None and server.returncode is None:
            server.terminate()
            with suppress(TimeoutError):
                await asyncio.wait_for(server.wait(), timeout=5)
            if server.returncode is None:
                server.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(server.wait(), timeout=3)
        if drain is not None:
            drain.cancel()
            with suppress(asyncio.CancelledError):
                await drain
        if session_path is not None:
            _remove_own_session(session_path)
        event_log.info("codex.bridge.stopped")
        event_log.close()


async def run_codex_queue_session(
    *,
    layout: NodeLayout,
    agent: str,
    thread_id: str,
    codex: str = "codex",
    sensitive_env: frozenset[str] = frozenset(),
) -> int:
    """接入已有 writer：queue 负责唤醒，只读 app-server 负责收最终回答。"""
    executable = shutil.which(codex) if not Path(codex).is_file() else codex
    if not executable:
        raise CodexAppServerError("找不到 codex CLI；先安装 Codex 并确认 `codex --version` 能运行")

    child_env = sanitized_child_env(blocked=sensitive_env)
    await _require_codex_queue(str(executable), layout.workspace, env=child_env)
    bridge_root = layout.agent_dir(agent) / "bridge"
    bridge_root.mkdir(parents=True, exist_ok=True)
    app_server_log_path = bridge_root / "codex-read-app-server.log"
    event_log = EventLog(layout.logs / f"codex-bridge-{agent}.jsonl", agent=agent, echo=False)
    server: asyncio.subprocess.Process | None = None
    drain: asyncio.Task[None] | None = None
    client: CodexRpcClient | None = None
    session_path: Path | None = None
    stop = asyncio.Event()

    try:
        with app_server_log_path.open("a", encoding="utf-8") as app_server_log:
            server, endpoint, drain = await start_app_server(
                codex=str(executable),
                cwd=layout.workspace,
                log_file=app_server_log,
                env=child_env,
            )
            client = CodexRpcClient(endpoint)
            await client.connect()
            # thread/read 不加载 thread、不抢 writer；同时尽早检查 ID 是否有效。
            await client.request("thread/read", {"threadId": thread_id, "includeTurns": False})
            session_path = write_session(
                layout,
                agent,
                endpoint=endpoint,
                thread_id=thread_id,
                server_pid=server.pid,
                mode="queue-attach",
            )
            event_log.info(
                "codex.queue.started",
                endpoint=endpoint,
                thread=thread_id,
                server_pid=server.pid,
            )
            console.print(
                f"[green]已接入[/green] 现有 Codex 前台 · Agent [b]{agent}[/b] · "
                f"thread [dim]{thread_id}[/dim]"
            )
            console.print(
                "[dim]AntHill 来信将通过 Codex 原生 queue 唤醒；处理过程显示在原前台。"
                "此监听终端请保持运行，Ctrl-C 停止。[/dim]"
            )
            bridge = CodexQueueBridge(
                client=client,
                layout=layout,
                agent=agent,
                thread_id=thread_id,
                codex=str(executable),
                log=event_log,
                child_env=child_env,
            )
            await bridge.run(stop)
            return 0
    finally:
        stop.set()
        if client is not None:
            with suppress(Exception):
                await client.close()
        if server is not None and server.returncode is None:
            server.terminate()
            with suppress(TimeoutError):
                await asyncio.wait_for(server.wait(), timeout=5)
            if server.returncode is None:
                server.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(server.wait(), timeout=3)
        if drain is not None:
            drain.cancel()
            with suppress(asyncio.CancelledError):
                await drain
        if session_path is not None:
            _remove_own_session(session_path)
        event_log.info("codex.queue.stopped")
        event_log.close()


async def _require_codex_queue(codex: str, cwd: Path, *, env: dict[str, str]) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            codex,
            "queue",
            "--help",
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise CodexAppServerError(f"检查 codex queue 失败：{exc}") from exc
    stdout, stderr = await process.communicate()
    output = (stdout + stderr).decode(errors="replace")
    if process.returncode or "--thread" not in output or "--message" not in output:
        raise CodexAppServerError(
            "当前 Codex CLI 不支持 `codex queue`；请升级到带 session queue 的版本"
        )


def _remove_own_session(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if int(data.get("pid", 0) or 0) == os.getpid():
        path.unlink(missing_ok=True)
