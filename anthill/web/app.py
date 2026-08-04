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
from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from anthill.core.config import Config
from anthill.core.envelope import Envelope
from anthill.core.errors import (
    AntHillError,
    MailboxError,
    PeerError,
    ProtocolError,
    SignatureError,
)
from anthill.core.ids import is_valid_id
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRecord, PeerRegistry
from anthill.security.keys import PairingToken, fingerprint
from anthill.security.pair_client import join as pair_join
from anthill.security.pair_client import resolve as pair_resolve
from anthill.security.pairing import (
    CONFIRM_HOST,
    CONFIRM_JOINER,
    WINDOW_SECONDS,
    PairingStore,
    confirm_tag,
    derive,
    exchange,
    new_pin,
    tags_match,
)
from anthill.security.signing import verify_envelope, verify_request
from anthill.web.actions import (
    ConfigRequest,
    RunRequest,
    SendRequest,
    is_local_client,
    is_same_origin,
    read_config,
    send_message,
    start_run,
    write_config,
)
from anthill.web.admin import (
    RemoteConfigRequest,
    admin_enabled,
    read_remote_config,
    write_remote_config,
)
from anthill.web.admin import refuse_reason as _admin_refuse
from anthill.web.chat import messages as chat_messages
from anthill.web.chat import threads as chat_threads
from anthill.web.cluster import ClusterCache, build_cluster, read_status
from anthill.web.endpoints import (
    CONFIG_PATH,
    DELIVER_PATH,
    PAIR_CONFIRM_PATH,
    PAIR_PATH,
    PANEL_PATH,
    SUMMARY_PATH,
)
from anthill.web.panel import build_snapshot
from anthill.web.remote import read_config as remote_read_config
from anthill.web.remote import write_config as remote_write_config

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
    summary: bool = True,
    advertise: str = "",
    remote_admin: bool = False,
) -> FastAPI:
    app = FastAPI(
        title=f"anthill:{config.node.name}",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.cluster_cache = ClusterCache()
    if panel:
        _mount_panel(app, layout=layout, config=config, peers=peers, log=log)
    if panel and panel_writable:
        _mount_panel_actions(app, layout=layout, config=config, peers=peers, log=log)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """给对端探活用。只暴露公开信息：节点名与 Agent 名单，绝不含密钥或路径。"""
        return {
            "node": config.node.name,
            "agents": sorted(config.agents),
            "proto": Envelope.model_fields["proto"].default,
        }

    @app.get(SUMMARY_PATH)
    async def node_summary(
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        """把本节点的状态给**已信任的对端**看，供它做总控面板。

        认证跟投递同一把共享密钥：签 `节点 + 路径 + 时间戳`，30 秒防重放窗。

        **说清楚给出去的是什么**：Agent 名单与积压、编排任务的目标与每步交付、
        最近若干条日志（含 `error` 字段，里面可能带本机路径或某个 peer 的
        `user@host`）。不含密钥，也不含本机的 peers 列表。
        换句话说：把一个节点标成 trusted，意味着它既能给你投消息，
        也能看你在干什么。不想共享就用 `anthill serve --no-summary`。
        """
        if not summary:
            raise HTTPException(status_code=404, detail="本节点没有开放状态共享")
        try:
            _, key = peers.require_trusted(x_anthill_node)
        except PeerError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        try:
            verify_request(
                key,
                node=x_anthill_node,
                path=SUMMARY_PATH,
                ts=x_anthill_ts,
                signature=x_anthill_sig,
            )
        except SignatureError as exc:
            log.warn("summary.rejected", frm=x_anthill_node, reason=str(exc))
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return read_status(layout, config, peers)

    def _signed(path: str, node: str, ts: str, sig: str) -> None:
        """已信任对端 + 有效签名，两道都过才算数。和 /node/summary 同一套。"""
        try:
            _, key = peers.require_trusted(node)
        except PeerError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        try:
            verify_request(key, node=node, path=path, ts=ts, signature=sig)
        except SignatureError as exc:
            log.warn("admin.rejected", frm=node, path=path, reason=str(exc))
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get(CONFIG_PATH)
    async def node_config(
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        """把本机 node.toml 给已信任的对端看。需要 remote_admin 打开。"""
        _admin_open(config, remote_admin)
        _signed(CONFIG_PATH, x_anthill_node, x_anthill_ts, x_anthill_sig)
        return read_remote_config(layout, by=x_anthill_node, log=log)

    @app.put(CONFIG_PATH)
    async def node_config_write(
        body: RemoteConfigRequest = Body(...),
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        """让已信任的对端直接改本机 node.toml。

        **这是本项目权限最大的一个接口** —— 能改配置就能加一个带 run_shell 的
        Agent。所以它默认根本不存在（404），要机器主人显式打开；
        打开之后每一次读写都留审计日志。
        """
        _admin_open(config, remote_admin)
        _signed(CONFIG_PATH, x_anthill_node, x_anthill_ts, x_anthill_sig)
        try:
            return write_remote_config(layout, body, by=x_anthill_node, log=log)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(PAIR_PATH)
    async def pair(body: PairRequest = Body(...)) -> dict[str, Any]:
        """PIN 码配对的第一步。**只在本机开着窗口时才存在意义**。

        没有窗口就是 409 —— 不是「密码错」，因为这时候根本没有可比对的东西。
        窗口用过一次立刻作废：六位 PIN 经不起在线穷举，
        而 PAKE 保证离线穷举无从下手（线路上没有密钥，也没有密文）。
        """
        return _pair_begin(layout, config, peers, log, body, endpoint=advertise)

    @app.post(PAIR_CONFIRM_PATH)
    async def pair_confirm(body: PairConfirmRequest = Body(...)) -> dict[str, Any]:
        """第二步：对方证明它推导出了同一把钥匙，本机才真正落库。

        少了这一步，PIN 打错会配成「看起来成功了、之后每条消息都验签失败」——
        最难查的那种状态。SPAKE2 在口令不符时不报错，只是各得一把不同的钥匙。
        """
        return _pair_commit(layout, peers, log, body)

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


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(default="", max_length=200)
    msg: str = Field(min_length=1, max_length=512)
    """对方的 SPAKE2 消息，base64。"""


class PairConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str = Field(min_length=1, max_length=64)
    confirm: str = Field(min_length=1, max_length=128)


def _pair_begin(
    layout: NodeLayout,
    config: Config,
    peers: PeerRegistry,
    log: EventLog,
    body: PairRequest,
    *,
    endpoint: str,
) -> dict[str, Any]:
    store = PairingStore(layout.root)
    window = store.current()
    if window is None or window.used:
        log.warn("pair.no_window", frm=body.node)
        raise HTTPException(status_code=409, detail="本机现在没有开着的配对窗口")

    try:
        inbound = b64decode(body.msg, validate=True)
    except (BinasciiError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"配对消息不是合法 base64：{exc}") from exc

    state, outbound = exchange(window.pin)
    try:
        key = derive(state, inbound)
    except PeerError as exc:
        store.close()  # 一个窗口一次机会
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store.hold(window, node=body.node, endpoint=body.endpoint, key=key)
    log.info("pair.offered", node=body.node, fingerprint=fingerprint(key))
    return {
        "node": config.node.name,
        "endpoint": endpoint,
        "msg": b64encode(outbound).decode(),
        "confirm": confirm_tag(key, CONFIRM_HOST),
    }


def _pair_commit(
    layout: NodeLayout, peers: PeerRegistry, log: EventLog, body: PairConfirmRequest
) -> dict[str, Any]:
    store = PairingStore(layout.root)
    window = store.current()
    if window is None or not window.peer_key or window.peer_node != body.node:
        raise HTTPException(status_code=409, detail="没有等待确认的配对")

    key = bytes.fromhex(window.peer_key)
    if not tags_match(body.confirm, confirm_tag(key, CONFIRM_JOINER)):
        store.close()
        log.warn("pair.rejected", node=body.node, reason="密钥确认不匹配（PIN 打错了？）")
        raise HTTPException(status_code=401, detail="密钥确认不匹配：两边的 PIN 不一样")

    store.close()
    record = peers.trust(
        PairingToken(node=window.peer_node, endpoint=window.peer_endpoint, key=key), replace=True
    )
    log.info("pair.trusted", node=record.node, fingerprint=record.fingerprint)
    return {"ok": True, "node": record.node, "fingerprint": record.fingerprint}


def _admin_open(config: Config, flag: bool = False) -> None:
    """没打开远端管理时这个接口**根本不存在**（404），不是「存在但会拒绝」。

    这一点和面板写入口是同一条原则：默认不挂，别给人留一个可以试探的门把手。
    """
    if not (flag or admin_enabled(config)):
        raise HTTPException(status_code=404, detail=_admin_refuse(config))


def _local_only(request: Request, *, what: str = "这个接口") -> None:
    """逐请求校验来源是回环。

    面板绑 0.0.0.0 时（跨机投递需要），这些接口会跟着暴露给整个网段 ——
    而它们给出的是**所有**对端的状态和对话内容，不是本机那点公开信息。
    """
    if not is_local_client(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail=f"{what}只允许本机访问")
    if not is_same_origin(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(status_code=403, detail=f"拒绝跨站读取{what}")


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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """退出时把后台刷新任务收干净，免得 uvicorn 抱怨「任务还没结束就被销毁」。"""
    yield
    cache: ClusterCache | None = getattr(app.state, "cluster_cache", None)
    if cache is not None:
        await cache.aclose()


def _mount_panel(
    app: FastAPI, *, layout: NodeLayout, config: Config, peers: PeerRegistry, log: EventLog
) -> None:
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

    @app.get(f"{PANEL_PATH}/api/cluster")
    async def panel_cluster(request: Request) -> dict[str, Any]:
        """总控视图：本机 + 所有已信任对端。连不上的标成不可达，不卡住整页。

        **只允许本机访问**，和写入口同一条理由：这个 GET 是有副作用的
        （每次可能触发 N 次对外连接），而且它把**所有**对端的状态汇到一处 ——
        面板绑了 `0.0.0.0` 时，那等于把整个集群的状态摊给整个网段。
        页面拿到 403 会自动退回只看本机。
        """
        _local_only(request, what="总控视图")
        return await build_cluster(layout, config, peers, log, request.app.state.cluster_cache)

    @app.get(f"{PANEL_PATH}/api/chats")
    async def panel_chats(request: Request) -> dict[str, Any]:
        """最近的会话列表。和总控视图同样只对本机开放 —— 这是对话内容。"""
        _local_only(request)
        return {"threads": chat_threads(layout)}

    @app.get(f"{PANEL_PATH}/api/chat/{{thread}}")
    async def panel_chat(request: Request, thread: str) -> dict[str, Any]:
        _local_only(request)
        if not is_valid_id(thread):
            raise HTTPException(status_code=400, detail="thread 不是合法 ULID")
        return {"thread": thread, "messages": chat_messages(layout, thread)}

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
    app: FastAPI, *, layout: NodeLayout, config: Config, peers: PeerRegistry, log: EventLog
) -> None:
    """面板的写入口（`anthill serve --panel-write` 才挂）。

    **逐请求校验来源是回环**，而不是依赖「我们绑的是 127.0.0.1 所以应该安全」——
    反向代理、端口转发、配置写错，任何一种都会让那个假设悄悄失效。
    能改配置 ≈ 能在这台机器上执行命令，这个前提值得多一道显式检查。

    确认与审批仍然只在 CLI：面板能发起任务，但不能替你批准危险操作。
    """

    def _guard(request: Request) -> None:
        """两道：连接必须来自本机，且请求不能是别的站点发起的。

        第一道是真正的那道闸（TCP 对端地址，客户端伪造不了）。
        第二道是纵深防御，见 `actions.is_same_origin` 的说明。
        """
        if not is_local_client(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="面板的写操作只允许本机访问")
        if not is_same_origin(request.headers.get("origin"), request.headers.get("host")):
            raise HTTPException(status_code=403, detail="拒绝跨站发起的写操作")

    @app.post(f"{PANEL_PATH}/api/run", status_code=202)
    async def panel_run(request: Request, body: RunRequest = Body(...)) -> dict[str, Any]:
        _guard(request)
        return await _acted(start_run(layout, config, body, log))

    @app.post(f"{PANEL_PATH}/api/send", status_code=202)
    async def panel_send(request: Request, body: SendRequest = Body(...)) -> dict[str, Any]:
        _guard(request)
        return await _acted(send_message(layout, config, body, log))

    @app.post(f"{PANEL_PATH}/api/pair/open", status_code=201)
    async def panel_pair_open(request: Request) -> dict[str, Any]:
        """在本机开一个配对窗口，把 PIN 显示在页面上让对方输。"""
        _guard(request)
        code = new_pin()
        PairingStore(layout.root).open(code)
        log.info("pair.window_opened")
        return {"pin": code, "seconds": int(WINDOW_SECONDS)}

    @app.post(f"{PANEL_PATH}/api/pair/join", status_code=201)
    async def panel_pair_join(
        request: Request, body: PairJoinRequest = Body(...)
    ) -> dict[str, Any]:
        """输入对方屏幕上的 PIN，把密钥换过来。"""
        _guard(request)
        try:
            record = await pair_join(
                base=pair_resolve(peers, body.target),
                my_node=config.node.name,
                my_endpoint=config.node.endpoint,
                pin=body.pin,
                peers=peers,
            )
        except PeerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "node": record.node, "fingerprint": record.fingerprint}

    @app.get(f"{PANEL_PATH}/api/config")
    async def panel_config(request: Request, node: str = "") -> dict[str, Any]:
        """`?node=` 指定哪台机器；留空就是本机。"""
        _guard(request)
        if node and node != config.node.name:
            return await _remote(remote_read_config(config, peers, node))
        try:
            return {"node": config.node.name, **read_config(layout)}
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"读不出配置：{exc}") from exc

    @app.put(f"{PANEL_PATH}/api/config")
    async def panel_config_write(
        request: Request, body: RemoteConfigWrite = Body(...)
    ) -> dict[str, Any]:
        _guard(request)
        if body.node and body.node != config.node.name:
            return await _remote(remote_write_config(config, peers, body.node, body.text))
        try:
            result = write_config(layout, ConfigRequest(text=body.text))
        except AntHillError as exc:
            # 配置不合法就原样退回，磁盘一个字都不改
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("panel.config_written")
        return result


class PairJoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, max_length=200)
    """对方的节点名或地址。"""

    pin: str = Field(min_length=4, max_length=16)


class RemoteConfigWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=200_000)
    node: str = Field(default="", max_length=64)
    """改哪台机器的配置。留空 = 本机。"""


async def _remote(coro: Any) -> dict[str, Any]:
    """对端的拒绝原样透给页面 —— 「它没开远端管理」是用户要看懂的信息。"""
    try:
        return await coro  # type: ignore[no-any-return]
    except AntHillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _acted(coro: Any) -> dict[str, Any]:
    try:
        return await coro  # type: ignore[no-any-return]
    except AntHillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
