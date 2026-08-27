"""传输层抽象（01-architecture §2 的那条公理）。

    Agent 之间的所有通信，最终都表现为「一个信封文件出现在目标 Agent 的 inbox/new 里」。

于是传输只有一个职责：**把信封送到那个目录**，且必须遵守 tmp→rename 原子写协议。
Local / LAN / SSH 三种实现对上层完全等价，新增传输不需要改 Agent 一行代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from anthill.core.config import PeerSection
from anthill.core.envelope import Envelope, TransportKind


@dataclass(frozen=True, slots=True)
class Destination:
    """一次投递的目标：哪个节点的哪个 Agent，以及（跨节点时）怎么连过去。"""

    node: str
    agent: str
    peer: PeerSection | None = None
    local_workspace: Path | None = None
    """跨工作区但仍在本机时，目标节点所在的工作区。"""

    @classmethod
    def from_envelope(cls, env: Envelope, peer: PeerSection | None = None) -> Destination:
        return cls(node=env.to.node, agent=env.to.agent, peer=peer)

    def __str__(self) -> str:
        return f"{self.node}:{self.agent}"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ok: bool
    transport: TransportKind
    destination: str
    path: str | None = None
    detail: str | None = None

    @classmethod
    def success(cls, kind: TransportKind, dest: Destination, path: str) -> DeliveryResult:
        return cls(ok=True, transport=kind, destination=str(dest), path=path)

    @classmethod
    def failure(cls, kind: TransportKind, dest: Destination, detail: str) -> DeliveryResult:
        return cls(ok=False, transport=kind, destination=str(dest), detail=detail)


class Transport(ABC):
    kind: TransportKind

    @abstractmethod
    async def deliver(self, env: Envelope, dest: Destination) -> DeliveryResult:
        """把信封送达目标邮箱。失败请返回 failure 或抛 DeliveryError，禁止静默。"""

    async def close(self) -> None:
        """释放连接等资源。默认无操作。"""
        return None
