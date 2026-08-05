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
)
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
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRecord, PeerRegistry
from anthill.security.keys import PairingToken, fingerprint
from anthill.security.pairing import (
    CONFIRM_HOST,
    CONFIRM_JOINER,
    PairingStore,
    confirm_tag,
    derive,
    exchange,
    tags_match,
)
from anthill.security.signing import verify_envelope, verify_request
from anthill.web.actions import (
    is_local_client,
    is_same_origin,
)
from anthill.web.admin import (
    RemoteConfigRequest,
    admin_enabled,
    read_remote_config,
    write_remote_config,
)
from anthill.web.admin import refuse_reason as _admin_refuse
from anthill.web.agents import start_agent, stop_agent
from anthill.web.cluster import ClusterCache, read_status
from anthill.web.context import NOT_READY, NodeContext, NodeRegistry
from anthill.web.endpoints import (
    AGENTS_PATH,
    CONFIG_PATH,
    DELIVER_PATH,
    PAIR_CONFIRM_PATH,
    PAIR_PATH,
    SUMMARY_PATH,
)
from anthill.web.panel_routes import mount_panel, mount_panel_actions

PANEL_HTML = Path(__file__).parent / "static" / "panel.html"
PANEL_REFRESH = 2.0


def create_app(
    *,
    layout: NodeLayout | None = None,
    config: Config | None = None,
    peers: PeerRegistry | None = None,
    registry: NodeRegistry | None = None,
    log: EventLog,
    panel: bool = False,
    panel_writable: bool = False,
    summary: bool = True,
    advertise: str = "",
    remote_admin: bool = False,
    panel_token: str = "",
) -> FastAPI:
    nodes = registry if registry is not None else _bootstrap(layout, config)
    app = FastAPI(
        title=f"anthill:{nodes.primary_name}",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.cluster_cache = ClusterCache()

    app.state.nodes = nodes

    def ready() -> None:
        if not nodes.ready:
            raise HTTPException(status_code=503, detail=NOT_READY)

    def target(name: str) -> NodeContext:
        """按名字取本进程照看的某个节点。名字不对就 421「这信不该寄到这儿」。"""
        ready()
        ctx = nodes.get(name) if name else nodes.primary
        if ctx is None:
            raise HTTPException(
                status_code=421,
                detail=f"本机没有名为 {name!r} 的节点（这里有：{'、'.join(nodes.names())}）",
            )
        return ctx

    if panel:
        mount_panel(app, nodes=nodes, log=log, token=panel_token)
    if panel and panel_writable:
        mount_panel_actions(app, nodes=nodes, log=log, token=panel_token)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """给对端探活用。只暴露公开信息：节点名与 Agent 名单，绝不含密钥或路径。"""
        ready()
        return {
            "node": nodes.primary_name,
            "nodes": [
                {"node": ctx.name, "agents": sorted(ctx.config.agents)} for ctx in nodes.all()
            ],
            "agents": sorted(nodes.primary.config.agents),
            "proto": Envelope.model_fields["proto"].default,
        }

    def _signed(request: Request, ctx: NodeContext, node: str, ts: str, sig: str) -> None:
        """已信任对端 + 有效签名，两道都过才算数。

        **签名签的是请求的完整路径** —— 一台机器上现在可能有好几个节点，
        路径里带着「问的是哪个节点」，所以一次签名换不到另一个节点的东西。
        校验用的是**收件那个节点自己的** peers 列表：密钥是跟着工作区走的。
        """
        try:
            _, key = ctx.peers.require_trusted(node)
        except PeerError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        try:
            verify_request(key, node=node, path=request.url.path, ts=ts, signature=sig)
        except SignatureError as exc:
            log.warn("request.rejected", frm=node, path=request.url.path, reason=str(exc))
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def _summary(request: Request, name: str, headers: tuple[str, str, str]) -> dict[str, Any]:
        """把某个节点的状态给**已信任的对端**看，供它做总控面板。

        认证跟投递同一把共享密钥，30 秒防重放窗。

        **说清楚给出去的是什么**：Agent 名单与积压、编排任务的目标与每步交付、
        最近若干条日志（含 `error` 字段，里面可能带本机路径或某个 peer 的
        `user@host`）。不含密钥，也不含本机的 peers 列表。
        换句话说：把一个节点标成 trusted，意味着它既能给你投消息，
        也能看你在干什么。不想共享就用 `anthill serve --no-summary`。
        """
        if not summary:
            raise HTTPException(status_code=404, detail="本节点没有开放状态共享")
        ctx = target(name)
        _signed(request, ctx, *headers)
        return read_status(ctx.layout, ctx.config, ctx.peers)

    @app.get(SUMMARY_PATH)
    async def node_summary(
        request: Request,
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        """不指名时说的是主节点 —— 一台机器只有一个工作区时就是全部。"""
        return _summary(request, "", (x_anthill_node, x_anthill_ts, x_anthill_sig))

    @app.get("/node/{name}/summary")
    async def node_summary_named(
        request: Request,
        name: str,
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        """一台机器可以照看好几个节点，所以要能指名道姓地问。"""
        return _summary(request, name, (x_anthill_node, x_anthill_ts, x_anthill_sig))

    def _config_read(request: Request, name: str, headers: tuple[str, str, str]) -> dict[str, Any]:
        ctx = target(name)
        _admin_open(ctx.config, remote_admin)
        _signed(request, ctx, *headers)
        return read_remote_config(ctx.layout, by=headers[0], log=log)

    def _config_write(
        request: Request, name: str, body: RemoteConfigRequest, headers: tuple[str, str, str]
    ) -> dict[str, Any]:
        """让已信任的对端直接改某个节点的 node.toml。

        **这是本项目权限最大的一个接口** —— 能改配置就能加一个带 run_shell 的
        Agent。所以它默认根本不存在（404），要机器主人显式打开；
        打开之后每一次读写都留审计日志。
        """
        ctx = target(name)
        _admin_open(ctx.config, remote_admin)
        _signed(request, ctx, *headers)
        try:
            return write_remote_config(ctx.layout, body, by=headers[0], log=log)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(CONFIG_PATH)
    async def node_config(
        request: Request,
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        return _config_read(request, "", (x_anthill_node, x_anthill_ts, x_anthill_sig))

    @app.get("/node/{name}/config")
    async def node_config_named(
        request: Request,
        name: str,
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        return _config_read(request, name, (x_anthill_node, x_anthill_ts, x_anthill_sig))

    @app.put(CONFIG_PATH)
    async def node_config_write(
        request: Request,
        body: RemoteConfigRequest = Body(...),
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        return _config_write(request, "", body, (x_anthill_node, x_anthill_ts, x_anthill_sig))

    @app.put("/node/{name}/config")
    async def node_config_write_named(
        request: Request,
        name: str,
        body: RemoteConfigRequest = Body(...),
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        return _config_write(request, name, body, (x_anthill_node, x_anthill_ts, x_anthill_sig))

    def _agent_control(
        request: Request, name: str, agent: str, action: str, headers: tuple[str, str, str]
    ) -> dict[str, Any]:
        """让已信任的对端启停某个节点的 agentd。需要 remote_admin 打开。

        权限级别和改 node.toml 是同一档 —— 能改配置本来就约等于能执行命令，
        所以走同一道闸，不另设一个。
        """
        ctx = target(name)
        _admin_open(ctx.config, remote_admin)
        _signed(request, ctx, *headers)
        if action not in ("start", "stop"):
            raise HTTPException(status_code=404, detail=f"不认识的动作 {action!r}")
        try:
            result = (
                start_agent(ctx.layout, ctx.config, agent)
                if action == "start"
                else stop_agent(ctx.layout, agent)
            )
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.warn("admin.agent_" + action, by=headers[0], node=ctx.name, agent=agent)
        return result

    @app.post(f"{AGENTS_PATH}/{{agent}}/{{action}}", status_code=202)
    async def node_agent_control(
        request: Request,
        agent: str,
        action: str,
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        return _agent_control(
            request, "", agent, action, (x_anthill_node, x_anthill_ts, x_anthill_sig)
        )

    # 少一个 f 前缀就注册成字面量 `{{agent}}` 了 —— 这条路由从来没匹配上过任何请求，
    # 而 404 又被翻译成「对端没开远端管理」，反过来诱导人去打开 remote_admin。
    @app.post("/node/{name}/agents/{agent}/{action}", status_code=202)
    async def node_agent_control_named(
        request: Request,
        name: str,
        agent: str,
        action: str,
        x_anthill_node: str = Header(default=""),
        x_anthill_ts: str = Header(default=""),
        x_anthill_sig: str = Header(default=""),
    ) -> dict[str, Any]:
        return _agent_control(
            request, name, agent, action, (x_anthill_node, x_anthill_ts, x_anthill_sig)
        )

    @app.post(PAIR_PATH)
    async def pair(body: PairRequest = Body(...)) -> dict[str, Any]:
        """PIN 码配对的第一步。**只在本机开着窗口时才存在意义**。

        没有窗口就是 409 —— 不是「密码错」，因为这时候根本没有可比对的东西。
        窗口用过一次立刻作废：六位 PIN 经不起在线穷举，
        而 PAKE 保证离线穷举无从下手（线路上没有密钥，也没有密文）。
        """
        ctx = target(body.for_node)
        return _pair_begin(ctx.layout, ctx.config, ctx.peers, log, body, endpoint=advertise)

    @app.post(PAIR_CONFIRM_PATH)
    async def pair_confirm(body: PairConfirmRequest = Body(...)) -> dict[str, Any]:
        """第二步：对方证明它推导出了同一把钥匙，本机才真正落库。

        少了这一步，PIN 打错会配成「看起来成功了、之后每条消息都验签失败」——
        最难查的那种状态。SPAKE2 在口令不符时不报错，只是各得一把不同的钥匙。
        """
        ctx = target(body.for_node)
        return _pair_commit(ctx.layout, ctx.peers, log, body)

    @app.post(DELIVER_PATH, status_code=202)
    async def deliver(
        body: dict[str, Any] = Body(...),
        x_anthill_endpoint: str = Header(default=""),
    ) -> dict[str, Any]:
        """收信。**收件人写在信封上，所以一个进程可以替好几个节点收。**

        `to.node` 就是分派键：找到本进程照看的那个节点，用**它自己的** peers
        验签、往**它自己的**邮箱里放。工作区之间是隔离的，这里也不例外。
        """
        ready()
        env = _parse(body)
        ctx = nodes.get(env.to.node)
        if ctx is None:
            # 不当跳板：只收发给本机某个节点的信，绝不代为转投第三方
            log.warn("lan.rejected", msg=env.id, to=str(env.to), reason="misrouted")
            raise HTTPException(
                status_code=421,
                detail=f"本机没有名为 {env.to.node!r} 的节点"
                f"（这里有：{'、'.join(nodes.names())}），不代转",
            )
        peer, key = _trusted_key(ctx.peers, env, log)
        _verify(env, key, log)
        _check_recipient(env, ctx.config, log)
        path = _deposit(env, ctx.layout, log)
        _learn_return_path(ctx.peers, peer, x_anthill_endpoint, log)
        log.info(
            "lan.received",
            node=ctx.name,
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
    for_node: str = Field(default="", max_length=64)
    """要和本机的哪个节点配对。留空 = 主节点。"""
    endpoint: str = Field(default="", max_length=200)
    msg: str = Field(min_length=1, max_length=512)
    """对方的 SPAKE2 消息，base64。"""


class PairConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str = Field(min_length=1, max_length=64)
    for_node: str = Field(default="", max_length=64)
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


def _bootstrap(layout: NodeLayout | None, config: Config | None) -> NodeRegistry:
    """兼容老的调用方式：直接给 layout/config 就是「只照看这一个节点」。

    给 None 就是「还没配工作区」—— 名册是空的，节点端点一律 503。
    """
    del config  # ConfigRef 会自己按 mtime 读，启动那份没必要留着
    return NodeRegistry([NodeContext(layout)] if layout is not None else [])


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
    """收件节点在分派那一步已经定了，这里只管「那个节点上有没有这个 Agent」。"""
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
