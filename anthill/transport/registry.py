"""按目标节点挑传输实现。

M1 只有 local；M4/M5 把 lan/ssh 注册进来即可，上层调用点一行都不用改。
"""

from __future__ import annotations

from anthill.core.config import Config
from anthill.core.envelope import Envelope, TransportKind
from anthill.core.errors import DeliveryError
from anthill.core.paths import NodeLayout
from anthill.transport.base import DeliveryResult, Destination, Transport
from anthill.transport.local import LocalTransport


class TransportRegistry:
    def __init__(self, config: Config, layout: NodeLayout) -> None:
        self._config = config
        self._layout = layout
        self._transports: dict[TransportKind, Transport] = {
            TransportKind.LOCAL: LocalTransport(layout)
        }

    def register(self, transport: Transport) -> None:
        self._transports[transport.kind] = transport

    def destination_for(self, env: Envelope) -> Destination:
        node = env.to.node
        if node == self._config.node.name:
            return Destination(node=node, agent=env.to.agent)
        peer = self._config.peers.get(node)
        if peer is None:
            raise DeliveryError(
                f"节点 {node!r} 不在 peers 列表里 —— 发现 ≠ 可通信，"
                f"需要先在 node.toml 配置或 `anthill peers trust {node}`",
                retryable=False,
            )
        return Destination(node=node, agent=env.to.agent, peer=peer)

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
