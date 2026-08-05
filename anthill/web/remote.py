"""总控面板去读写**别台机器**的配置。

只有一条路：HTTP + 那把共享密钥的请求签名（和取状态同一套）。
SSH peer 不走这里 —— 那侧的约定是"不开任何新端口"，
要在服务器上改配置，你本来就有 SSH，直接改比绕一圈更直接也更明白。
"""

from __future__ import annotations

from typing import Any

import httpx

from anthill.core.config import Config
from anthill.core.errors import AntHillError, PeerError
from anthill.core.ids import now
from anthill.discovery.registry import PeerRegistry
from anthill.security.signing import sign_request
from anthill.transport.http import peer_client

TIMEOUT = 10.0
MAX_CONFIG_BYTES = 512 * 1024


def _signed_for(
    config: Config, peers: PeerRegistry, node: str, path: str
) -> tuple[str, dict[str, str]]:
    """算出 `(完整 URL, 签名头)`。签名签的是**请求路径本身**，
    所以一个「启动 coder」的签名换不来「启动别人」或者别的动作。"""
    try:
        peer, key = peers.require_trusted(node)
    except PeerError as exc:
        raise AntHillError(str(exc)) from exc
    if not peer.endpoint:
        raise AntHillError(
            f"{node} 没有 HTTP 地址 —— SSH peer 那侧的约定是不开任何新端口，"
            "在那台机器上直接操作，面板不代管"
        )
    stamp = now().isoformat()
    return peer.endpoint.rstrip("/") + path, {
        "X-AntHill-Node": config.node.name,
        "X-AntHill-Ts": stamp,
        "X-AntHill-Sig": sign_request(key, node=config.node.name, path=path, ts=stamp),
    }


async def control_agent(
    config: Config, peers: PeerRegistry, node: str, agent: str, action: str
) -> dict[str, Any]:
    """启停别台机器上的 agentd。和改它的配置同一道闸（remote_admin）。"""
    if action not in ("start", "stop"):
        raise AntHillError(f"不认识的动作 {action!r}")
    url, headers = _signed_for(config, peers, node, f"/node/{node}/agents/{agent}/{action}")
    async with peer_client(TIMEOUT) as client:
        try:
            response = await client.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AntHillError(f"连不上 {node}：{exc}") from exc
    _check(node, response, ok=(200, 202))
    return {"node": node, **response.json()}


async def read_config(config: Config, peers: PeerRegistry, node: str) -> dict[str, Any]:
    url, headers = _signed_for(config, peers, node, f"/node/{node}/config")
    async with peer_client(TIMEOUT) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AntHillError(f"连不上 {node}：{exc}") from exc
    _check(node, response)
    body: dict[str, Any] = response.json()
    return {"node": node, "text": str(body.get("text", ""))[:MAX_CONFIG_BYTES]}


async def write_config(config: Config, peers: PeerRegistry, node: str, text: str) -> dict[str, Any]:
    url, headers = _signed_for(config, peers, node, f"/node/{node}/config")
    async with peer_client(TIMEOUT) as client:
        try:
            response = await client.put(url, headers=headers, json={"text": text})
        except httpx.HTTPError as exc:
            raise AntHillError(f"连不上 {node}：{exc}") from exc
    _check(node, response)
    return {"ok": True, "node": node, **response.json()}


def _check(node: str, response: httpx.Response, ok: tuple[int, ...] = (200,)) -> None:
    """把对端的拒绝翻译成人能看懂的话 —— 404 在这里有特定含义。"""
    if response.status_code in ok:
        return
    if response.status_code == 404:
        raise AntHillError(
            f"{node} 上没有这条路径。**别急着去开远端管理** —— 404 有好几种可能：\n"
            f"  · 那台机器上没有名为 {node} 的节点（名字打错了？）\n"
            "  · 两边版本不一致，那个端点还不存在\n"
            "  · 它确实没开远端管理（关着时端点就是不存在，不回 403 —— "
            "刻意不给人留一个可以试探的门把手）\n"
            "先确认前两条。真要开第三条，那台机器上写 [security] remote_admin = true，"
            "并且想清楚：能改 node.toml ≈ 能在那台机器上执行命令。"
        )
    if response.status_code == 403:
        raise AntHillError(f"{node} 不信任本节点 —— 先配对（anthill peers pair）")
    raise AntHillError(
        f"{node} 拒绝了这次操作（HTTP {response.status_code}）：{response.text[:300]}"
    )
