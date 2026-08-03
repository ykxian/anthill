"""peers 注册表：发现 ≠ 信任，以及 TOFU 指纹校验。"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from anthill.core.errors import PeerError
from anthill.discovery.registry import PeerRecord, PeerRegistry
from anthill.security.keys import PairingToken, fingerprint, new_key

KEY = b"a" * 32
OTHER_KEY = b"b" * 32
ENDPOINT = "http://10.0.8.21:45778"


def token(node: str = "lab", key: bytes = KEY) -> PairingToken:
    return PairingToken(node=node, endpoint=ENDPOINT, key=key)


# ---------- 发现 ≠ 信任 ----------


def test_a_freshly_discovered_peer_is_not_trusted(tmp_path: Path) -> None:
    registry = PeerRegistry(tmp_path)

    registry.observe(node="lab", endpoint=ENDPOINT, agents=("runner",))

    peer = registry.get("lab")
    assert peer is not None
    assert not peer.trusted
    assert registry.key_for("lab") is None


def test_untrusted_peer_cannot_be_used_for_delivery(tmp_path: Path) -> None:
    registry = PeerRegistry(tmp_path)
    registry.observe(node="lab", endpoint=ENDPOINT, agents=())

    with pytest.raises(PeerError, match="未信任"):
        registry.require_trusted("lab")


def test_unknown_peer_says_how_to_fix_it(tmp_path: Path) -> None:
    with pytest.raises(PeerError, match="trust"):
        PeerRegistry(tmp_path).require_trusted("stranger")


def test_observing_again_refreshes_last_seen_without_losing_trust(tmp_path: Path) -> None:
    # Arrange
    registry = PeerRegistry(tmp_path)
    registry.trust(token())
    before = registry.get("lab")

    # Act
    registry.observe(node="lab", endpoint="http://10.0.8.21:9999", agents=("runner",))

    # Assert：广播能更新端点，但不能把一个已信任的对端悄悄降级
    peer = registry.get("lab")
    assert peer is not None and before is not None
    assert peer.trusted
    assert peer.endpoint == "http://10.0.8.21:9999"
    assert peer.last_seen >= before.last_seen


# ---------- TOFU ----------


def test_trusting_records_the_fingerprint(tmp_path: Path) -> None:
    registry = PeerRegistry(tmp_path)

    peer = registry.trust(token())

    assert peer.trusted
    assert peer.fingerprint == fingerprint(KEY)
    assert registry.key_for("lab") == KEY


def test_a_changed_fingerprint_is_refused_loudly(tmp_path: Path) -> None:
    # Arrange：已经信任过 lab
    registry = PeerRegistry(tmp_path)
    registry.trust(token())

    # Act / Assert：同名节点换了一把钥匙 —— 可能是重装，也可能是有人冒充
    with pytest.raises(PeerError, match="指纹"):
        registry.trust(token(key=OTHER_KEY))


def test_a_changed_fingerprint_can_be_accepted_explicitly(tmp_path: Path) -> None:
    registry = PeerRegistry(tmp_path)
    registry.trust(token())

    peer = registry.trust(token(key=OTHER_KEY), replace=True)

    assert peer.fingerprint == fingerprint(OTHER_KEY)
    assert registry.key_for("lab") == OTHER_KEY


def test_forget_removes_the_peer_and_its_key(tmp_path: Path) -> None:
    registry = PeerRegistry(tmp_path)
    registry.trust(token())

    assert registry.forget("lab")
    assert registry.get("lab") is None
    assert registry.key_for("lab") is None
    assert not registry.forget("lab")  # 再删一次是 no-op，不报错


# ---------- 落盘 ----------


def test_registry_survives_a_reload(tmp_path: Path) -> None:
    PeerRegistry(tmp_path).trust(token())

    reloaded = PeerRegistry(tmp_path)

    peer = reloaded.get("lab")
    assert peer is not None and peer.trusted
    assert reloaded.key_for("lab") == KEY


def test_peer_file_is_not_world_readable(tmp_path: Path) -> None:
    """文件里有共享密钥明文，权限必须收紧 —— 这是本项目唯一落盘的秘密。"""
    registry = PeerRegistry(tmp_path)
    registry.trust(token())

    mode = stat.S_IMODE(registry.path.stat().st_mode)
    assert mode & 0o077 == 0


def test_corrupt_peer_file_is_reported_not_silently_ignored(tmp_path: Path) -> None:
    (tmp_path / "peers.json").write_text("{坏", encoding="utf-8")

    with pytest.raises(PeerError, match="损坏"):
        PeerRegistry(tmp_path)


def test_listing_is_sorted_and_hides_the_key(tmp_path: Path) -> None:
    # Arrange
    registry = PeerRegistry(tmp_path)
    registry.trust(PairingToken(node="zulu", endpoint=ENDPOINT, key=new_key()))
    registry.observe(node="alpha", endpoint=ENDPOINT, agents=())

    # Act
    listed = registry.all()

    # Assert
    assert [p.node for p in listed] == ["alpha", "zulu"]
    assert all(not hasattr(p, "key") for p in listed)


def test_record_renders_a_short_status(tmp_path: Path) -> None:
    registry = PeerRegistry(tmp_path)
    trusted = registry.trust(token())
    registry.observe(node="alpha", endpoint=ENDPOINT, agents=("a", "b"))
    seen = registry.get("alpha")

    assert isinstance(trusted, PeerRecord)
    assert trusted.status == "trusted"
    assert seen is not None and seen.status == "discovered"


# ---------- 多进程共享（联调时踩到的坑，留作回归） ----------


def test_changes_made_by_another_process_are_picked_up(tmp_path: Path) -> None:
    """一个节点上跑着好几个进程：每个 agentd 一个，加一个 serve。

    serve 学到的对端地址必须让 agentd 看得见，否则回信路由是断的。
    文件是唯一真相，内存只是缓存。
    """
    # Arrange：两个实例模拟两个进程
    agentd_view = PeerRegistry(tmp_path)
    serve_view = PeerRegistry(tmp_path)
    assert agentd_view.get("lab") is None

    # Act：serve 那个进程学到了地址
    serve_view.trust(token())
    serve_view.observe(node="lab", endpoint="http://learned:45778", agents=("runner",))

    # Assert：agentd 那个进程立刻能用上
    peer, key = agentd_view.require_trusted("lab")
    assert peer.endpoint == "http://learned:45778"
    assert key == KEY


def test_a_registry_does_not_reload_after_its_own_writes(tmp_path: Path) -> None:
    registry = PeerRegistry(tmp_path)
    registry.trust(token())

    assert registry.get("lab") is not None
    assert registry.key_for("lab") == KEY
