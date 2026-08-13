"""归档读回来还得是原来那回事。

这些用例盯的是一个**具体的坑**：`done/<id>.md` 有两种可能 —— 回出去的正文，
或者「压根没回就归档了」时留下的收件 note。两者混淆的后果是页面上显示
「它回了：---\\nfrom: …」这种鬼东西。判据见 bridge_history._read。
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from anthill.adapters.bridge import DONE, render_request
from anthill.adapters.bridge_history import recent
from anthill.core.envelope import Envelope
from anthill.core.ids import new_id, now
from anthill.core.payloads import ChatPayload, MessageType
from anthill.core.router import Address


def _incoming(body: str) -> Envelope:
    return Envelope(
        id=new_id(),
        ts=now(),
        from_=Address(node="lab", agent="coder"),
        to=Address(node="here", agent="cc"),
        type=MessageType.CHAT,
        thread=new_id(),
        payload=ChatPayload(body=body),
    )


def _archive(root: Path, env: Envelope, *, reply: str | None) -> None:
    done = root / DONE
    done.mkdir(parents=True, exist_ok=True)
    (done / f"{env.id}.json").write_bytes(env.to_json_bytes())
    # 回了 → outbox 归档覆盖掉收件 note；没回 → 留下的就是收件 note 本身
    text = reply if reply is not None else render_request(env)
    (done / f"{env.id}.md").write_text(text, encoding="utf-8")


def test_pairs_incoming_with_its_reply(tmp_path: Path) -> None:
    # Arrange
    env = _incoming("这块接口能改成异步的吗")
    _archive(tmp_path, env, reply="能，我这边只用了返回值")

    # Act
    log = recent(tmp_path)

    # Assert
    assert len(log) == 1
    assert log[0]["direction"] == "in"
    assert log[0]["peer"] == "lab:coder"
    assert log[0]["incoming"] == "这块接口能改成异步的吗"
    assert log[0]["reply"] == "能，我这边只用了返回值"
    assert log[0]["answered"] is True


def test_archived_without_reply_is_not_mistaken_for_one(tmp_path: Path) -> None:
    """没回就归档时，done/<id>.md 是收件 note —— 别把它当成回复显示出去。"""
    # Arrange
    env = _incoming("在吗")
    _archive(tmp_path, env, reply=None)

    # Act
    log = recent(tmp_path)

    # Assert
    assert log[0]["answered"] is False
    assert log[0]["reply"] == ""
    assert log[0]["incoming"] == "在吗"


def test_outgoing_note_without_envelope(tmp_path: Path) -> None:
    """主动发起的那条没有配对信封 —— 文件名是随手起的。"""
    # Arrange
    done = tmp_path / DONE
    done.mkdir(parents=True)
    (done / "hello.md").write_text("---\nto: coder\n---\n我先说一句", encoding="utf-8")

    # Act
    log = recent(tmp_path)

    # Assert
    assert log[0]["direction"] == "out"
    assert log[0]["peer"] == "coder"
    assert log[0]["reply"] == "我先说一句"


def test_failed_send_is_flagged(tmp_path: Path) -> None:
    # Arrange
    done = tmp_path / DONE
    done.mkdir(parents=True)
    (done / "oops.md.failed").write_text("---\nto: nobody\n---\n发不出去", encoding="utf-8")

    # Act
    log = recent(tmp_path)

    # Assert
    assert log[0]["failed"] is True


def test_newest_first_and_capped(tmp_path: Path) -> None:
    # Arrange
    envs = [_incoming(f"第 {i} 条") for i in range(6)]
    for env in envs:
        _archive(tmp_path, env, reply=f"收到 {env.id[-4:]}")

    # Act
    log = recent(tmp_path, limit=3)

    # Assert：只要 3 条，且都是完整配对（不能出现半条）
    assert len(log) == 3
    assert all(entry["incoming"] and entry["reply"] for entry in log)


def test_ordered_by_when_it_happened_not_when_it_was_filed(tmp_path: Path) -> None:
    """一条晚归档的**旧**消息不能排到新消息前面。

    没人回的消息会一直躺在 inbox/ 里，等对话结束才归档 —— 它的 mtime 因此
    比后来收到又秒回的消息还新。按 mtime 排就会把这段对话读乱。
    """
    # Arrange：old 先发生，new 晚 5 分钟发生；但 old 的归档时刻反而更晚
    old = _incoming("我先说的")
    new = _incoming("我后说的")
    object.__setattr__(new, "ts", old.ts + timedelta(minutes=5))
    _archive(tmp_path, new, reply="秒回")
    _archive(tmp_path, old, reply=None)
    # mtime 得显式拉开 —— 两次写盘只差几微秒，造不出真实里那种反转
    for stem, mtime in ((new.id, 1_000.0), (old.id, 9_000.0)):
        for suffix in (".json", ".md"):
            os.utime(tmp_path / DONE / f"{stem}{suffix}", (mtime, mtime))

    # Act
    log = recent(tmp_path)

    # Assert
    assert [entry["incoming"] for entry in log] == ["我后说的", "我先说的"]


def test_missing_archive_is_empty_not_an_error(tmp_path: Path) -> None:
    # Act / Assert
    assert recent(tmp_path / "nope") == []
