"""02-protocol §8 用例 5（重试部分）：指数退避与死信。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from anthill.core.errors import MailboxError
from anthill.core.ids import new_id, now
from anthill.core.outbox import MAX_ATTEMPTS, Outbox, backoff_delay


def test_enqueued_message_is_immediately_due(mailbox, make_task):
    outbox = Outbox(mailbox)

    entry = outbox.enqueue(make_task())

    assert entry.is_due()
    assert [e.msg_id for e in outbox.due()] == [entry.msg_id]


def test_backoff_is_exponential():
    delays = [backoff_delay(n) for n in range(1, 6)]

    assert delays == [timedelta(seconds=s) for s in (1, 2, 4, 8, 16)]


def test_failure_pushes_next_attempt_into_the_future(mailbox, make_task):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())

    failed = outbox.mark_failed(entry, "对方邮箱不存在")

    assert failed.attempts == 1
    assert not failed.is_due(now())
    assert failed.is_due(now() + timedelta(seconds=2))
    assert outbox.due(now()) == []


def test_retry_metadata_survives_reload(mailbox, make_task):
    """agentd 崩溃重启后，重试计数不能归零，否则会无限重试。"""
    outbox = Outbox(mailbox)
    outbox.mark_failed(outbox.enqueue(make_task()), "网络抖动")

    reloaded = Outbox(mailbox).load_pending()

    assert len(reloaded) == 1
    assert reloaded[0].attempts == 1
    assert reloaded[0].last_error == "网络抖动"


def test_exhausted_retries_move_to_dead_letters(mailbox, make_task):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())

    for _ in range(MAX_ATTEMPTS):
        entry = outbox.mark_failed(entry, "对端一直不在")

    assert entry.is_dead
    assert outbox.load_pending() == []
    assert len(outbox.dead_letters()) == 1
    assert "attempts=5" in (mailbox.dead / f"{entry.msg_id}.error.txt").read_text(encoding="utf-8")


def test_sent_messages_leave_pending(mailbox, make_task):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())

    outbox.mark_sent(entry)

    assert outbox.load_pending() == []
    assert (mailbox.sent / f"{entry.msg_id}.json").is_file()
    assert not (mailbox.pending / f"{entry.msg_id}.meta.json").exists()


def test_corrupted_pending_entry_does_not_block_others(mailbox, make_task):
    outbox = Outbox(mailbox)
    good = outbox.enqueue(make_task())
    (mailbox.pending / "01J000000000000000000000ZZ.json").write_text("坏文件", encoding="utf-8")

    pending = outbox.load_pending()

    assert [e.msg_id for e in pending] == [good.msg_id]
    assert list(mailbox.pending.glob("*.corrupt"))  # 隔离而非静默丢弃


# ---------- 死信得有出路 ----------


def test_a_dead_letter_records_who_it_was_for_and_why(mailbox, make_task):
    """看不懂原因的死信等于没有死信。"""
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())

    outbox.abandon(entry, "对端 agentd 没启动")

    letter = outbox.dead_letter(entry.msg_id)
    assert letter is not None
    assert letter.to == str(entry.envelope.to)
    assert "没启动" in letter.reason


def test_a_dead_letter_can_be_put_back_for_another_try(mailbox, make_task):
    """进死信最常见的原因就是「对端晚起了十秒」—— 修好之后必须能重投。
    以前唯一的恢复手段是手动 mv 文件。"""
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())
    outbox.abandon(entry, "连不上")

    requeued = outbox.requeue_dead(entry.msg_id)

    assert requeued.attempts == 0, "重投不清零的话，放回去立刻又被判死"
    assert [e.msg_id for e in outbox.load_pending()] == [entry.msg_id]
    assert outbox.dead_letters() == []


def test_requeueing_something_that_is_not_dead_is_an_error(mailbox):
    outbox = Outbox(mailbox)

    with pytest.raises(MailboxError):
        outbox.requeue_dead(new_id())


def test_a_dead_letter_can_be_dropped(mailbox, make_task):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())
    outbox.abandon(entry, "不要了")

    assert outbox.drop_dead(entry.msg_id) is True
    assert outbox.drop_dead(entry.msg_id) is False
    assert outbox.dead_letters() == []
