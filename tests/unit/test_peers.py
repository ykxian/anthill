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

    # Assert：广播能刷新「见过」和 Agent 清单，但**不能改投递地址**，也不能降级信任。
    # 这里以前断言的是 endpoint 被改成广播里那个 —— 那正是被劫持的样子。
    peer = registry.get("lab")
    assert peer is not None and before is not None
    assert peer.trusted
    assert peer.endpoint == ENDPOINT
    assert peer.agents == ("runner",)
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

    # Act：serve 那个进程完成了配对（配对是认证过的，地址以它为准）
    serve_view.trust(PairingToken(node="lab", endpoint="http://learned:45778", key=KEY))
    serve_view.observe(node="lab", endpoint="http://learned:45778", agents=("runner",))

    # Assert：agentd 那个进程立刻能用上
    peer, key = agentd_view.require_trusted("lab")
    assert peer.endpoint == "http://learned:45778"
    assert peer.agents == ("runner",)
    assert key == KEY


def test_a_registry_does_not_reload_after_its_own_writes(tmp_path: Path) -> None:
    registry = PeerRegistry(tmp_path)
    registry.trust(token())

    assert registry.get("lab") is not None
    assert registry.key_for("lab") == KEY


# ---------- 广播不能改路由 ----------


def test_a_forged_beacon_cannot_move_a_trusted_peer(tmp_path: Path):
    """伪造一个 UDP 包就能劫持已信任节点的全部出站消息 —— 真出过的洞。

    `observe()` 以前是 `endpoint or existing.endpoint`：来包带地址就无条件覆盖，
    不看对方是不是已信任。而 beacon 收包**没有任何认证**（就是个组播 UDP 包），
    这个 endpoint 又正是投递用的 URL。局域网是明文 HTTP，
    后果是任务内容直接泄露 + 静默 DoS。discovery 现在还是默认开着的。
    """
    registry = PeerRegistry(tmp_path)
    registry.trust(PairingToken(node="lab", key=b"k" * 32, endpoint="http://10.0.0.5:45778"))

    registry.observe(node="lab", endpoint="http://192.168.9.9:45778", agents=("coder",))

    peer, _ = registry.require_trusted("lab")
    assert peer.endpoint == "http://10.0.0.5:45778", "投递地址被一个没认证的广播包改掉了"


def test_the_conflicting_address_is_recorded_so_a_human_can_see_it(tmp_path: Path):
    """不能只是默默忽略 —— 对方可能真换了 IP，那个判断得由人来做。"""
    registry = PeerRegistry(tmp_path)
    registry.trust(PairingToken(node="lab", key=b"k" * 32, endpoint="http://10.0.0.5:45778"))

    registry.observe(node="lab", endpoint="http://192.168.9.9:45778", agents=())

    peer = registry.get("lab")
    assert peer is not None
    assert peer.seen_endpoint == "http://192.168.9.9:45778"
    assert peer.endpoint_conflict is True


def test_discovery_still_fills_in_the_address_before_pairing(tmp_path: Path):
    """还没信任的对端，广播是唯一的信息来源 —— 这条路不能一起堵死，
    否则配对前根本不知道该往哪儿连。（反正没信任就投不出去，见 require_trusted。）"""
    registry = PeerRegistry(tmp_path)

    registry.observe(node="newbie", endpoint="http://10.0.0.9:45778", agents=())

    peer = registry.get("newbie")
    assert peer is not None
    assert peer.endpoint == "http://10.0.0.9:45778"
    assert peer.endpoint_conflict is False  # 没信任就谈不上冲突


def test_pairing_again_clears_the_warning(tmp_path: Path):
    """对方真换了 IP 时的正路：重新配对（认证过的），告警随之消失。"""
    registry = PeerRegistry(tmp_path)
    registry.trust(PairingToken(node="lab", key=b"k" * 32, endpoint="http://10.0.0.5:45778"))
    registry.observe(node="lab", endpoint="http://10.0.0.7:45778", agents=())

    registry.trust(
        PairingToken(node="lab", key=b"k" * 32, endpoint="http://10.0.0.7:45778"), replace=True
    )

    peer = registry.get("lab")
    assert peer is not None
    assert peer.endpoint == "http://10.0.0.7:45778"
    assert peer.endpoint_conflict is False
