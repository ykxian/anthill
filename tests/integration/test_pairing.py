"""六位 PIN 码配对。

这套东西的全部价值在于：**短口令也能安全换密钥**。所以测试盯住的是
「短口令为什么没被暴破」——密钥不上线、一个窗口只有一次机会、
PIN 不对时两边都不落库。
"""

from __future__ import annotations

import json
from base64 import b64decode, b64encode
from pathlib import Path

import httpx
import pytest

from anthill.core.config import Config
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRegistry
from anthill.security.pairing import (
    CONFIRM_HOST,
    CONFIRM_JOINER,
    PairingStore,
    confirm_tag,
    derive,
    exchange,
    new_pin,
)
from anthill.web.app import create_app
from anthill.web.endpoints import PAIR_CONFIRM_PATH, PAIR_PATH

NODE_TOML = """
[node]
name = "{name}"
workspace = "."
endpoint = "http://{name}.test:45778"

[agents.cli]
role = "user"
"""

Bundle = tuple[NodeLayout, Config, PeerRegistry]


def make_node(root: Path, name: str) -> Bundle:
    layout = NodeLayout(root).ensure_base()
    layout.node_toml.write_text(NODE_TOML.format(name=name), encoding="utf-8")
    return layout, Config.load_from(layout), PeerRegistry(layout.root)


@pytest.fixture
def host(tmp_path: Path) -> Bundle:
    return make_node(tmp_path / "box61", "box61")


def client_for(bundle: Bundle) -> httpx.AsyncClient:
    layout, config, peers = bundle
    app = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=EventLog(None, agent="test", echo=False),
        advertise="http://box61.test:45778",
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.0.0.9", 1)),  # type: ignore[arg-type]
        base_url="http://box61.test",
    )


async def join(bundle: Bundle, pin: str, *, node: str = "box59") -> tuple[httpx.Response, bytes]:
    """扮演「去连对方的一方」：发一条 SPAKE2 消息，推导密钥。"""
    state, outbound = exchange(pin)
    async with client_for(bundle) as client:
        response = await client.post(
            PAIR_PATH,
            json={
                "node": node,
                "endpoint": f"http://{node}.test:45778",
                "msg": b64encode(outbound).decode(),
            },
        )
    if response.status_code != 200:
        return response, b""
    return response, derive(state, b64decode(str(response.json()["msg"])))


# ---------- 密钥不上线 ----------


async def test_the_key_never_travels_over_the_wire(host: Bundle) -> None:
    """这是整个方案成立的前提。

    如果密钥（或用 PIN 加密的密钥）过了网线，抓包的人离线暴破六位数字是秒级的事。
    PAKE 的意义就是让线路上**没有任何可暴破的东西**。
    """
    # Arrange
    layout, _, _ = host
    pin = new_pin()
    PairingStore(layout.root).open(pin)

    # Act
    response, key = await join(host, pin)

    # Assert
    assert response.status_code == 200
    wire = json.dumps(response.json())
    assert key.hex() not in wire
    assert b64encode(key).decode() not in wire
    assert pin not in wire


async def test_both_sides_derive_the_same_key(host: Bundle) -> None:
    layout, _, _ = host
    pin = new_pin()
    PairingStore(layout.root).open(pin)

    response, key = await join(host, pin)

    # 主机侧把推导结果暂存起来等确认 —— 这时两边应该已经是同一把钥匙
    held = PairingStore(layout.root).current()
    assert held is not None
    assert held.peer_key == key.hex()
    assert response.json()["confirm"] == confirm_tag(key, CONFIRM_HOST)


# ---------- 一个窗口只有一次机会 ----------


async def test_a_wrong_pin_is_detected_and_nothing_is_written(host: Bundle) -> None:
    """SPAKE2 在口令不符时**不报错**，只是各得一把不同的钥匙。

    少了密钥确认这一步，PIN 打错会配成「看起来成功了、之后每条消息都验签失败」——
    最难查的那种状态。
    """
    layout, _, peers = host
    PairingStore(layout.root).open("111111")

    response, key = await join(host, "999999")

    assert response.status_code == 200  # 协议层面走通了
    assert response.json()["confirm"] != confirm_tag(key, CONFIRM_HOST)  # 但钥匙不是同一把
    assert peers.all() == []  # 而且什么都没落库


async def test_a_used_window_refuses_a_second_attempt(host: Bundle) -> None:
    """六位 PIN 只有一百万种。在线穷举必须被堵死 —— 一个窗口一次机会。"""
    layout, _, _ = host
    PairingStore(layout.root).open("111111")

    first, _ = await join(host, "999999")  # 猜错
    second, _ = await join(host, "111111")  # 就算下一次猜对了也没用

    assert first.status_code == 200
    assert second.status_code == 409


async def test_pairing_without_an_open_window_is_refused(host: Bundle) -> None:
    """没开窗口时 `/pair` 什么也不是 —— 平时它不构成攻击面。"""
    response, _ = await join(host, "123456")

    assert response.status_code == 409


