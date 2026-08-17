"""配对的客户端一侧 —— CLI 和面板共用这一份。

抽出来是因为 CLI 那边习惯 `fail()` 直接退进程，而面板需要把失败翻译成 HTTP。
协议只有一套，就不该有两份实现。
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from typing import Any

import httpx

from anthill.core.errors import PeerError
from anthill.discovery.registry import PeerRecord, PeerRegistry
from anthill.security.keys import PairingToken
from anthill.security.pairing import (
    CONFIRM_HOST,
    CONFIRM_JOINER,
    confirm_tag,
    derive,
    exchange,
    tags_match,
)
from anthill.transport.http import peer_client
from anthill.web.endpoints import PAIR_CONFIRM_PATH, PAIR_PATH

TIMEOUT = 10.0


def resolve(peers: PeerRegistry, target: str) -> str:
    """`--to` 可以是地址，也可以是 discovery 见过的节点名。"""
    if target.startswith(("http://", "https://")):
        return target.rstrip("/")
    for peer in peers.all():
        if peer.node == target and peer.endpoint:
            return peer.endpoint.rstrip("/")
    raise PeerError(
        f"不认识节点 {target!r}，也没见过它的地址。"
        "等 discovery 发现它（anthill peers list 看看），或者直接写地址。"
    )


async def join(
    *,
    base: str,
    my_node: str,
    my_endpoint: str,
    pin: str,
    peers: PeerRegistry,
    for_node: str = "",
) -> PeerRecord:
    """去连对方：一次往返把密钥换完，成功后写进本机 peers 列表。

    **先验对方，再落自己的库** —— PIN 打错时 SPAKE2 不报错，
    只是两边各得一把不同的钥匙，不验就会配成「之后每条消息都验签失败」。

    `for_node`：对方一台机器照看好几个节点时，窗口开在哪个节点上就写谁 ——
    接收端一直按它路由，但这边以前**从来没发过**这个字段，空值落到主节点，
    「窗口开在第二个工作区」的配对永远踩空。
    """
    state, outbound = exchange(pin)
    async with peer_client(TIMEOUT) as client:
        body = await _offer(client, base, my_node, my_endpoint, outbound, for_node)
        try:
            key = derive(state, b64decode(str(body["msg"]), validate=True))
        except (KeyError, ValueError) as exc:
            raise PeerError(f"对方的配对消息不合法：{exc}") from exc

        if not tags_match(str(body.get("confirm", "")), confirm_tag(key, CONFIRM_HOST)):
            raise PeerError("PIN 不对：两边推导出的密钥不一致，本机什么都没写入。")

        await _confirm(client, base, my_node, key, for_node)

    return peers.trust(
        PairingToken(
            node=str(body.get("node") or base),
            endpoint=str(body.get("endpoint", "")),
            key=key,
        ),
        replace=True,
    )


async def _offer(
    client: httpx.AsyncClient, base: str, node: str, endpoint: str, outbound: bytes, for_node: str
) -> dict[str, Any]:
    payload = {"node": node, "endpoint": endpoint, "msg": b64encode(outbound).decode()}
    if for_node:
        payload["for_node"] = for_node
    try:
        response = await client.post(base + PAIR_PATH, json=payload)
    except httpx.HTTPError as exc:
        raise PeerError(f"连不上 {base}：{exc}") from exc
    if response.status_code == 409:
        raise PeerError(
            f"{base} 那边没有开着的配对窗口 —— 让对方先点「配对」或跑 anthill peers pair"
        )
    if response.status_code != 200:
        raise PeerError(f"配对被拒（HTTP {response.status_code}）：{response.text[:200]}")
    result: dict[str, Any] = response.json()
    return result


async def _confirm(
    client: httpx.AsyncClient, base: str, node: str, key: bytes, for_node: str
) -> None:
    payload = {"node": node, "confirm": confirm_tag(key, CONFIRM_JOINER)}
    if for_node:
        payload["for_node"] = for_node
    try:
        response = await client.post(base + PAIR_CONFIRM_PATH, json=payload)
    except httpx.HTTPError as exc:
        raise PeerError(f"确认这一步连不上 {base}：{exc}") from exc
    if response.status_code != 200:
        raise PeerError(f"对方没能确认（HTTP {response.status_code}）：{response.text[:200]}")
