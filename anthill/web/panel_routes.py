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
from starlette.requests import HTTPConnection

from anthill.core.config import Config
from anthill.core.errors import AntHillError, PeerError
from anthill.core.ids import is_valid_id
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.traffic import conversations
from anthill.core.traffic_purge import doomed as doomed_traffic
from anthill.core.traffic_purge import purge as purge_traffic
from anthill.core.workspace import ensure_mailboxes
from anthill.security import secrets
from anthill.security.pair_client import join as pair_join
from anthill.security.pair_client import resolve as pair_resolve
from anthill.security.pairing import WINDOW_SECONDS, PairingStore, new_pin
from anthill.security.panel_token import matches, presented
from anthill.web.actions import (
    ConfigRequest,
    RunRequest,
    SendRequest,
    StaleConfigBase,
    is_local_client,
    is_same_origin,
    read_config,
    send_message,
    start_run,
    write_config,
)
from anthill.web.agents import AgentSpec, add_agent, agent_op, remove_agent, start_agent, stop_agent
from anthill.web.bridge_panel import BridgeReply
from anthill.web.bridge_panel import inbox as bridge_inbox
from anthill.web.bridge_panel import reply as bridge_reply
from anthill.web.bridge_panel import speak as bridge_speak
from anthill.web.chat import messages as chat_messages
from anthill.web.chat import recorded as chat_recorded
from anthill.web.chat import threads as chat_threads
from anthill.web.cluster import build_cluster
from anthill.web.context import NodeRegistry
from anthill.web.endpoints import PANEL_PATH
from anthill.web.panel import build_snapshot
from anthill.web.providers import PRESETS, ProviderSpec, SecretSpec, add_provider, remove_provider
from anthill.web.providers import listing as provider_listing
from anthill.web.remote import control_agent as remote_control_agent
from anthill.web.remote import read_config as remote_read_config
from anthill.web.remote import write_config as remote_write_config
from anthill.web.setup import browse
from anthill.web.setup import home as setup_home
from anthill.web.workspaces import WorkspaceSpec
from anthill.web.workspaces import clear as clear_workspaces
from anthill.web.workspaces import create as create_workspace_entry
from anthill.web.workspaces import delete as delete_workspace
from anthill.web.workspaces import doomed as doomed_workspaces
from anthill.web.workspaces import listing as list_workspaces

PANEL_HTML = Path(__file__).parent / "static" / "panel.html"
PANEL_REFRESH = 2.0
WS_POLICY_VIOLATION = 1008
"""RFC 6455 的「策略违规」—— 关 WebSocket 时用它，别用普通关闭码糊弄过去。"""


def denial(conn: HTTPConnection, token: str, *, what: str = "这个接口") -> HTTPException | None:
    """两条路任选其一：**连接来自本机**，或者**带着有效的面板令牌**。都不成立就返回该报的错。

    只认回环的话，一台没有显示器的机器就永远操作不了 —— 你没法在它上面开浏览器。
    令牌是那台机器自己生成的（`--panel-token`），分量等同于「能在它上面执行命令」。

    跨站检查一直都在：即使请求确实来自本机，发起它的如果是别的站点也不放行。

    收 `HTTPConnection` 而不是 `Request`，是因为 **WebSocket 也得走这一套**。
    以前它收 `Request`，于是 WS 那条路根本没法复用，就这么一直敞着 —— 见 `panel_ws`。
    """
    if not is_same_origin(conn.headers.get("origin"), conn.headers.get("host")):
        return HTTPException(status_code=403, detail=f"拒绝跨站访问{what}")
    server = conn.scope.get("server")
    if is_local_client(
        conn.client.host if conn.client else None,
        server[0] if server else None,
    ):
        return None
    given = presented(
        {k.lower(): v for k, v in conn.headers.items()},
        dict(conn.cookies),
        conn.query_params.get("token", ""),
    )
    if token and matches(given, token):
        return None
    return HTTPException(
        status_code=401 if token else 403,
        detail=(
            f"{what}需要面板令牌 —— 那台机器上用 `anthill serve --panel-token` 起，"
            "把打印出来的令牌填进面板"
            if token
            else f"{what}只允许本机访问；没有显示器的机器请用 --panel-token 起"
        ),
    )