async def test_an_expired_window_is_gone(host: Bundle) -> None:
    layout, _, _ = host
    store = PairingStore(layout.root)
    store.open("111111")
    stale = json.loads(store.path.read_text(encoding="utf-8"))
    stale["opened_at"] = "2020-01-01T00:00:00+00:00"
    store.path.write_text(json.dumps(stale), encoding="utf-8")

    response, _ = await join(host, "111111")

    assert response.status_code == 409


async def test_the_pairing_file_is_not_world_readable(host: Bundle) -> None:
    """窗口里存着 PIN —— 在那两分钟里它等价于密钥。"""
    layout, _, _ = host
    store = PairingStore(layout.root)
    store.open("111111")

    assert store.path.stat().st_mode & 0o077 == 0


# ---------- 确认这一步 ----------


async def test_a_confirmed_exchange_lands_in_the_peer_list(host: Bundle) -> None:
    # Arrange
    layout, _, peers = host
    pin = new_pin()
    PairingStore(layout.root).open(pin)
    _, key = await join(host, pin)

    # Act
    async with client_for(host) as client:
        response = await client.post(
            PAIR_CONFIRM_PATH,
            json={"node": "box59", "confirm": confirm_tag(key, CONFIRM_JOINER)},
        )

    # Assert
    assert response.status_code == 200
    peer = next(p for p in peers.all() if p.node == "box59")
    assert peer.trusted
    assert peer.endpoint == "http://box59.test:45778"
    assert PairingStore(layout.root).current() is None  # 窗口已经收掉


async def test_a_bad_confirmation_writes_nothing_and_burns_the_window(host: Bundle) -> None:
    layout, _, peers = host
    PairingStore(layout.root).open("111111")
    await join(host, "111111")

    async with client_for(host) as client:
        response = await client.post(PAIR_CONFIRM_PATH, json={"node": "box59", "confirm": "0" * 64})

    assert response.status_code == 401
    assert peers.all() == []
    assert PairingStore(layout.root).current() is None


async def test_confirmation_from_a_different_node_is_refused(host: Bundle) -> None:
    """别人不能替这次配对拍板。"""
    layout, _, peers = host
    PairingStore(layout.root).open("111111")
    _, key = await join(host, "111111")

    async with client_for(host) as client:
        response = await client.post(
            PAIR_CONFIRM_PATH,
            json={"node": "stranger", "confirm": confirm_tag(key, CONFIRM_JOINER)},
        )

    assert response.status_code == 409
    assert peers.all() == []


async def test_malformed_pairing_input_is_refused_not_crashed(host: Bundle) -> None:
    layout, _, _ = host
    PairingStore(layout.root).open("111111")

    async with client_for(host) as client:
        response = await client.post(
            PAIR_PATH, json={"node": "box59", "endpoint": "", "msg": "这不是 base64"}
        )

    assert response.status_code == 400


# ---------- 两边一起跑一遍真的协议 ----------


async def paired_through_the_real_client(host_bundle: Bundle, joiner: Bundle, pin: str) -> object:
    """让 `security/pair_client.py`（CLI 和面板共用的那份）打真的 HTTP 栈。"""
    import anthill.security.pair_client as client_mod

    app = create_app(
        layout=host_bundle[0],
        config=host_bundle[1],
        peers=host_bundle[2],
        log=EventLog(None, agent="test", echo=False),
        advertise="http://box61.test:45778",
    )
    original = httpx.AsyncClient

    def patched(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.ASGITransport(app=app)  # type: ignore[assignment]
        return original(*args, **kwargs)  # type: ignore[arg-type]

    client_mod.httpx.AsyncClient = patched  # type: ignore[assignment]
    try:
        return await client_mod.join(
            base="http://box61.test",
            my_node=joiner[1].node.name,
            my_endpoint=joiner[1].node.endpoint,
            pin=pin,
            peers=joiner[2],
        )
    finally:
        client_mod.httpx.AsyncClient = original  # type: ignore[assignment]


async def test_the_two_sides_end_up_with_the_same_key(tmp_path: Path) -> None:
    """端到端跑一遍：两边都落库，而且是同一把钥匙（指纹一致）。"""
    # Arrange
    host_bundle = make_node(tmp_path / "box61", "box61")
    joiner = make_node(tmp_path / "box59", "box59")
    pin = new_pin()
    PairingStore(host_bundle[0].root).open(pin)

    # Act
    record = await paired_through_the_real_client(host_bundle, joiner, pin)

    # Assert
    theirs = next(p for p in host_bundle[2].all() if p.node == "box59")
    assert theirs.trusted
    assert theirs.fingerprint == record.fingerprint  # type: ignore[attr-defined]
    assert theirs.endpoint == "http://box59.test:45778"


async def test_a_wrong_pin_leaves_both_sides_clean(tmp_path: Path) -> None:
    """PIN 打错时，两边**都**不能落库 —— 只要有一边落了就是「配上了但用不了」。"""
    from anthill.core.errors import PeerError

    host_bundle = make_node(tmp_path / "box61", "box61")
    joiner = make_node(tmp_path / "box59", "box59")
    PairingStore(host_bundle[0].root).open("111111")

    with pytest.raises(PeerError, match="PIN"):
        await paired_through_the_real_client(host_bundle, joiner, "999999")

    assert joiner[2].all() == []
    assert host_bundle[2].all() == []
