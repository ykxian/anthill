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
from anthill.web.endpoints import CONFIG_PATH

TIMEOUT = 10.0
MAX_CONFIG_BYTES = 512 * 1024


def _headers(config: Config, peers: PeerRegistry, node: str) -> tuple[str, dict[str, str]]:
    try:
        peer, key = peers.require_trusted(node)
    except PeerError as exc:
        raise AntHillError(str(exc)) from exc
    if not peer.endpoint:
        raise AntHillError(
            f"{node} 没有 HTTP 地址 —— SSH peer 的配置请直接在那台机器上改，"
            "面板不代管（那侧的约定是不开任何新端口）"
        )
    stamp = now().isoformat()
    return peer.endpoint.rstrip("/") + CONFIG_PATH, {
        "X-AntHill-Node": config.node.name,
        "X-AntHill-Ts": stamp,
        "X-AntHill-Sig": sign_request(key, node=config.node.name, path=CONFIG_PATH, ts=stamp),
    }


async def read_config(config: Config, peers: PeerRegistry, node: str) -> dict[str, Any]:
    url, headers = _headers(config, peers, node)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AntHillError(f"连不上 {node}：{exc}") from exc
    _check(node, response)
    body: dict[str, Any] = response.json()
    return {"node": node, "text": str(body.get("text", ""))[:MAX_CONFIG_BYTES]}


async def write_config(config: Config, peers: PeerRegistry, node: str, text: str) -> dict[str, Any]:
    url, headers = _headers(config, peers, node)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.put(url, headers=headers, json={"text": text})
        except httpx.HTTPError as exc:
            raise AntHillError(f"连不上 {node}：{exc}") from exc
    _check(node, response)
    return {"ok": True, "node": node, **response.json()}


def _check(node: str, response: httpx.Response) -> None:
    """把对端的拒绝翻译成人能看懂的话 —— 404 在这里有特定含义。"""
    if response.status_code == 404:
        raise AntHillError(
            f"{node} 没有开放远端管理。让那台机器在 node.toml 里写\n"
            "  [security]\n  remote_admin = true\n"
            "或者用 anthill serve --remote-admin 启动。\n"
            "想清楚再开：能改 node.toml ≈ 能在那台机器上执行命令。"
        )
    if response.status_code == 403:
        raise AntHillError(f"{node} 不信任本节点 —— 先配对（anthill peers pair）")
    if response.status_code != 200:
        raise AntHillError(
            f"{node} 拒绝了这次操作（HTTP {response.status_code}）：{response.text[:300]}"
        )
