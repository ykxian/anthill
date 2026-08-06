"""UDP 组播信标：周期性 announce 自己，同时收听别人（01-architecture §5.3）。

默认开着 —— 否则同网段的两台机器要先手动互相告知地址，太劝退。
广播包里只有公开信息：节点名、Agent 名单、地址。
**`enabled = false` 时不发包、不监听、连 socket 都不创建**，这条路一直留着。

用组播（239.x.x.x）而不是全网广播，且 TTL=1 不出本网段，是为了把打扰面压到最小。
收到 announce 只会把对方记进 peers 列表（`discovered`），**不会**建立任何信任 ——
真要互投消息，还得有人在两边各看一眼、核对指纹（`anthill peers pair`）。
这条线是这个功能的全部安全性所在，任何时候都不能因为"方便"而松掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import struct
from dataclasses import dataclass
from typing import Any

from anthill.core.config import DiscoverySection
from anthill.core.envelope import NODE_NAME_RE, PROTO_VERSION
from anthill.core.logging import EventLog
from anthill.discovery.registry import PeerRegistry

ANNOUNCE_INTERVAL = 30.0
MULTICAST_TTL = 1  # 只在本网段内可见，不跨路由器
MAX_DATAGRAM = 4096


@dataclass(frozen=True, slots=True)
class Announcement:
    """广播包的内容。**只放公开信息** —— 密钥、路径、任务内容一概不进。"""

    node: str
    endpoint: str
    agents: tuple[str, ...] = ()
    proto: str = PROTO_VERSION

    def to_bytes(self) -> bytes:
        payload = {
            "kind": "anthill.announce",
            "proto": self.proto,
            "node": self.node,
            "endpoint": self.endpoint,
            "agents": list(self.agents),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> Announcement | None:
        """解析失败一律返回 None：网络上什么垃圾包都可能飞过来，不能让它把进程搞崩。"""
        if len(data) > MAX_DATAGRAM:
            return None
        try:
            parsed: Any = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(parsed, dict) or parsed.get("kind") != "anthill.announce":
            return None
        node = str(parsed.get("node", ""))
        if not NODE_NAME_RE.match(node):
            return None
        agents = parsed.get("agents")
        return cls(
            node=node,
            endpoint=str(parsed.get("endpoint", ""))[:200],
            agents=tuple(str(a)[:64] for a in agents[:64]) if isinstance(agents, list) else (),
            proto=str(parsed.get("proto", PROTO_VERSION)),
        )


class Beacon:
    def __init__(
        self,
        *,
        settings: DiscoverySection,
        announcement: Announcement,
        peers: PeerRegistry,
        log: EventLog,
        interval: float = ANNOUNCE_INTERVAL,
        siblings: frozenset[str] = frozenset(),
    ) -> None:
        self._settings = settings
        self._announcement = announcement
        self._siblings = siblings
        """同一个 serve 照看的**别的本机节点**。

        一个进程照看两个工作区时，它给两个节点各发一份 announce，也各收一份 ——
        于是节点 A 会把同机器的节点 B「发现」成一个外部对端。那既没意义
        （它们本来就在一个进程里，投递走本地文件），又实实在在坏事：
        面板的总控视图会把 B 显示成「连不上的对端」，而不是本机的第二个工作区。
        """
        self._peers = peers
        self._log = log
        self._interval = interval
        self._transport: asyncio.DatagramTransport | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    def on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """收到一个包。只记「见过」，绝不建立信任。"""
        announcement = Announcement.from_bytes(data)
        if announcement is None:
            return
        if announcement.node == self._announcement.node:
            return  # 自己的包
        if announcement.node in self._siblings:
            return  # 同一个 serve 照看的另一个本机节点，不是「对端」
        if announcement.proto.split(".", 1)[0] != PROTO_VERSION.split(".", 1)[0]:
            self._log.warn(
                "discovery.proto_mismatch",
                node=announcement.node,
                proto=announcement.proto,
            )
            return
        known = self._peers.get(announcement.node)
        self._peers.observe(
            node=announcement.node,
            endpoint=announcement.endpoint,
            agents=announcement.agents,
        )
        if known is None:
            self._log.info(
                "discovery.found",
                node=announcement.node,
                endpoint=announcement.endpoint,
                agents=",".join(announcement.agents),
                hint="发现 ≠ 可通信；核对指纹后用 `anthill peers trust` 才能互投",
            )

    async def run(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            # 一行日志，让「为什么对面看不见我」这个问题一眼有答案
            self._log.info("discovery.disabled", hint="node.toml 里 [discovery] enabled = false")
            await stop.wait()
            return

        loop = asyncio.get_running_loop()
        try:
            sock = make_socket(self._settings.multicast_group, self._settings.port)
        except OSError as exc:
            self._log.error("discovery.socket_failed", error=str(exc))
            await stop.wait()
            return

        transport, _ = await loop.create_datagram_endpoint(lambda: _BeaconProtocol(self), sock=sock)
        self._transport = transport
        self._log.info(
            "discovery.started",
            group=self._settings.multicast_group,
            port=self._settings.port,
            interval=self._interval,
        )
        try:
            await self._announce_loop(stop)
        finally:
            transport.close()
            self._transport = None

    async def _announce_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.announce_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)

    def announce_once(self) -> None:
        if self._transport is None:
            return
        target = (self._settings.multicast_group, self._settings.port)
        try:
            self._transport.sendto(self._announcement.to_bytes(), target)
        except OSError as exc:
            self._log.warn("discovery.announce_failed", error=str(exc))


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, beacon: Beacon) -> None:
        self._beacon = beacon

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._beacon.on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        self._beacon._log.warn("discovery.socket_error", error=str(exc))


def make_socket(group: str, port: int) -> socket.socket:
    """一个既能收组播又能发组播的 socket。TTL=1 保证不出本网段。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with contextlib.suppress(AttributeError, OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    membership = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
    sock.setblocking(False)
    return sock