def authorize(conn: HTTPConnection, token: str, *, what: str = "这个接口") -> None:
    refusal = denial(conn, token, what=what)
    if refusal is not None:
        raise refusal


def _pick(nodes: NodeRegistry, name: str) -> Any:
    """面板上的 `?node=` 指的是**本机**哪个节点；不给就是主节点。"""
    if not nodes.ready:
        raise HTTPException(status_code=409, detail="本节点还没配好工作区")
    ctx = nodes.get(name) if name else nodes.primary
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"本机没有名为 {name!r} 的节点")
    return ctx


def mount_panel(app: FastAPI, *, nodes: NodeRegistry, log: EventLog, token: str = "") -> None:
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
        authorize(request, token, what="设置界面")
        return {
            "ready": nodes.ready,
            "node": nodes.primary_name,
            "nodes": [{"node": c.name, "workspace": str(c.layout.workspace)} for c in nodes.all()],
            "workspace": str(nodes.primary.layout.workspace) if nodes.ready else "",
            "home": str(setup_home()),
            "workspaces": list_workspaces(
                [c.layout.workspace for c in nodes.all()],
                nodes.primary.layout.workspace if nodes.ready else None,
            ),
        }

    @app.get(f"{PANEL_PATH}/api/setup/browse")
    async def panel_browse(request: Request, path: str = "") -> dict[str, Any]:
        """挑工作区放哪用的目录浏览器。只列目录，只对本机开放。"""
        authorize(request, token, what="目录浏览")
        try:
            return browse(path)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(f"{PANEL_PATH}/api/bridge/{{agent}}")
    async def panel_bridge(request: Request, agent: str, node: str = "") -> dict[str, Any]:
        """桥接 Agent 那边在等什么 —— 加它的地方和用它的地方该是同一个地方。"""
        authorize(request, token, what="桥接收件箱")
        ctx = _pick(nodes, node)
        try:
            return bridge_inbox(ctx.layout, ctx.config, agent)
        except AntHillError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get(f"{PANEL_PATH}/api/traffic")
    async def panel_traffic(request: Request, node: str = "") -> dict[str, Any]:
        """Agent 之间聊了什么 —— 消息流有元数据没正文，这一页补上正文。

        只读，所以不要求写权限：看见发生过什么本来就不是危险动作。
        """
        authorize(request, token, what="往来记录")
        ctx = _pick(nodes, node)
        # 「谁是人的代理」由 role 判断，不是硬编码 "cli" —— 那个名字是可以改的
        humans = frozenset(
            name for name, agent in ctx.config.agents.items() if agent.role == "user"
        )
        # 本机记的发件兜住「刚发出去、对方还没归档」那个空档 —— 对方没启动时
        # 那个空档是**永远的**，不补的话页面上看着就像消息丢了
        return {
            **conversations(ctx.layout, humans=humans, extra=chat_recorded(ctx.layout)),
            # 页面靠它认出「哪一头是我」，好把发送框的收件人填成另一头。
            # 别让前端硬编码 "cli" —— 那个名字是可以改的
            "humans": sorted(humans),
        }

    @app.get(f"{PANEL_PATH}/api/state")
    async def panel_state(request: Request, node: str = "") -> dict[str, Any]:
        """`?node=` 指本机哪个节点；一台机器可以照看好几个。

        **这也要认证。** 它给出的是编排任务的正文和最近的日志 ——
        和 `/node/summary` 给对端看的是同一批东西，那边要签名，这边不该裸奔。
        以前它是敞着的（面板绑 0.0.0.0 时给网段上的人留个只读视图），
        现在有了令牌就没必要再留这个口子。
        """
        authorize(request, token, what="节点状态")
        ctx = _pick(nodes, node)
        return build_snapshot(ctx.layout, ctx.config, ctx.peers)

    @app.get(f"{PANEL_PATH}/api/cluster")
    async def panel_cluster(request: Request) -> dict[str, Any]:
        """总控视图：本机 + 所有已信任对端。连不上的标成不可达，不卡住整页。

        **只允许本机访问**，和写入口同一条理由：这个 GET 是有副作用的
        （每次可能触发 N 次对外连接），而且它把**所有**对端的状态汇到一处 ——
        面板绑了 `0.0.0.0` 时，那等于把整个集群的状态摊给整个网段。
        页面拿到 403 会自动退回只看本机。
        """
        authorize(request, token, what="总控视图")
        if not nodes.ready:
            return {"node": nodes.primary_name, "nodes": []}
        # 本机照看的每个节点都是一格，各自再带上它自己信任的对端
        views = [
            await build_cluster(c.layout, c.config, c.peers, log, request.app.state.cluster_cache)
            for c in nodes.all()
        ]
        # **本机那一格优先。** 以前是先到先得：主节点的视图排在最前，而它的
        # peers 里可能有一条同名记录（同一台机器上的另一个工作区被组播「发现」了），
        # 于是本机的第二个节点在页面上显示成「连不上的对端」—— 侧栏里点都点不了。
        merged: dict[str, dict[str, Any]] = {}
        for view in views:
            for entry in view["nodes"]:
                seen = merged.get(entry["node"])
                if seen is None or (entry.get("local") and not seen.get("local")):
                    merged[entry["node"]] = entry
        return {"node": nodes.primary_name, "nodes": list(merged.values())}

    @app.get(f"{PANEL_PATH}/api/chats")
    async def panel_chats(request: Request, node: str = "") -> dict[str, Any]:
        """最近的会话列表。和总控视图同样只对本机开放 —— 这是对话内容。"""
        authorize(request, token)
        return {"threads": chat_threads(_pick(nodes, node).layout)}

    @app.get(f"{PANEL_PATH}/api/chat/{{thread}}")
    async def panel_chat(request: Request, thread: str, node: str = "") -> dict[str, Any]:
        authorize(request, token)
        if not is_valid_id(thread):
            raise HTTPException(status_code=400, detail="thread 不是合法 ULID")
        return {"thread": thread, "messages": chat_messages(_pick(nodes, node).layout, thread)}

    @app.websocket(f"{PANEL_PATH}/ws")
    async def panel_ws(websocket: WebSocket) -> None:
        """定时推快照。做成推送而不是让页面轮询，是为了 kill -9 之后状态能立刻变灰。

        **这里也要认证。** 推的是 `build_snapshot()` —— 和 `/api/state` 一模一样的东西：
        编排任务的正文、最近的日志、对端清单。REST 那边把口子堵上了，这边曾经是敞着的：
        `accept()` 之前一行检查都没有，令牌配了也白配。

        两个后果都真实存在：`--host 0.0.0.0` 时「写操作只对本机开放」的承诺在读方向被绕过；
        而且 **WebSocket 不受同源策略约束**，你浏览器里随便打开一个网页，那个页面
        `new WebSocket("ws://127.0.0.1:45778/panel/ws")` 就能一直读走整个节点状态 ——
        默认的回环配置一样中招。同源检查（浏览器仍会带 Origin）正是挡住后者的那道。
        """
        refusal = denial(websocket, token, what="实时快照")
        if refusal is not None:
            await websocket.close(code=WS_POLICY_VIOLATION, reason=str(refusal.detail)[:120])
            return
        await websocket.accept()
        try:
            while True:
                ctx = nodes.primary if nodes.ready else None
                if ctx is None:
                    await asyncio.sleep(PANEL_REFRESH)
                    continue
                await websocket.send_json(build_snapshot(ctx.layout, ctx.config, ctx.peers))
                await asyncio.sleep(PANEL_REFRESH)
        except (WebSocketDisconnect, RuntimeError):
            return  # 页面关了，正常退出


