"""回信暂存区：给「连不回去的对端」用。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.envelope import Address, Envelope
from anthill.core.errors import MailboxError
from anthill.core.payloads import MessageType, TaskResultPayload
from anthill.core.spool import Spool


def reply_to(node: str = "laptop", agent: str = "cli") -> Envelope:
    return Envelope.new(
        sender=Address(node="lab-server", agent="runner"),
        recipient=Address(node=node, agent=agent),
        type=MessageType.TASK_RESULT,
        payload=TaskResultPayload(summary="3 failed, 12 passed"),
    )


def test_spooled_envelope_is_grouped_by_target_node(tmp_path: Path) -> None:
    # Arrange
    spool = Spool(tmp_path)
    env = reply_to()

    # Act
    path = spool.deposit(env)

    # Assert：按目标节点分目录，拉取方只看自己那一格
    assert path.parent.name == "laptop"
    assert spool.nodes() == ["laptop"]
    assert [p.name for p in spool.pending("laptop")] == [f"{env.id}.json"]


def test_spooled_envelope_survives_the_round_trip_unchanged(tmp_path: Path) -> None:
    """拉回去之后要走和同机投递完全一样的路径，所以 id/签名/thread 都不能变。"""
    spool = Spool(tmp_path)
    env = reply_to()
    spool.deposit(env)

    restored = spool.take("laptop", f"{env.id}.json")

    assert restored.id == env.id
    assert restored.thread == env.thread
    assert restored.payload.summary == "3 failed, 12 passed"  # type: ignore[union-attr]


def test_dropping_removes_the_file(tmp_path: Path) -> None:
    spool = Spool(tmp_path)
    env = reply_to()
    spool.deposit(env)

    spool.drop("laptop", f"{env.id}.json")

    assert spool.pending("laptop") == []
    spool.drop("laptop", f"{env.id}.json")  # 再删一次不该报错


def test_several_nodes_are_kept_apart(tmp_path: Path) -> None:
    spool = Spool(tmp_path)
    spool.deposit(reply_to("laptop"))
    spool.deposit(reply_to("desktop"))

    assert spool.nodes() == ["desktop", "laptop"]
    assert len(spool.pending("laptop")) == 1


def test_empty_spool_is_not_an_error(tmp_path: Path) -> None:
    spool = Spool(tmp_path)

    assert spool.nodes() == []
    assert spool.pending("laptop") == []


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "UPPER"])
def test_node_name_is_validated_before_it_becomes_a_directory(tmp_path: Path, bad: str) -> None:
    with pytest.raises(MailboxError, match="节点名"):
        Spool(tmp_path).dir_for(bad)


def test_taking_a_missing_envelope_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(MailboxError, match="读取"):
        Spool(tmp_path).take("laptop", "nope.json")
