"""局域网传输：HTTP POST + HMAC 签名。

与 local 传输的唯一区别是「送进目录」这一步换成了「请对方把它送进目录」。
上层（Sender / Agent）完全无感。

失败分两类，直接决定重试行为：
- **不可重试**：未信任、签名被拒、收件人不存在 —— 重试一万次结果一样，立刻死信；
- **可重试**：连不上、超时、对端 5xx —— 交给 outbox 指数退避。
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

import httpx

from anthill.core.envelope import Envelope, TransportKind
from anthill.core.errors import DeliveryError, PeerError
from anthill.core.logging import EventLog
from anthill.discovery.registry import PeerRegistry
from anthill.security.signing import sign_envelope
from anthill.transport.base import DeliveryResult, Destination, Transport
from anthill.web.endpoints import DELIVER_PATH

DEFAULT_TIMEOUT = 15.0
FATAL_STATUS = frozenset({400, 401, 403, 404, 421})


class LanTransport(Transport):
    kind = TransportKind.LAN

    def __init__(
        self,
        *,
        node_name: str,
        peers: PeerRegistry,
        log: EventLog,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        advertise: str = "",
    ) -> None:
        self._node = node_name
        self._advertise = advertise
        self._peers = peers
        self._log = log
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def deliver(self, env: Envelope, dest: Destination) -> DeliveryResult:
        try:
            peer, key = self._peers.require_trusted(dest.node)
        except PeerError as exc:
            # 连请求都不发：免得把消息内容泄给一个还没被信任的节点
            raise DeliveryError(str(exc), retryable=False) from exc

        endpoint = dest.peer.endpoint if dest.peer and dest.peer.endpoint else peer.endpoint
        if not endpoint:
            raise DeliveryError(f"节点 {dest.node} 没有可用的 endpoint", retryable=False)

        signed = sign_envelope(env, key)
        url = endpoint.rstrip("/") + DELIVER_PATH
        try:
            response = await self._client.post(
                url,
                json=signed.model_dump(mode="json", by_alias=True),
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            return DeliveryResult.failure(self.kind, dest, f"{type(exc).__name__}: {exc}")

        if response.status_code in FATAL_STATUS:
            detail = _detail(response)
            self._log.error(
                "lan.refused", msg=env.id, to=str(dest), status=response.status_code, error=detail
            )
            raise DeliveryError(
                f"对端拒收（HTTP {response.status_code}）：{detail}", retryable=False
            )
        if response.status_code >= 400:
            return DeliveryResult.failure(
                self.kind, dest, f"HTTP {response.status_code}：{_detail(response)}"
            )
        return DeliveryResult.success(self.kind, dest, url)

    def _headers(self) -> dict[str, str]:
        """带上自己的回信地址：对方收到后记进 peers，回信才知道往哪发。

        没有它的话，`invite/trust` 配好的一对节点只能单向通信 ——
        被邀请方在邀请方的 peers 列表里是没有 endpoint 的。
        """
        headers = {"X-AntHill-Node": self._node}
        if self._advertise:
            headers["X-AntHill-Endpoint"] = self._advertise
        return headers

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("detail", body))[:500] if isinstance(body, dict) else str(body)[:200]
