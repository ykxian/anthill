"""面板的写入口：发起任务、发消息、改配置。

**为什么这部分要特别小心**：能改 node.toml 就等于能加一个带 `run_shell` 的 Agent ——
写权限的实际威力等同于「在这台机器上执行命令」。所以设了两道闸，缺一不可：

1. 显式开关（`anthill serve --panel-write`），默认关；
2. **每个请求**都校验来源是回环地址 —— 不是「我们没绑 0.0.0.0 所以应该安全」，
   而是真的逐个请求检查。反向代理、端口转发、配置写错，任何一种都可能让
   「应该只有本机能访问」这个假设悄悄失效。

确认与审批仍然只在 CLI：面板可以发起任务，但不能替你批准危险操作。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anthill.agent.sender import Sender
from anthill.core.atomic import atomic_write
from anthill.core.config import Config
from anthill.core.envelope import Address
from anthill.core.errors import AntHillError
from anthill.core.ids import new_thread_id
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType, TaskRequestPayload
from anthill.core.router import Router, parse_address
from anthill.core.states import DeliveryTracker
from anthill.transport.registry import TransportRegistry
from anthill.web.chat import record_outgoing

CLI_AGENT = "cli"
COORDINATOR_ROLE = "coordinator"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
CONFIG_BACKUP = "node.toml.bak"
MAX_BODY_CHARS = 32_000


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    to: str = ""
    """留空则自动找 role=coordinator 的那个。"""


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    kind: str = "chat"
    mentions: tuple[str, ...] = ()
    thread: str = Field(default="", max_length=64)
    """接着已有会话往下说；留空则新开一个。对方的 thread 记忆靠这个接上。"""


class ConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=200_000)


def is_local_client(host: str | None, server_host: str | None = None) -> bool:
    """请求是不是来自本机。逐请求判，不依赖「我们绑的是回环」这个假设。

    判据是 TCP 连接的对端地址，**不看任何 HTTP 头** —— 头是客户端说了算的，
    拿它当授权依据等于没有授权。uvicorn 那边也显式关掉了 proxy_headers，
    免得 `X-Forwarded-For` 有机会影响这个值。

    回环之外还认一种：**对端地址 == 这条连接的本端地址**（`server_host` 来自
    ASGI scope，是内核给这条连接的本地套接字地址，不是任何头）。在本机浏览器里
    打开本机自己的局域网 IP 就是这种 —— 包没出过这台机器，而伪造这个源地址
    无法完成 TCP 握手（SYN-ACK 会回到本机自己）。不认它的话，serve 自己
    打印的面板地址在它自己的机器上打开反而 403 —— Windows 实机第一步就栽这。

    有意的边界决策：经**本机 NAT 出口**的客体（VirtualBox NAT 虚拟机、部分
    容器网络）源地址也是本机自有 IP，同样会被算作本机 —— 它们是同一台
    物理机上的软件，符合「挡的是 LAN 上其他主机」的威胁模型。
    """
    peer = (host or "").strip().lower()
    if peer in LOOPBACK_HOSTS:
        return True
    own = (server_host or "").strip().lower()
    return bool(peer) and peer == own


def is_same_origin(origin: str | None, host_header: str | None) -> bool:
    """跨站请求伪造的纵深防御。

    说清楚它挡的**不是**一个现成的洞：写接口只收 JSON body，浏览器发跨源 JSON
    请求前必须先预检，而本服务没有任何 CORS 响应头，预检就过不去 ——
    所以那条路本来就是通不了的。这里只是不想把安全性寄托在那个间接保证上：
    浏览器策略会变，而「同源才放行」是自己能守住的。

    没有 Origin 头就放行 —— curl、脚本、以及同源 GET 本来就不带它，
    而**这些请求还得先过回环那一关**，不是白给。
    """
    if not origin:
        return True
    if not host_header:
        return False
    return origin.split("://", 1)[-1].strip().lower() == host_header.strip().lower()


def find_coordinator(config: Config) -> str:
    names = sorted(a.name for a in config.agents_with_role(COORDINATOR_ROLE))
    if not names:
        raise AntHillError(
            'node.toml 里没有 role = "coordinator" 的 Agent；先配一个，或在请求里指定 to'
        )
    return names[0]


async def start_run(
    layout: NodeLayout, config: Config, request: RunRequest, log: EventLog
) -> dict[str, Any]:
    """把任务交给 coordinator —— 和 `anthill run` 走的是同一条路。"""
    target = request.to or find_coordinator(config)
    env = await _send(
        layout,
        config,
        to=parse_address(target, default_node=config.node.name),
        kind=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title=_clip(request.task), body=request.task),
        log=log,
    )
    log.info("panel.run", msg=env.id, to=target, thread=env.thread)
    return {"ok": True, "id": env.id, "thread": env.thread, "to": target}


async def send_message(
    layout: NodeLayout, config: Config, request: SendRequest, log: EventLog
) -> dict[str, Any]:
    if request.kind not in ("chat", "task"):
        raise AntHillError(f"kind 只能是 chat 或 task，收到 {request.kind!r}")
    payload = (
        TaskRequestPayload(title=_clip(request.body), body=request.body)
        if request.kind == "task"
        else ChatPayload(body=request.body, mentions=request.mentions)
    )
    env = await _send(
        layout,
        config,
        to=parse_address(request.to, default_node=config.node.name),
        kind=MessageType.TASK_REQUEST if request.kind == "task" else MessageType.CHAT,
        payload=payload,
        log=log,
        thread=request.thread,
    )
    record_outgoing(layout, env, request.body)
    log.info("panel.send", msg=env.id, to=request.to, kind=request.kind, thread=env.thread)
    return {"ok": True, "id": env.id, "thread": env.thread}


async def _send(
    layout: NodeLayout,
    config: Config,
    *,
    to: Address,
    kind: MessageType,
    payload: TaskRequestPayload | ChatPayload,
    log: EventLog,
    thread: str = "",
) -> Any:
    """以 cli 这个 Agent 的身份发出去，所以回执与结果会落进它的邮箱、面板能看到。"""
    sender = Sender(
        identity=Address(node=config.node.name, agent=CLI_AGENT),
        mailbox=Mailbox(layout.mailbox_dir(CLI_AGENT)).ensure(),
        router=Router(config, layout),
        transports=TransportRegistry(config, layout),
        tracker=DeliveryTracker(),
        log=log,
    )
    return await sender.send_new(
        to=to, type=kind, payload=payload, thread=thread or new_thread_id()
    )


def read_config(layout: NodeLayout) -> dict[str, Any]:
    """原文 + 解析后的结构 —— 页面拿后者画概览卡片和常用字段表单。

    文件被改坏时更需要打开这一页修：原文照常给，`parsed` 置 null，
    图形视图标不可用就行，别让整页 500。"""
    text = layout.node_toml.read_text(encoding="utf-8")
    try:
        parsed: dict[str, Any] | None = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        parsed = None
    return {"text": text, "parsed": parsed}


def write_config(layout: NodeLayout, request: ConfigRequest) -> dict[str, Any]:
    """校验通过才落盘，并留一份上一版的备份。

    面板上改坏配置的代价是所有 agentd 都起不来，所以这里**先用同一套 pydantic
    模型校验一遍**（和启动期一模一样的规则），不合法就原样退回，磁盘一个字都不改。
    """
    try:
        raw = tomllib.loads(request.text)
    except tomllib.TOMLDecodeError as exc:
        raise AntHillError(f"不是合法的 TOML：{exc}") from exc
    try:
        Config.model_validate({**raw, "source": layout.node_toml})
    except ValueError as exc:
        raise AntHillError(f"配置不合法：{exc}") from exc

    if layout.node_toml.is_file():
        _backup(layout)
    atomic_write(layout.root, layout.root, layout.node_toml.name, request.text.encode("utf-8"))
    return {"ok": True, "backup": CONFIG_BACKUP}


def _backup(layout: NodeLayout) -> Path:
    return atomic_write(layout.root, layout.root, CONFIG_BACKUP, layout.node_toml.read_bytes())


def _clip(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return (flat[:limit] if len(flat) > limit else flat) or "任务"
