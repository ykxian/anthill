"""LAN 投递端点。

它做的事只有一件：**把一个可信的信封原子地写进本机某个 Agent 的 inbox/new**。
写完就结束 —— 后续处理由那个 Agent 的 watcher 接手。
这正是「一切皆邮箱」的好处：HTTP 只是又一种把文件送进目录的方式，
agentd 完全不需要知道这条消息是从网线上来的。

准入是四道闸，任何一道不过都不落盘：
1. 能不能解析成合法信封（400）
2. 发件节点在不在信任列表里（403）—— 发现 ≠ 可通信
3. 签名与时间窗对不对（401）
4. 收件人是不是本机的某个已存在 Agent（421 / 404）
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import (
    Body,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from anthill.core.config import Config
from anthill.core.envelope import Envelope
from anthill.core.errors import (
    AntHillError,
    MailboxError,
    PeerError,
    ProtocolError,
    SignatureError,
)
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRecord, PeerRegistry
from anthill.security.signing import verify_envelope
from anthill.web.actions import (
    ConfigRequest,
    RunRequest,
    SendRequest,
    is_local_client,
    read_config,
    send_message,
    start_run,
    write_config,
)
from anthill.web.endpoints import DELIVER_PATH, PANEL_PATH
from anthill.web.panel import build_snapshot

PANEL_HTML = Path(__file__).parent / "static" / "panel.html"
PANEL_REFRESH = 2.0


def create_app(
    *,
    layout: NodeLayout,
    config: Config,
    peers: PeerRegistry,
    log: EventLog,
    panel: bool = False,
    panel_writable: bool = False,
) -> FastAPI:
    app = FastAPI(title=f"anthill:{config.node.name}", docs_url=None, redoc_url=None)
    if panel:
        _mount_panel(app, layout=layout, config=config, peers=peers)
    if panel and panel_writable:
        _mount_panel_actions(app, layout=layout, config=config, log=log)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """给对端探活用。只暴露公开信息：节点名与 Agent 名单，绝不含密钥或路径。"""
        return {
            "node": config.node.name,
            "agents": sorted(config.agents),
            "proto": Envelope.model_fields["proto"].default,
        }

    @app.post(DELIVER_PATH, status_code=202)
    async def deliver(
        body: dict[str, Any] = Body(...),
        x_anthill_endpoint: str = Header(default=""),
    ) -> dict[str, Any]:
        env = _parse(body)
        peer, key = _trusted_key(peers, env, log)
        _verify(env, key, log)
        _check_recipient(env, config, log)
        path = _deposit(env, layout, log)
        _learn_return_path(peers, peer, x_anthill_endpoint, log)
        log.info(
            "lan.received",
            msg=env.id,
            frm=str(env.from_),
            to=str(env.to),
            type=str(env.type),
            thread=env.thread,
        )
        return {"ok": True, "id": env.id, "path": str(path)}

    return app


def _parse(body: dict[str, Any]) -> Envelope:
    try:
        return Envelope.model_validate(body)
    except (ValidationError, ProtocolError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"信封不合法：{exc}") from exc


def _trusted_key(peers: PeerRegistry, env: Envelope, log: EventLog) -> tuple[PeerRecord, bytes]:
    try:
        return peers.require_trusted(env.from_.node)
    except PeerError as exc:
        log.warn("lan.rejected", msg=env.id, frm=str(env.from_), reason="untrusted")
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _verify(env: Envelope, key: bytes, log: EventLog) -> None:
    try:
        verify_envelope(env, key)
    except SignatureError as exc:
        log.warn("lan.rejected", msg=env.id, frm=str(env.from_), reason="signature")
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _check_recipient(env: Envelope, config: Config, log: EventLog) -> None:
    if env.to.node != config.node.name:
        # 不当跳板：只收发给自己的信，绝不代为转投第三方
        log.warn("lan.rejected", msg=env.id, to=str(env.to), reason="misrouted")
        raise HTTPException(
            status_code=421,
            detail=f"本节点是 {config.node.name}，这条消息是发给 {env.to.node} 的，不代转",
        )
    if env.to.is_role or env.to.is_broadcast:
        return  # 角色/广播地址由本机路由层解析，端点这里不展开
    if env.to.agent not in config.agents:
        log.warn("lan.rejected", msg=env.id, to=str(env.to), reason="unknown_agent")
        raise HTTPException(status_code=404, detail=f"本节点没有 Agent {env.to.agent!r}")


def _learn_return_path(peers: PeerRegistry, peer: PeerRecord, endpoint: str, log: EventLog) -> None:
    """记下对端自报的回信地址。

    没有这一步，`invite/trust` 配好的一对节点只能单向通信：被邀请方的地址
    邀请方根本不知道。只对**已经通过签名校验**的来件生效，所以不构成新的攻击面
    （能伪造这个头的人已经拿到了共享密钥）。
    """
    address = endpoint.strip()
    if not address.startswith(("http://", "https://")) or address == peer.endpoint:
        return
    peers.observe(node=peer.node, endpoint=address[:200], agents=peer.agents)
    log.info("peer.endpoint_learned", node=peer.node, endpoint=address)


def _deposit(env: Envelope, layout: NodeLayout, log: EventLog) -> Any:
    mailbox = Mailbox(layout.mailbox_dir(env.to.agent))
    if not mailbox.exists:
        log.warn("lan.rejected", msg=env.id, to=str(env.to), reason="no_mailbox")
        raise HTTPException(
            status_code=404, detail=f"{env.to.agent} 的邮箱还没建（agentd 没启动过？）"
        )
    try:
        return mailbox.deposit(env)
    except MailboxError as exc:
        log.error("lan.deposit_failed", msg=env.id, error=str(exc))
        raise HTTPException(status_code=503, detail=f"写入邮箱失败：{exc}") from exc


def _mount_panel(app: FastAPI, *, layout: NodeLayout, config: Config, peers: PeerRegistry) -> None:
    """面板的只读部分（03-tech-design §9）。

    默认就只有这些 `GET` —— 一个只会读的页面，最坏也就是被人看到状态。
    写入口是另外挂的（`_mount_panel_actions`），默认不开。
    """

    @app.get(PANEL_PATH, response_class=HTMLResponse)
    async def panel_page() -> HTMLResponse:
        try:
            return HTMLResponse(PANEL_HTML.read_text(encoding="utf-8"))
        except OSError as exc:  # pragma: no cover - 安装损坏才会走到
            raise HTTPException(status_code=500, detail=f"面板页面读不出来：{exc}") from exc

    @app.get(f"{PANEL_PATH}/api/state")
    async def panel_state() -> dict[str, Any]:
        return build_snapshot(layout, config, peers)

    @app.websocket(f"{PANEL_PATH}/ws")
    async def panel_ws(websocket: WebSocket) -> None:
        """定时推快照。做成推送而不是让页面轮询，是为了 kill -9 之后状态能立刻变灰。"""
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(build_snapshot(layout, config, peers))
                await asyncio.sleep(PANEL_REFRESH)
        except (WebSocketDisconnect, RuntimeError):
            return  # 页面关了，正常退出


def _mount_panel_actions(
    app: FastAPI, *, layout: NodeLayout, config: Config, log: EventLog
) -> None:
    """面板的写入口（`anthill serve --panel-write` 才挂）。

    **逐请求校验来源是回环**，而不是依赖「我们绑的是 127.0.0.1 所以应该安全」——
    反向代理、端口转发、配置写错，任何一种都会让那个假设悄悄失效。
    能改配置 ≈ 能在这台机器上执行命令，这个前提值得多一道显式检查。

    确认与审批仍然只在 CLI：面板能发起任务，但不能替你批准危险操作。
    """

    def _guard(request: Request) -> None:
        if not is_local_client(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="面板的写操作只允许本机访问")

    @app.post(f"{PANEL_PATH}/api/run", status_code=202)
    async def panel_run(request: Request, body: RunRequest = Body(...)) -> dict[str, Any]:
        _guard(request)
        return await _acted(start_run(layout, config, body, log))

    @app.post(f"{PANEL_PATH}/api/send", status_code=202)
    async def panel_send(request: Request, body: SendRequest = Body(...)) -> dict[str, Any]:
        _guard(request)
        return await _acted(send_message(layout, config, body, log))

    @app.get(f"{PANEL_PATH}/api/config")
    async def panel_config(request: Request) -> dict[str, Any]:
        _guard(request)
        try:
            return read_config(layout)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"读不出配置：{exc}") from exc

    @app.put(f"{PANEL_PATH}/api/config")
    async def panel_config_write(
        request: Request, body: ConfigRequest = Body(...)
    ) -> dict[str, Any]:
        _guard(request)
        try:
            result = write_config(layout, body)
        except AntHillError as exc:
            # 配置不合法就原样退回，磁盘一个字都不改
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("panel.config_written")
        return result


async def _acted(coro: Any) -> dict[str, Any]:
    try:
        return await coro  # type: ignore[no-any-return]
    except AntHillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
