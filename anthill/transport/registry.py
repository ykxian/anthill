"""按目标节点挑传输实现。

M1 只有 local；M4/M5 把 lan/ssh 注册进来即可，上层调用点一行都不用改。
"""

from __future__ import annotations

from anthill.core.config import Config, PeerSection
from anthill.core.envelope import Envelope, TransportKind
from anthill.core.errors import DeliveryError, UnroutableNode
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRegistry
from anthill.transport.base import DeliveryResult, Destination, Transport
from anthill.transport.lan import LanTransport
from anthill.transport.local import LocalTransport
from anthill.transport.ssh import SshTransport


class TransportRegistry:
    def __init__(
        self,
        config: Config,
        layout: NodeLayout,
        *,
        peers: PeerRegistry | None = None,
        log: EventLog | None = None,
    ) -> None:
        self._config = config
        self._layout = layout
        self._peers = peers if peers is not None else PeerRegistry(layout.root)
        self._transports: dict[TransportKind, Transport] = {
            TransportKind.LOCAL: LocalTransport(layout),
            TransportKind.LAN: LanTransport(
                node_name=config.node.name,
                peers=self._peers,
                log=log or EventLog(None, agent=config.node.name, echo=False),
                advertise=config.node.endpoint,
            ),
            TransportKind.SSH: SshTransport(
                node_name=config.node.name,
                peers=self._peers,
                log=log or EventLog(None, agent=config.node.name, echo=False),
            ),
        }

    def register(self, transport: Transport) -> None:
        self._transports[transport.kind] = transport

    def destination_for(self, env: Envelope) -> Destination:
        """先看 node.toml 里显式配的 peer，再看 peers 列表里信任过的（LAN 发现来的）。"""
        node = env.to.node
        if node == self._config.node.name:
            return Destination(node=node, agent=env.to.agent)
        peer = self._config.peers.get(node) or self._lan_peer(node)
        if peer is None:
            raise UnroutableNode(
                f"节点 {node!r} 不在 peers 列表里 —— 发现 ≠ 可通信，"
                f"需要先在 node.toml 配置或 `anthill peers trust --token <令牌>`"
            )
        return Destination(node=node, agent=env.to.agent, peer=peer)

    def _lan_peer(self, node: str) -> PeerSection | None:
        record = self._peers.get(node)
        if record is None or not record.trusted or not record.endpoint:
            return None
        return PeerSection(transport=TransportKind.LAN, endpoint=record.endpoint)

    def transport_for(self, dest: Destination) -> Transport:
        kind = dest.peer.transport if dest.peer else TransportKind.LOCAL
        transport = self._transports.get(kind)
        if transport is None:
            raise DeliveryError(f"传输方式 {kind} 尚未实现/未启用", retryable=False)
        return transport

    async def deliver(self, env: Envelope) -> DeliveryResult:
        dest = self.destination_for(env)
        return await self.transport_for(dest).deliver(env, dest)

    async def close(self) -> None:
        for transport in self._transports.values():
            await transport.close()
