"""面板的 HTTP 路由。

从 `app.py` 里分出来，是因为那边该只管**节点对节点**那几个端点
（投递、状态共享、配对、远端管理）；面板是给人用的另一套东西，
两者的读者、威胁模型、变更节奏都不一样，放一起只会越滚越大。

分界线：这里所有东西都在 `/panel` 下，且**一律只允许本机访问** ——
读的是全集群状态和对话内容，写的等价于在这台机器上执行命令。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from anthill.core.config import Config
from anthill.core.errors import AntHillError, PeerError
from anthill.core.ids import is_valid_id
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.workspace import ensure_mailboxes
from anthill.security.pair_client import join as pair_join
from anthill.security.pair_client import resolve as pair_resolve
from anthill.security.pairing import WINDOW_SECONDS, PairingStore, new_pin
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
from anthill.web.agents import AgentSpec, add_agent, remove_agent, start_agent, stop_agent
from anthill.web.chat import messages as chat_messages
from anthill.web.chat import threads as chat_threads
from anthill.web.cluster import build_cluster
from anthill.web.context import NodeContext
from anthill.web.endpoints import PANEL_PATH
from anthill.web.panel import build_snapshot
from anthill.web.remote import read_config as remote_read_config
from anthill.web.remote import write_config as remote_write_config
from anthill.web.setup import browse
from anthill.web.setup import home as setup_home
from anthill.web.workspaces import WorkspaceSpec
from anthill.web.workspaces import create as create_workspace_entry
from anthill.web.workspaces import delete as delete_workspace
from anthill.web.workspaces import listing as list_workspaces

PANEL_HTML = Path(__file__).parent / "static" / "panel.html"
PANEL_REFRESH = 2.0


def local_only(request: Request, *, what: str = "这个接口") -> None:
    """逐请求校验来源是回环 + 不是跨站发起的。

    面板绑 0.0.0.0 时（跨机投递需要），这些接口会跟着暴露给整个网段 ——
    而它们给出的是**所有**对端的状态和对话内容，不是本机那点公开信息。
    """
    if not is_local_client(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail=f"{what}只允许本机访问")
    if not is_same_origin(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(status_code=403, detail=f"拒绝跨站读取{what}")


def mount_panel(app: FastAPI, *, ctx: NodeContext, log: EventLog) -> None:
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

    @app.get(f"{PANEL_PATH}/api/setup")
    async def panel_setup(request: Request) -> dict[str, Any]:
        """本机还没配好工作区时，页面靠这个知道该显示设置界面。"""
        local_only(request, what="设置界面")
        return {
            "ready": ctx.ready,
            "node": ctx.node_name,
            "workspace": str(ctx.layout.workspace) if ctx.ready else "",
            "home": str(setup_home()),
            "workspaces": list_workspaces(ctx.layout if ctx.ready else None),
        }

    @app.get(f"{PANEL_PATH}/api/setup/browse")
    async def panel_browse(request: Request, path: str = "") -> dict[str, Any]:
        """挑工作区放哪用的目录浏览器。只列目录，只对本机开放。"""
        local_only(request, what="目录浏览")
        try:
            return browse(path)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(f"{PANEL_PATH}/api/state")
    async def panel_state() -> dict[str, Any]:
        return build_snapshot(ctx.layout, ctx.config, ctx.peers)

    @app.get(f"{PANEL_PATH}/api/cluster")
    async def panel_cluster(request: Request) -> dict[str, Any]:
        """总控视图：本机 + 所有已信任对端。连不上的标成不可达，不卡住整页。

        **只允许本机访问**，和写入口同一条理由：这个 GET 是有副作用的
        （每次可能触发 N 次对外连接），而且它把**所有**对端的状态汇到一处 ——
        面板绑了 `0.0.0.0` 时，那等于把整个集群的状态摊给整个网段。
        页面拿到 403 会自动退回只看本机。
        """
        local_only(request, what="总控视图")
        return await build_cluster(
            ctx.layout, ctx.config, ctx.peers, log, request.app.state.cluster_cache
        )

    @app.get(f"{PANEL_PATH}/api/chats")
    async def panel_chats(request: Request) -> dict[str, Any]:
        """最近的会话列表。和总控视图同样只对本机开放 —— 这是对话内容。"""
        local_only(request)
        return {"threads": chat_threads(ctx.layout)}

    @app.get(f"{PANEL_PATH}/api/chat/{{thread}}")
    async def panel_chat(request: Request, thread: str) -> dict[str, Any]:
        local_only(request)
        if not is_valid_id(thread):
            raise HTTPException(status_code=400, detail="thread 不是合法 ULID")
        return {"thread": thread, "messages": chat_messages(ctx.layout, thread)}

    @app.websocket(f"{PANEL_PATH}/ws")
    async def panel_ws(websocket: WebSocket) -> None:
        """定时推快照。做成推送而不是让页面轮询，是为了 kill -9 之后状态能立刻变灰。"""
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(build_snapshot(ctx.layout, ctx.config, ctx.peers))
                await asyncio.sleep(PANEL_REFRESH)
        except (WebSocketDisconnect, RuntimeError):
            return  # 页面关了，正常退出


def mount_panel_actions(app: FastAPI, *, ctx: NodeContext, log: EventLog) -> None:
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
        return await _acted(start_run(ctx.layout, ctx.config, body, log))

    @app.post(f"{PANEL_PATH}/api/send", status_code=202)
    async def panel_send(request: Request, body: SendRequest = Body(...)) -> dict[str, Any]:
        _guard(request)
        return await _acted(send_message(ctx.layout, ctx.config, body, log))

    @app.post(f"{PANEL_PATH}/api/pair/open", status_code=201)
    async def panel_pair_open(request: Request) -> dict[str, Any]:
        """在本机开一个配对窗口，把 PIN 显示在页面上让对方输。"""
        _guard(request)
        code = new_pin()
        PairingStore(ctx.layout.root).open(code)
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
                base=pair_resolve(ctx.peers, body.target),
                my_node=ctx.config.node.name,
                my_endpoint=ctx.config.node.endpoint,
                pin=body.pin,
                peers=ctx.peers,
            )
        except PeerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "node": record.node, "fingerprint": record.fingerprint}

    @app.post(f"{PANEL_PATH}/api/setup/adopt", status_code=201)
    async def panel_adopt(request: Request, body: WorkspaceSpec = Body(...)) -> dict[str, Any]:
        """认下一个工作区：本进程还没配好就现场接管，已经配好了就只是记进清单。

        **只能从「未配置」走到「已配置」**，不支持中途换 —— peers 与密钥都跟着
        工作区走，换一次等于换身份，已经跑着的东西全对不上。想换就重启 serve。
        """
        _guard(request)
        try:
            created = create_workspace_entry(body)
            if not ctx.ready:
                ctx.adopt(NodeLayout(Path(str(created["path"]))), node_name=body.node_name)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("panel.workspace_adopted", path=str(created["path"]))
        return {**created, "ready": ctx.ready, "node": ctx.node_name}

    @app.delete(f"{PANEL_PATH}/api/setup/workspace")
    async def panel_workspace_delete(
        request: Request, path: str, purge: bool = False
    ) -> dict[str, Any]:
        """默认只从清单里移除；`purge=true` 才真的删 `.anthill/`（会带走密钥与邮箱）。"""
        _guard(request)
        if ctx.ready and Path(path).expanduser().resolve() == ctx.layout.workspace:
            raise HTTPException(status_code=400, detail="这就是当前 serve 在用的工作区，删不得")
        try:
            return delete_workspace(path, purge=purge)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(f"{PANEL_PATH}/api/agents", status_code=201)
    async def panel_agent_add(request: Request, body: AgentSpec = Body(...)) -> dict[str, Any]:
        """加一个 Agent。改的是 node.toml，所以走和手改配置一样的校验与备份。"""
        _guard(request)
        return _agent_edit(ctx.layout, log, lambda fresh: add_agent(ctx.layout, fresh, body))

    @app.delete(f"{PANEL_PATH}/api/agents/{{name}}")
    async def panel_agent_remove(request: Request, name: str) -> dict[str, Any]:
        _guard(request)
        return _agent_edit(ctx.layout, log, lambda fresh: remove_agent(ctx.layout, fresh, name))

    @app.post(f"{PANEL_PATH}/api/agents/{{name}}/start", status_code=202)
    async def panel_agent_start(request: Request, name: str) -> dict[str, Any]:
        """在本机把这个 agentd 拉起来 —— 单机场景下这是最后一处非用终端不可的事。"""
        _guard(request)
        fresh = Config.load_from(ctx.layout)  # 可能刚在面板上加过 Agent
        try:
            result = start_agent(ctx.layout, fresh, name)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("panel.agent_started", agent=name, pid=str(result.get("pid", "")))
        return result

    @app.post(f"{PANEL_PATH}/api/agents/{{name}}/stop", status_code=202)
    async def panel_agent_stop(request: Request, name: str) -> dict[str, Any]:
        _guard(request)
        try:
            result = stop_agent(ctx.layout, name)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("panel.agent_stopped", agent=name)
        return result

    @app.get(f"{PANEL_PATH}/api/config")
    async def panel_config(request: Request, node: str = "") -> dict[str, Any]:
        """`?node=` 指定哪台机器；留空就是本机。"""
        _guard(request)
        if node and node != ctx.config.node.name:
            return await _remote(remote_read_config(ctx.config, ctx.peers, node))
        try:
            return {"node": ctx.config.node.name, **read_config(ctx.layout)}
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"读不出配置：{exc}") from exc

    @app.put(f"{PANEL_PATH}/api/config")
    async def panel_config_write(
        request: Request, body: RemoteConfigWrite = Body(...)
    ) -> dict[str, Any]:
        _guard(request)
        if body.node and body.node != ctx.config.node.name:
            return await _remote(remote_write_config(ctx.config, ctx.peers, body.node, body.text))
        try:
            result = write_config(ctx.layout, ConfigRequest(text=body.text))
        except AntHillError as exc:
            # 配置不合法就原样退回，磁盘一个字都不改
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ensure_mailboxes(ctx.layout, Config.load_from(ctx.layout))  # 手改着加了 Agent 也算
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


def _agent_edit(
    layout: NodeLayout, log: EventLog, build: Callable[[Config], dict[str, Any]]
) -> dict[str, Any]:
    """加/删 Agent 都是「算出新的 node.toml 文本，再走原来那条写配置的路」。

    这样校验、备份、「不合法就磁盘一个字不动」三件事只有一份实现。
    """
    try:
        fresh = Config.load_from(layout)  # 别拿启动时那份，中间可能被改过
        result = build(fresh)
        written = write_config(layout, ConfigRequest(text=str(result.pop("text"))))
    except AntHillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 配置里有它就该能收它的信，别等那个 agentd 第一次启动
    ensure_mailboxes(layout, Config.load_from(layout))
    log.info("panel.agents_changed", agent=str(result.get("name", "")))
    return {**result, **written}


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