def mount_panel_actions(
    app: FastAPI, *, nodes: NodeRegistry, log: EventLog, token: str = ""
) -> None:
    """面板的写入口（`anthill serve --panel-write` 才挂）。

    **逐请求校验来源是回环**，而不是依赖「我们绑的是 127.0.0.1 所以应该安全」——
    反向代理、端口转发、配置写错，任何一种都会让那个假设悄悄失效。
    能改配置 ≈ 能在这台机器上执行命令，这个前提值得多一道显式检查。

    确认与审批仍然只在 CLI：面板能发起任务，但不能替你批准危险操作。
    """

    def _guard(request: Request) -> None:
        authorize(request, token, what="面板的写操作")

    @app.get(f"{PANEL_PATH}/api/can-write")
    async def panel_can_write(request: Request) -> dict[str, Any]:
        """页面靠它判断「写入口挂着没有」。

        专门开一个端点，是因为拿别的路由来探会把两件事混起来：
        **没有写权限**（路由压根不存在，404）和**还没配工作区**（409）。
        混了的话，一台全新机器上会判成「不能写」，于是那个专门给
        「还没有工作区」准备的设置界面反而永远出不来 —— 死路一条。
        这个端点不看节点状态，只回答「你能不能写」。
        """
        _guard(request)
        return {"ok": True}

    @app.post(f"{PANEL_PATH}/api/run", status_code=202)
    async def panel_run(
        request: Request, body: RunRequest = Body(...), node: str = ""
    ) -> dict[str, Any]:
        _guard(request)
        ctx = _pick(nodes, node)
        return await _acted(start_run(ctx.layout, ctx.config, body, log))

    @app.post(f"{PANEL_PATH}/api/send", status_code=202)
    async def panel_send(
        request: Request, body: SendRequest = Body(...), node: str = ""
    ) -> dict[str, Any]:
        """`?node=` 是**以本机哪个节点的身份**发；收件人写在 body.to 里。"""
        _guard(request)
        ctx = _pick(nodes, node)
        return await _acted(send_message(ctx.layout, ctx.config, body, log))

    @app.post(f"{PANEL_PATH}/api/pair/open", status_code=201)
    async def panel_pair_open(request: Request, node: str = "") -> dict[str, Any]:
        """在本机某个节点上开一个配对窗口，把 PIN 显示在页面上让对方输。"""
        _guard(request)
        ctx = _pick(nodes, node)
        code = new_pin()
        PairingStore(ctx.layout.root).open(code)
        log.info("pair.window_opened", node=ctx.name)
        return {"pin": code, "seconds": int(WINDOW_SECONDS), "node": ctx.name}

    @app.post(f"{PANEL_PATH}/api/pair/join", status_code=201)
    async def panel_pair_join(
        request: Request, body: PairJoinRequest = Body(...)
    ) -> dict[str, Any]:
        """输入对方屏幕上的 PIN，把密钥换过来。"""
        _guard(request)
        ctx = _pick(nodes, body.as_node)
        try:
            record = await pair_join(
                base=pair_resolve(ctx.peers, body.target),
                my_node=ctx.config.node.name,
                my_endpoint=ctx.config.node.endpoint,
                pin=body.pin,
                peers=ctx.peers,
                # 对方一台机器好几个节点：窗口开在谁头上就指名谁。
                # target 是裸地址时无从得知节点名，维持主节点旧行为。
                for_node="" if body.target.startswith(("http://", "https://")) else body.target,
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
            attached = nodes.attach(
                NodeLayout(Path(str(created["path"]))), node_name=body.node_name
            )
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("panel.workspace_attached", node=attached.name, path=str(created["path"]))
        return {**created, "ready": nodes.ready, "node": attached.name}

    @app.delete(f"{PANEL_PATH}/api/setup/workspace")
    async def panel_workspace_delete(
        request: Request, path: str, purge: bool = False
    ) -> dict[str, Any]:
        """默认只从清单里移除；`purge=true` 才真的删 `.anthill/`（会带走密钥与邮箱）。"""
        _guard(request)
        wanted = Path(path).expanduser().resolve()
        held = next((c for c in nodes.all() if c.layout.workspace == wanted), None)
        if held is not None and held.name == nodes.primary_name:
            raise HTTPException(status_code=400, detail="这是本进程的主节点，删不得（重启换一个）")
        try:
            if held is not None:
                nodes.detach(held.name)  # 先不再照看它，再动盘
            return delete_workspace(path, purge=purge)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(f"{PANEL_PATH}/api/traffic/doomed")
    async def panel_traffic_doomed(
        request: Request, node: str = "", thread: str = ""
    ) -> dict[str, Any]:
        """清对话记录会碰到哪些 —— **先给人看，再动手**。`thread` 留空 = 全部。"""
        authorize(request, token, what="对话记录")
        return doomed_traffic(_pick(nodes, node).layout, thread=thread).as_dict()

    @app.delete(f"{PANEL_PATH}/api/traffic")
    async def panel_traffic_purge(
        request: Request,
        node: str = "",
        thread: str = "",
        drop_pending: bool = False,
        expect: int = -1,
    ) -> dict[str, Any]:
        """删对话记录。

        默认**只删归档**（处理完的那些）。`inbox/new/` 里还没被处理的是**实信**，
        删了就是丢件 —— 要一起删得显式 `drop_pending=true`。
        正在被处理的（`cur/`）任何情况下都不碰。

        `expect` 必须等于刚才 `/doomed` 给出的条数：中间新到了一条消息这一次就作废，
        不会顺手把它带走。**网页上的一次误点没有 undo。**
        """
        _guard(request)
        try:
            result = purge_traffic(
                _pick(nodes, node).layout,
                thread=thread,
                drop_pending=drop_pending,
                expect=None if expect < 0 else expect,
            )
        except AntHillError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        log.info("panel.traffic_purged", thread=thread or "*", removed=result["removed"])
        return result

    @app.get(f"{PANEL_PATH}/api/setup/workspaces/doomed")
    async def panel_workspaces_doomed(request: Request, stale_only: bool = False) -> dict[str, Any]:
        """这次清理会碰到哪些 —— **先给人看，再动手**。

        批量删除最该有的一道闸不是「你确定吗」，而是「你确定要删**这几个**吗」。
        前端拿到这份名单，把数目跟着删除请求回传；对不上就整个拒绝。
        """
        authorize(request, token, what="工作区清单")
        paths = doomed_workspaces(
            keep=[c.layout.workspace for c in nodes.all()], stale_only=stale_only
        )
        return {"paths": paths, "count": len(paths)}

    @app.delete(f"{PANEL_PATH}/api/setup/workspaces")
    async def panel_workspaces_clear(
        request: Request, stale_only: bool = False, purge: bool = False, expect: int = -1
    ) -> dict[str, Any]:
        """清清单；`purge=true` 连 `.anthill/` 一起删（带走邮箱、黑板、密钥）。

        只删 `.anthill/`，**不碰你放在那个目录里的别的东西** —— 和单个删除同一条规矩。

        `expect` 必须等于刚才 `/doomed` 给出的数目：中间有人加了/删了工作区，
        这一次就作废，不会误伤。**网页上的一次误点没有 undo**，所以这道闸不是可选的。

        本进程正照看着的那些永远留下 —— 把自己删掉，面板下一秒就找不着自己了。
        """
        _guard(request)
        try:
            result = clear_workspaces(
                keep=[c.layout.workspace for c in nodes.all()],
                stale_only=stale_only,
                purge=purge,
                expect=None if expect < 0 else expect,
            )
        except AntHillError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # 删文件是要留痕的：谁都可能第二天问「我那个工作区呢」
        log.warn(
            "panel.workspaces_cleared",
            removed=result["removed"],
            purged=len(result["purged"]),
            failed=len(result["failed"]),
            stale_only=stale_only,
        )
        for path in result["purged"]:
            log.warn("panel.workspace_purged", path=path)
        return result

    @app.post(f"{PANEL_PATH}/api/bridge/{{agent}}/reply/{{msg_id}}", status_code=201)
    async def panel_bridge_reply(
        request: Request, agent: str, msg_id: str, body: BridgeReply = Body(...), node: str = ""
    ) -> dict[str, Any]:
        """在页面上替这个人回一句。写的还是 outbox 里那个文件，
        所以和盯着目录的 Claude Code 会话完全并存。"""
        _guard(request)
        ctx = _pick(nodes, node)
        try:
            return bridge_reply(ctx.layout, ctx.config, agent, msg_id, body)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(f"{PANEL_PATH}/api/bridge/{{agent}}/speak", status_code=201)
    async def panel_bridge_speak(
        request: Request, agent: str, body: BridgeReply = Body(...), node: str = ""
    ) -> dict[str, Any]:
        """以这个桥接 Agent 的身份主动说一句 —— 人随时插进正在进行的协作里。"""
        _guard(request)
        ctx = _pick(nodes, node)
        try:
            return bridge_speak(ctx.layout, ctx.config, agent, body)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(f"{PANEL_PATH}/api/providers")
    async def panel_providers(request: Request, node: str = "") -> dict[str, Any]:
        """有哪些 provider，以及它们的 key 到底设没设。

        「设没设」是这一页最要紧的一格 —— 配好了 provider 却没设 key 的话，
        agentd 一启动就 fail fast，而人在面板上完全看不出为什么。
        """
        authorize(request, token, what="模型配置")
        return {
            "providers": provider_listing(_pick(nodes, node).config),
            "presets": PRESETS,
        }

    @app.post(f"{PANEL_PATH}/api/providers", status_code=201)
    async def panel_provider_add(
        request: Request, body: ProviderSpec = Body(...), node: str = ""
    ) -> dict[str, Any]:
        """加一个 provider。**这是「面板上建不出能干活的 Agent」那堵墙的另一半**：
        选 provider 大脑要求 [providers.*] 已存在，而以前面板没有任何地方能配它。"""
        _guard(request)
        ctx = _pick(nodes, node)
        return _agent_edit(ctx.layout, log, lambda fresh: add_provider(ctx.layout, fresh, body))

    @app.delete(f"{PANEL_PATH}/api/providers/{{name}}")
    async def panel_provider_remove(request: Request, name: str, node: str = "") -> dict[str, Any]:
        _guard(request)
        ctx = _pick(nodes, node)
        return _agent_edit(ctx.layout, log, lambda fresh: remove_provider(ctx.layout, fresh, name))

    @app.post(f"{PANEL_PATH}/api/secrets", status_code=201)
    async def panel_secret_set(request: Request, body: SecretSpec = Body(...)) -> dict[str, Any]:
        """存一个密钥到 `~/.anthill/secrets.env`（0600）。

        **值只进不出** —— 没有任何读接口会回传它。node.toml 里仍然只有变量名，
        那条规矩没动；密钥和 peers.json 同一个待遇：家目录、0600、不进工作区。
        """
        _guard(request)
        try:
            secrets.set_secret(body.name, body.value)
        except AntHillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.warn("panel.secret_set", name=body.name)  # 记名字，不记值
        return {"ok": True, "name": body.name}

    @app.delete(f"{PANEL_PATH}/api/secrets/{{name}}")
    async def panel_secret_unset(request: Request, name: str) -> dict[str, Any]:
        _guard(request)
        removed = secrets.unset_secret(name)
        if removed:
            log.warn("panel.secret_unset", name=name)
        return {"ok": True, "removed": removed}

    @app.post(f"{PANEL_PATH}/api/agents", status_code=201)
    async def panel_agent_add(
        request: Request, body: AgentSpec = Body(...), node: str = ""
    ) -> dict[str, Any]:
        """加一个 Agent。改的是 node.toml，所以走和手改配置一样的校验与备份。"""
        _guard(request)
        ctx = _pick(nodes, node)
        return _agent_edit(ctx.layout, log, lambda fresh: add_agent(ctx.layout, fresh, body))

    @app.delete(f"{PANEL_PATH}/api/agents/{{name}}")
    async def panel_agent_remove(request: Request, name: str, node: str = "") -> dict[str, Any]:
        _guard(request)
        ctx = _pick(nodes, node)
        try:
            # 删除和启停一样不许与别的操作交错
            with agent_op(ctx.name, name):
                return _agent_edit(
                    ctx.layout, log, lambda fresh: remove_agent(ctx.layout, fresh, name)
                )
        except AntHillError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete(f"{PANEL_PATH}/api/peers/{{name}}")
    async def panel_peer_forget(request: Request, name: str) -> dict[str, Any]:
        """把一个「见过还没配对」的对端从清单里移掉 —— 侧栏里那排连不上的
        发现残留，面板上看得见就该在面板上能扫。

        不带 `?node=`：同一个幽灵会被本机**每个**工作区各记一笔（组播是
        广播，谁都听见了），只清一家、侧栏合并视图里它还在。所以一次全清。

        **已信任的对端不让删**：那等于丢掉密钥、拒收它今后的一切消息 ——
        这一刀和审批一样留在 CLI（`anthill peers forget`）。
        """
        _guard(request)
        found = [c for c in nodes.all() for p in c.peers.all() if p.node == name]
        if not found:
            raise HTTPException(status_code=404, detail=f"清单里没有对端 {name}")
        if any(p.trusted for c in nodes.all() for p in c.peers.all() if p.node == name):
            raise HTTPException(
                status_code=409,
                detail=f"{name} 是已信任的对端 —— 删它要在终端里：anthill peers forget {name}",
            )
        removed = [c.name for c in nodes.all() if c.peers.forget(name)]
        log.info("panel.peer_forgot", peer=name, nodes=",".join(removed))
        return {"removed": removed}

    @app.post(f"{PANEL_PATH}/api/agents/{{name}}/{{action}}", status_code=202)
    async def panel_agent_control(
        request: Request, name: str, action: str, node: str = ""
    ) -> dict[str, Any]:
        """启停 agentd。`?node=` 可以是本机的某个节点，也可以是某台远端。

        远端那一档走的是和「改它的配置」同一道闸（对方的 `remote_admin`）——
        能改 node.toml 本来就约等于能在那台机器上执行命令，不必再分一级。
        """
        _guard(request)
        if action not in ("start", "stop"):
            raise HTTPException(status_code=404, detail=f"不认识的动作 {action!r}")
        if node and nodes.get(node) is None:
            speaker = nodes.speaker_for(node)
            return await _remote(
                remote_control_agent(speaker.config, speaker.peers, node, name, action)
            )
        ctx = _pick(nodes, node)
        try:
            # 同一只 Agent 的操作串行 —— 双击/两个页面同时点会互相顶（409 而不是竞态）
            with agent_op(ctx.name, name):
                result = (
                    start_agent(ctx.layout, ctx.config, name)
                    if action == "start"
                    else stop_agent(ctx.layout, name)
                )
        except AntHillError as exc:
            busy = "正在操作中" in str(exc)
            raise HTTPException(status_code=409 if busy else 400, detail=str(exc)) from exc
        log.info(f"panel.agent_{action}", node=ctx.name, agent=name)
        return result

    @app.get(f"{PANEL_PATH}/api/config")
    async def panel_config(request: Request, node: str = "") -> dict[str, Any]:
        """`?node=` 指定哪台机器；留空就是本机。"""
        _guard(request)
        if node and nodes.get(node) is None:
            speaker = nodes.speaker_for(node)
            return await _remote(remote_read_config(speaker.config, speaker.peers, node))
        ctx = _pick(nodes, node)
        # 文件被改坏时 ctx.name（活配置）会在重读时抛解析错 —— 而这一页
        # 恰恰是修坏文件的地方，名字退回注册名，原文照常给
        try:
            name = ctx.name
        except Exception:
            name = node or nodes.primary_name
        try:
            return {"node": name, **read_config(ctx.layout)}
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"读不出配置：{exc}") from exc

    @app.put(f"{PANEL_PATH}/api/config")
    async def panel_config_write(
        request: Request, body: RemoteConfigWrite = Body(...)
    ) -> dict[str, Any]:
        _guard(request)
        if body.node and nodes.get(body.node) is None:
            speaker = nodes.speaker_for(body.node)
            return await _remote(
                remote_write_config(speaker.config, speaker.peers, body.node, body.text)
            )
        ctx = _pick(nodes, body.node)
        try:
            result = write_config(
                ctx.layout, ConfigRequest(text=body.text, base_text=body.base_text)
            )
        except StaleConfigBase as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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

    as_node: str = Field(default="", max_length=64)
    """以本机哪个节点的身份配对。留空 = 主节点。"""

    pin: str = Field(min_length=4, max_length=16)


class RemoteConfigWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=200_000)
    base_text: str = Field(default="", max_length=200_000)
    """保存者对着看 diff 的那版磁盘原文；不匹配 409。远端写入不带这道闸。"""
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
