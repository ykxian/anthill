"""组播信标：默认静默、解析健壮、发现 ≠ 信任。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anthill.core.config import DiscoverySection
from anthill.core.logging import EventLog
from anthill.discovery.beacon import Announcement, Beacon
from anthill.discovery.registry import PeerRegistry

ME = Announcement(node="laptop", endpoint="http://10.0.8.9:45778", agents=("cli", "coder"))
PEER = Announcement(node="lab", endpoint="http://10.0.8.21:45778", agents=("runner",))


def make_beacon(
    tmp_path: Path, *, enabled: bool = True, siblings: frozenset[str] = frozenset()
) -> tuple[Beacon, PeerRegistry]:
    peers = PeerRegistry(tmp_path)
    beacon = Beacon(
        settings=DiscoverySection(enabled=enabled, port=45999),
        announcement=ME,
        peers=peers,
        log=EventLog(None, agent="laptop", echo=False),
        interval=0.05,
        siblings=siblings,
    )
    return beacon, peers


# ---------- 广播包 ----------


def test_announcement_roundtrips() -> None:
    decoded = Announcement.from_bytes(PEER.to_bytes())

    assert decoded == PEER


def test_announcement_carries_no_secrets() -> None:
    """广播是明文且谁都能收，所以只能放公开信息。"""
    body = PEER.to_bytes().decode()

    for forbidden in ("key", "token", "secret", "/home/"):
        assert forbidden not in body.lower()


@pytest.mark.parametrize(
    "junk",
    [b"", b"not json", b"[]", b'{"kind":"something-else"}', b'{"kind":"anthill.announce"}'],
)
def test_garbage_datagrams_are_ignored(junk: bytes) -> None:
    """网络上什么包都可能飞过来，解析失败必须返回 None 而不是抛异常。"""
    assert Announcement.from_bytes(junk) is None


def test_oversized_datagram_is_ignored() -> None:
    assert Announcement.from_bytes(b"x" * 100_000) is None


def test_announcement_with_an_illegal_node_name_is_ignored() -> None:
    forged = b'{"kind":"anthill.announce","node":"../../etc","endpoint":"x"}'

    assert Announcement.from_bytes(forged) is None


# ---------- 收包行为 ----------


def test_receiving_an_announcement_records_a_discovered_peer(tmp_path: Path) -> None:
    # Arrange
    beacon, peers = make_beacon(tmp_path)

    # Act
    beacon.on_datagram(PEER.to_bytes(), ("10.0.8.21", 45999))

    # Assert：只是「见过」，绝不是「可通信」
    peer = peers.get("lab")
    assert peer is not None
    assert peer.endpoint == PEER.endpoint
    assert peer.agents == ("runner",)
    assert not peer.trusted


def test_own_announcement_is_ignored(tmp_path: Path) -> None:
    beacon, peers = make_beacon(tmp_path)

    beacon.on_datagram(ME.to_bytes(), ("10.0.8.9", 45999))

    assert peers.get("laptop") is None


def test_incompatible_protocol_version_is_ignored(tmp_path: Path) -> None:
    beacon, peers = make_beacon(tmp_path)
    future = Announcement(node="lab", endpoint="x", proto="9.0")

    beacon.on_datagram(future.to_bytes(), ("10.0.8.21", 45999))

    assert peers.get("lab") is None


def test_a_trusted_peer_is_not_downgraded_or_moved_by_an_announcement(tmp_path: Path) -> None:
    """收包这条路**没有任何认证** —— 谁都能往组播地址上丢一个包。

    所以它既不能改信任状态，也不能改投递地址。以前只挡住了前者：
    `trusted` 字段确实保住了，可 endpoint 被无条件覆盖，而那正是投递用的 URL ——
    伪造一个包就能把一个已信任节点的全部出站消息引到自己这儿。
    局域网是明文 HTTP，等于任务内容直接泄露 + 静默 DoS。
    """
    # Arrange
    from anthill.security.keys import PairingToken, new_key

    beacon, peers = make_beacon(tmp_path)
    peers.trust(PairingToken(node="lab", endpoint="http://old", key=new_key()))

    # Act：伪造一条 announce，自称 lab 在别的地址上
    beacon.on_datagram(PEER.to_bytes(), ("10.0.8.21", 45999))

    # Assert
    peer = peers.get("lab")
    assert peer is not None
    assert peer.trusted
    assert peer.endpoint == "http://old", "一个没认证的广播包改掉了投递地址"
    assert peer.seen_endpoint == PEER.endpoint  # 但要记下来，摆给人看
    assert peer.endpoint_conflict is True


# ---------- 默认静默（核心需求）----------


async def test_disabled_beacon_creates_no_socket_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「不开启时节点完全静默」：连 socket 都不该创建。"""
    # Arrange
    import anthill.discovery.beacon as beacon_module

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("discovery 关闭时不该创建任何 socket")

    monkeypatch.setattr(beacon_module, "make_socket", explode)
    beacon, _ = make_beacon(tmp_path, enabled=False)
    stop = asyncio.Event()

    # Act
    task = asyncio.create_task(beacon.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    # Assert
    assert not beacon.enabled


async def test_disabled_beacon_stops_promptly(tmp_path: Path) -> None:
    beacon, _ = make_beacon(tmp_path, enabled=False)
    stop = asyncio.Event()
    task = asyncio.create_task(beacon.run(stop))

    stop.set()

    await asyncio.wait_for(task, timeout=2)  # 不会卡住退出流程


async def test_announce_is_a_noop_before_the_socket_exists(tmp_path: Path) -> None:
    beacon, _ = make_beacon(tmp_path)

    beacon.announce_once()  # 不抛异常即可


def test_a_sibling_node_on_the_same_serve_is_not_a_peer(tmp_path: Path) -> None:
    """一个 serve 照看两个工作区时，它给两个节点各发一份 announce、也各收一份 ——
    于是节点 A 会把同机器的节点 B「发现」成外部对端。

    那既没意义（它们本来就在一个进程里，投递走本地文件），又实实在在坏事：
    总控视图合并时先到先得，B 会被显示成「连不上的对端」，
    而不是本机的第二个工作区 —— 侧栏里点都点不了。
    """
    beacon, peers = make_beacon(tmp_path, siblings=frozenset({PEER.node}))

    beacon.on_datagram(PEER.to_bytes(), ("10.0.8.21", 45999))

    assert peers.get(PEER.node) is None, "同一个 serve 的另一个节点不该进 peers"


def test_a_real_stranger_is_still_discovered(tmp_path: Path) -> None:
    """别把闸修得太宽 —— 真正的外部节点照常发现。"""
    beacon, peers = make_beacon(tmp_path, siblings=frozenset({"某个本机节点"}))

    beacon.on_datagram(PEER.to_bytes(), ("10.0.8.21", 45999))

    assert peers.get(PEER.node) is not None
