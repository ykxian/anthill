"""从归档里把对话拼回来。

盯的是三件容易搞错的事：
- **只读收件方的归档**，所以每条消息恰好出现一次（并进发件方记录就会重影）；
- 回执不进正文，但条数得报出来（悄悄扔掉和「本来就没有」是两回事）；
- thread 内按时刻排，thread 之间按最后活跃排。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from anthill.core.envelope import Envelope
from anthill.core.ids import new_id, now
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType, ReceiptPayload
from anthill.core.router import Address
from anthill.core.traffic import conversations


def _layout(tmp_path: Path) -> NodeLayout:
    layout = NodeLayout(workspace=tmp_path)
    layout.ensure_base()
    return layout


def _deliver(layout: NodeLayout, env: Envelope, *, day: str = "2026-08-13") -> None:
    """把一个信封放进**收件方**的归档 —— 真实里 Mailbox.archive 就是这么放的。"""
    done = layout.mailbox_dir(env.to.agent) / "inbox" / "done" / day
    done.mkdir(parents=True, exist_ok=True)
    (done / f"{env.id}.json").write_bytes(env.to_json_bytes())


def _chat(frm: str, to: str, body: str, *, thread: str, at) -> Envelope:
    return Envelope(
        id=new_id(),
        ts=at,
        from_=Address(node="n", agent=frm),
        to=Address(node="n", agent=to),
        type=MessageType.CHAT,
        thread=thread,
        payload=ChatPayload(body=body),
    )


def test_two_agents_talking_becomes_one_conversation(tmp_path: Path) -> None:
    # Arrange：tst1 和 tst2 你一句我一句
    layout = _layout(tmp_path)
    thread, start = new_id(), now()
    lines = [("tst1", "tst2", "在吗"), ("tst2", "tst1", "在"), ("tst1", "tst2", "那开始吧")]
    for index, (frm, to, body) in enumerate(lines):
        _deliver(layout, _chat(frm, to, body, thread=thread, at=start + timedelta(seconds=index)))

    # Act
    result = conversations(layout)

    # Assert
    assert len(result["threads"]) == 1
    convo = result["threads"][0]
    assert convo["peers"] == ["n:tst1", "n:tst2"]
    assert [m["body"] for m in convo["messages"]] == ["在吗", "在", "那开始吧"]
    assert convo["count"] == 3


def test_each_message_appears_once(tmp_path: Path) -> None:
    """同一条消息不该出现两遍 —— 只读收件方的归档就天然做到了。"""
    # Arrange
    layout = _layout(tmp_path)
    env = _chat("tst1", "tst2", "只说一次", thread=new_id(), at=now())
    _deliver(layout, env)
    # 发件方那边也留了一份（真实里是 sent/），不该被算进来
    sent = layout.mailbox_dir("tst1") / "sent"
    sent.mkdir(parents=True, exist_ok=True)
    (sent / f"{env.id}.json").write_bytes(env.to_json_bytes())

    # Act
    result = conversations(layout)

    # Assert
    assert [m["body"] for m in result["threads"][0]["messages"]] == ["只说一次"]


def test_receipts_are_counted_not_shown(tmp_path: Path) -> None:
    """回执没正文，每句话配一条 —— 掺进去会把 3 句话显示成 6 条，一半是空的。"""
    # Arrange
    layout = _layout(tmp_path)
    thread, at = new_id(), now()
    _deliver(layout, _chat("tst1", "tst2", "干活", thread=thread, at=at))
    _deliver(
        layout,
        Envelope(
            id=new_id(),
            ts=at,
            from_=Address(node="n", agent="tst2"),
            to=Address(node="n", agent="tst1"),
            type=MessageType.RECEIPT_ACCEPTED,
            thread=thread,
            payload=ReceiptPayload(ref=new_id()),
        ),
    )

    # Act
    convo = conversations(layout)["threads"][0]

    # Assert
    assert [m["body"] for m in convo["messages"]] == ["干活"]
    assert convo["receipts"] == 1


def test_threads_ordered_by_last_activity(tmp_path: Path) -> None:
    # Arrange
    layout = _layout(tmp_path)
    old, fresh = new_id(), new_id()
    base = now()
    _deliver(layout, _chat("a", "b", "很久以前", thread=old, at=base - timedelta(hours=3)))
    _deliver(layout, _chat("a", "b", "刚刚", thread=fresh, at=base))

    # Act
    result = conversations(layout)

    # Assert
    assert [t["messages"][0]["body"] for t in result["threads"]] == ["刚刚", "很久以前"]


def test_agent_removed_from_config_keeps_its_history(tmp_path: Path) -> None:
    """看的是磁盘上的邮箱，不是配置里列着的 —— 删掉一个 Agent，它说过的话还在。"""
    # Arrange
    layout = _layout(tmp_path)
    _deliver(layout, _chat("gone", "still", "我还在记录里", thread=new_id(), at=now()))

    # Act
    result = conversations(layout)

    # Assert
    assert result["threads"][0]["messages"][0]["body"] == "我还在记录里"


def test_empty_workspace_is_empty_not_an_error(tmp_path: Path) -> None:
    # Act / Assert
    assert conversations(_layout(tmp_path))["threads"] == []


def test_threads_with_a_human_agent_are_flagged(tmp_path: Path) -> None:
    """「你跟 Agent 说的」和「Agent 之间说的」是两个问题 —— 页面上要能分开看。"""
    # Arrange
    layout = _layout(tmp_path)
    _deliver(layout, _chat("cli", "tst1", "帮我看下", thread=new_id(), at=now()))
    _deliver(layout, _chat("tst1", "tst2", "你那边呢", thread=new_id(), at=now()))

    # Act
    result = conversations(layout, humans=frozenset({"cli"}))

    # Assert
    flags = {t["messages"][0]["body"]: t["with_human"] for t in result["threads"]}
    assert flags == {"帮我看下": True, "你那边呢": False}


def test_just_sent_shows_before_the_other_side_archives_it(tmp_path: Path) -> None:
    """对方没启动时，归档里永远不会有这条 —— 不补就看着像消息丢了。"""
    # Arrange：只有本机记的发件，收件方那边什么都没有
    layout = _layout(tmp_path)
    thread = new_id()
    mine = {
        "id": new_id(),
        "ts": now().isoformat(),
        "frm": "n:cli",
        "to": "n:sleepy",
        "kind": "chat",
        "thread": thread,
        "body": "在吗",
    }

    # Act
    result = conversations(layout, extra=[mine])

    # Assert
    assert [m["body"] for m in result["threads"][0]["messages"]] == ["在吗"]


def test_archived_copy_wins_over_the_local_note(tmp_path: Path) -> None:
    """同一条别出现两遍 —— 归档那份带真正的投递时刻，更权威。"""
    # Arrange
    layout = _layout(tmp_path)
    env = _chat("cli", "tst1", "只说一次", thread=new_id(), at=now())
    _deliver(layout, env)
    same = {
        "id": env.id,
        "ts": env.ts.isoformat(),
        "frm": "n:cli",
        "to": "n:tst1",
        "kind": "chat",
        "thread": env.thread,
        "body": "只说一次",
    }

    # Act
    result = conversations(layout, extra=[same])

    # Assert
    assert [m["body"] for m in result["threads"][0]["messages"]] == ["只说一次"]
