"""删对话记录。

盯的是那条**必须守住的线**：`inbox/done/` 是记录，删了只是少了历史；
`inbox/new/` 是还没被处理的**实信**，删了就是丢件。默认只删前者。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.envelope import Envelope
from anthill.core.errors import AntHillError
from anthill.core.ids import new_id, now
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType
from anthill.core.router import Address
from anthill.core.traffic import conversations
from anthill.core.traffic_purge import doomed, purge


def _layout(tmp_path: Path) -> NodeLayout:
    return NodeLayout(workspace=tmp_path).ensure_base()


def _env(frm: str, to: str, body: str, *, thread: str) -> Envelope:
    return Envelope(
        id=new_id(),
        ts=now(),
        from_=Address(node="n", agent=frm),
        to=Address(node="n", agent=to),
        type=MessageType.CHAT,
        thread=thread,
        payload=ChatPayload(body=body),
    )


def _archived(layout: NodeLayout, env: Envelope) -> Path:
    done = layout.mailbox_dir(env.to.agent) / "inbox" / "done" / "2026-08-14"
    done.mkdir(parents=True, exist_ok=True)
    path = done / f"{env.id}.json"
    path.write_bytes(env.to_json_bytes())
    return path


def _unread(layout: NodeLayout, env: Envelope) -> Path:
    new = layout.mailbox_dir(env.to.agent) / "inbox" / "new"
    new.mkdir(parents=True, exist_ok=True)
    path = new / f"{env.id}.json"
    path.write_bytes(env.to_json_bytes())
    return path


def test_unread_mail_survives_a_wipe(tmp_path: Path) -> None:
    """**这条是底线。** 清记录不能把还没被处理的信一起带走 —— 那是丢件。"""
    # Arrange
    layout = _layout(tmp_path)
    thread = new_id()
    old = _archived(layout, _env("a", "b", "聊过的", thread=thread))
    live = _unread(layout, _env("a", "b", "还没读的", thread=thread))

    # Act
    result = purge(layout)

    # Assert
    assert not old.exists()
    assert live.exists(), "还没被处理的信被删掉了 —— 这是丢件"
    assert result["kept_pending"] == 1


def test_unread_mail_goes_only_when_asked(tmp_path: Path) -> None:
    """`cli` 那种没有处理者的信箱，人确实需要能清掉 —— 但得显式说。"""
    # Arrange
    layout = _layout(tmp_path)
    live = _unread(layout, _env("a", "cli", "永远没人读", thread=new_id()))

    # Act
    result = purge(layout, drop_pending=True)

    # Assert
    assert not live.exists()
    assert result["dropped"] == 1


def test_wiping_one_thread_leaves_the_others(tmp_path: Path) -> None:
    # Arrange
    layout = _layout(tmp_path)
    doomed_thread, keeper = new_id(), new_id()
    gone = _archived(layout, _env("a", "b", "删我", thread=doomed_thread))
    stays = _archived(layout, _env("a", "b", "留我", thread=keeper))

    # Act
    purge(layout, thread=doomed_thread)

    # Assert
    assert not gone.exists()
    assert stays.exists()
    assert [t["messages"][0]["body"] for t in conversations(layout)["threads"]] == ["留我"]


def test_a_message_arriving_mid_confirm_aborts_the_whole_thing(tmp_path: Path) -> None:
    """闸不是「你确定吗」，是「你确定要删**这几条**吗」。"""
    # Arrange：人看到的是 1 条，确认之前又来了一条
    layout = _layout(tmp_path)
    seen = doomed(layout).count
    kept = _archived(layout, _env("a", "b", "确认期间刚到的", thread=new_id()))

    # Act / Assert
    with pytest.raises(AntHillError, match="对不上"):
        purge(layout, expect=seen)
    assert kept.exists(), "作废的那一次不该删掉任何东西"


def test_wiping_everything_empties_the_page(tmp_path: Path) -> None:
    # Arrange
    layout = _layout(tmp_path)
    for i in range(3):
        _archived(layout, _env("a", "b", f"第 {i} 条", thread=new_id()))

    # Act
    purge(layout)

    # Assert
    assert conversations(layout)["threads"] == []


def test_quarantined_envelopes_are_not_touched(tmp_path: Path) -> None:
    """`done/invalid/` 是解析不了的信封，另有隔离的道理，不在「清对话」范围内。"""
    # Arrange
    layout = _layout(tmp_path)
    bad = layout.mailbox_dir("b") / "inbox" / "done" / "invalid"
    bad.mkdir(parents=True, exist_ok=True)
    junk = bad / "whatever.json"
    junk.write_text("{ 半个", encoding="utf-8")

    # Act
    purge(layout)

    # Assert
    assert junk.exists()


def test_nothing_to_delete_is_not_an_error(tmp_path: Path) -> None:
    # Act
    result = purge(_layout(tmp_path))

    # Assert
    assert result["removed"] == 0
