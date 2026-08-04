"""总控面板：把各节点的快照汇到一处。

为什么不做成「面板直接去读别人的文件」：跨机没有共享文件系统，
而逐个文件走 SFTP 拉又太碎。所以约定一件事 ——

    **每个节点定期把自己的快照写成 `.anthill/status.json`；
    总控只需要去取那一个文件。**

取法按对端的传输方式走已有的路，不新造轮子：

- LAN peer：`GET /node/summary`（HMAC 签名的请求，和投递用同一把共享密钥）
- SSH peer：SFTP 直接读那个文件（`SshTransport.read_bytes`）

三条不能破的规矩：

1. **拿回来的东西是外部输入**，一律先过 `web/status.py` 的模型再用 ——
   那些字段最终会进面板的 HTML，一个没校验的整数字段就是一次 XSS。
2. **绝不让一个连不上的节点把整个面板卡住**：并发拉取、各自超时；
   已经有旧数据的节点直接返回旧数据、后台去刷，页面一秒都不用等。
3. **缓存**：浏览器每 5 秒轮询，而远端本来也只每 5 秒写一次自己的快照 ——
   每次轮询都去连一遍每台机器（SSH 还得重开连接）纯属浪费，多开一个标签页还翻倍。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from anthill.core.atomic import atomic_write
from anthill.core.config import Config, PeerSection
from anthill.core.envelope import TransportKind
from anthill.core.ids import now
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRecord, PeerRegistry
from anthill.security.signing import sign_request
from anthill.web.endpoints import SUMMARY_PATH
from anthill.web.panel import build_snapshot
from anthill.web.status import NodeStatus

STATUS_FILE = "status.json"
REMOTE_STATUS = f".anthill/{STATUS_FILE}"
STATUS_MODE = 0o600

HTTP_TIMEOUT = 6.0
SSH_TIMEOUT = 25.0
"""SSH 握手比一次 HTTP 请求慢得多（`transport/ssh.py` 给的连接超时就是 20 秒）。
给它和 HTTP 一样的窗，等于把所有握手慢一点的机器永久判成「连不上」。"""

REFRESH_INTERVAL = 10.0
"""远端最快也只每 5 秒写一次快照，比这更勤地去取只是浪费对方的 sshd。"""

FRESH_WINDOW = 90.0
"""`status.json` 超过这个岁数就不能再当「那台机器活着」—— 见 `_grade`。"""

MAX_STATUS_BYTES = 512 * 1024
"""对面放一个几个 G 的文件就能把总控撑爆，所以读之前先封顶。"""


def write_status(layout: NodeLayout, config: Config, peers: PeerRegistry) -> None:
    """把本节点的快照落成一个文件，供别人来取。

    原子写：总控可能正好在读它。权限 0600：共用服务器上，
    同机器的其他账号不该能改这个文件（它的内容会进别人面板的页面）。
    """
    snapshot = build_snapshot(layout, config, peers, include_peers=False)
    snapshot["written_at"] = now().isoformat()
    path = atomic_write(
        layout.root,
        layout.root,
        STATUS_FILE,
        json.dumps(snapshot, ensure_ascii=False).encode(),
    )
    path.chmod(STATUS_MODE)


def read_status(layout: NodeLayout, config: Config, peers: PeerRegistry) -> dict[str, Any]:
    """给 `/node/summary` 用。文件太旧就现算一份 —— 别把陈旧状态当成真相。"""
    cached = _cached_status(layout.root / STATUS_FILE)
    if cached is not None:
        return cached
    fresh = build_snapshot(layout, config, peers, include_peers=False)
    fresh["written_at"] = now().isoformat()
    return fresh


def _cached_status(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    age = _age(str(data.get("written_at", "")))
    return dict(data) if age is not None and age < REFRESH_INTERVAL else None


def _age(written_at: str) -> float | None:
    """快照写下来多久了（秒）。取绝对值 —— 时钟往前跳过的文件同样不可信。"""
    try:
        moment = datetime.fromisoformat(written_at)
    except ValueError:
        return None
    try:
        return abs((now() - moment).total_seconds())
    except TypeError:  # 没带时区的时间戳，减不了
        return None


class ClusterCache:
    """远端快照的短期缓存，附带「有旧数据就先给旧的、后台去刷」的策略。

    第一次没得选，只能等；此后**任何一次请求都不会被某台机器拖住** ——
    这正是「一台连不上不会把整页卡住」这句承诺的实现方式。
    """

    def __init__(self, *, ttl: float = REFRESH_INTERVAL) -> None:
        self._ttl = ttl
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def node(
        self, peer: PeerRecord, config: Config, peers: PeerRegistry, log: EventLog
    ) -> dict[str, Any]:
        cached = self._entries.get(peer.node)
        if cached is not None and self._clock() - cached[0] < self._ttl:
            return cached[1]
        if cached is not None:
            self._spawn(peer, config, peers, log)
            return cached[1]
        return await self._refresh(peer, config, peers, log)

    async def aclose(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _spawn(self, peer: PeerRecord, config: Config, peers: PeerRegistry, log: EventLog) -> None:
        if any(task.get_name() == peer.node and not task.done() for task in self._tasks):
            return  # 这台机器已经在刷了，别叠加
        task = asyncio.create_task(self._refresh_quietly(peer, config, peers, log), name=peer.node)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _refresh_quietly(
        self, peer: PeerRecord, config: Config, peers: PeerRegistry, log: EventLog
    ) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await self._refresh(peer, config, peers, log)

    async def _refresh(
        self, peer: PeerRecord, config: Config, peers: PeerRegistry, log: EventLog
    ) -> dict[str, Any]:
        lock = self._locks.setdefault(peer.node, asyncio.Lock())
        async with lock:
            fresh = self._entries.get(peer.node)
            if fresh is not None and self._clock() - fresh[0] < self._ttl:
                return fresh[1]  # 排队等锁的时候别人已经刷过了
            node = await _fetch(peer, config, peers, log)
            self._entries[peer.node] = (self._clock(), node)
            return node

    @staticmethod
    def _clock() -> float:
        return asyncio.get_running_loop().time()  # 单调钟：不受系统改时间影响


async def build_cluster(
    layout: NodeLayout,
    config: Config,
    peers: PeerRegistry,
    log: EventLog,
    cache: ClusterCache | None = None,
) -> dict[str, Any]:
    """本机 + 所有**已信任**的对端。未信任的只出现在拓扑里，不去拉它的状态。"""
    local = build_snapshot(layout, config, peers)
    local["written_at"] = now().isoformat()

    store = cache or ClusterCache()
    targets = [p for p in peers.all() if p.trusted]
    fetched = await asyncio.gather(
        *(store.node(peer, config, peers, log) for peer in targets),
        return_exceptions=True,
    )
    return {
        "node": config.node.name,
        "nodes": [
            _ok(config.node.name, local, local=True),
            *(
                _down(peer, f"{type(node).__name__}: {node}")
                if isinstance(node, BaseException)
                else node
                for peer, node in zip(targets, fetched, strict=True)
            ),
        ],
    }


async def _fetch(
    peer: PeerRecord, config: Config, peers: PeerRegistry, log: EventLog
) -> dict[str, Any]:
    """取一个对端的快照。**绝不抛异常** —— 任何失败都变成「这台标成不可用」。"""
    timeout = SSH_TIMEOUT if _ssh_peer(config, peer.node) is not None else HTTP_TIMEOUT
    try:
        raw = await asyncio.wait_for(_pull(peer, config, peers), timeout=timeout)
    except Exception as exc:  # 一个节点出问题不该让整张面板打不开
        log.warn("cluster.unreachable", node=peer.node, error=f"{type(exc).__name__}: {exc}")
        return _down(peer, f"{type(exc).__name__}: {exc}")

    try:
        status = NodeStatus.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        # 校验不过就整份丢掉：宁可显示成「不可用」，也不把没校验的东西塞进页面
        log.warn("cluster.invalid_status", node=peer.node, error=str(exc)[:200])
        return _down(peer, "状态文件不合法，已丢弃（对端版本不一致，或内容被改过）")

    stale = _grade(status.written_at)
    if stale is not None:
        return _down(peer, stale)
    return _ok(peer.node, status.as_dict())


def _grade(written_at: str) -> str | None:
    """快照太旧就不能当「活着」。

    SSH 那条路是直接读对方磁盘上的文件 —— 那台机器上的 serve 被 kill 掉之后，
    文件还在，读得到。不看时间的话，一台已经死了一周的机器会一直显示成绿灯。
    """
    age = _age(written_at)
    if age is None:
        return "状态文件没有可用的时间戳"
    if age > FRESH_WINDOW:
        return f"状态已停更 {int(age)} 秒 —— 那台机器上的 anthill serve 可能已经停了"
    return None


async def _pull(peer: PeerRecord, config: Config, peers: PeerRegistry) -> bytes:
    """按对端的接法取那一个文件。SSH 优先 —— 它不要求对方开端口。"""
    ssh_peer = _ssh_peer(config, peer.node)
    if ssh_peer is not None:
        from anthill.transport.ssh import SshTransport

        transport = SshTransport(
            node_name=config.node.name,
            log=EventLog(None, agent=config.node.name, echo=False),
        )
        try:
            return await transport.read_bytes(
                peer.node, ssh_peer, REMOTE_STATUS, max_bytes=MAX_STATUS_BYTES
            )
        finally:
            await transport.close()

    if not peer.endpoint:
        raise ValueError(f"{peer.node} 既不是 ssh peer，也没有 HTTP 地址")
    return await _pull_http(peer, config, peers)


async def _pull_http(peer: PeerRecord, config: Config, peers: PeerRegistry) -> bytes:
    import httpx

    _, key = peers.require_trusted(peer.node)
    stamp = now().isoformat()
    headers = {
        "X-AntHill-Node": config.node.name,
        "X-AntHill-Ts": stamp,
        "X-AntHill-Sig": sign_request(key, node=config.node.name, path=SUMMARY_PATH, ts=stamp),
    }
    url = peer.endpoint.rstrip("/") + SUMMARY_PATH
    async with (
        httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client,
        client.stream("GET", url, headers=headers) as response,
    ):
        if response.status_code != 200:
            await response.aread()
            raise ValueError(f"HTTP {response.status_code}：{response.text[:200]}")
        # 边收边数：等 content 全进内存再判大小就已经晚了
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_STATUS_BYTES:
                raise ValueError(f"{peer.node} 的状态超过 {MAX_STATUS_BYTES} 字节上限")
            chunks.append(chunk)
    return b"".join(chunks)


def _ssh_peer(config: Config, node: str) -> PeerSection | None:
    peer = config.peers.get(node)
    return peer if peer is not None and peer.transport is TransportKind.SSH else None


def _ok(node: str, snapshot: dict[str, Any], *, local: bool = False) -> dict[str, Any]:
    # 默认值写在后面：远端传来的 payload 不能把 local/reachable 覆盖掉
    return {
        **snapshot,
        "node": node,
        "local": local,
        "reachable": True,
    }


def _down(peer: PeerRecord, reason: str) -> dict[str, Any]:
    return {
        "node": peer.node,
        "local": False,
        "reachable": False,
        "reason": reason,
        "endpoint": peer.endpoint,
        "agents": [],
        "runs": [],
        "events": [],
    }
