"""02-protocol §8 用例 5（重试部分）：指数退避与死信。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from anthill.core import outbox as outbox_module
from anthill.core.errors import MailboxError
from anthill.core.ids import new_id, now
from anthill.core.outbox import DELIVERY_LOCK_BUCKETS, MAX_ATTEMPTS, Outbox, backoff_delay


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


def test_first_enqueue_failure_never_leaves_meta_without_an_envelope(
    mailbox, make_task, monkeypatch: pytest.MonkeyPatch
):
    outbox = Outbox(mailbox)

    def fail_envelope(*_args, **_kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr(outbox_module, "atomic_write", fail_envelope)

    with pytest.raises(OSError, match="simulated crash"):
        outbox.enqueue(make_task())

    assert list(mailbox.pending.iterdir()) == []


def test_failure_update_only_writes_meta_not_the_existing_envelope(
    mailbox, make_task, monkeypatch: pytest.MonkeyPatch
):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())
    real_write = outbox_module.atomic_write
    names: list[str] = []

    def track_write(tmp_dir, dst_dir, name, data):
        names.append(name)
        return real_write(tmp_dir, dst_dir, name, data)

    monkeypatch.setattr(outbox_module, "atomic_write", track_write)

    outbox.mark_failed(entry, "网络抖动")

    reloaded = Outbox(mailbox).load_pending()
    assert names == [f"{entry.msg_id}.meta.json"]
    assert reloaded[0].attempts == 1
    assert reloaded[0].last_error == "网络抖动"


def test_enqueuing_the_same_envelope_again_preserves_retry_metadata(mailbox, make_task):
    outbox = Outbox(mailbox)
    env = make_task()
    outbox.mark_failed(outbox.enqueue(env), "网络抖动")

    reused = outbox.enqueue(env)

    assert reused.attempts == 1
    assert reused.last_error == "网络抖动"


def test_enqueuing_an_already_sent_envelope_does_not_recreate_pending(mailbox, make_task):
    outbox = Outbox(mailbox)
    env = make_task()
    outbox.mark_sent(outbox.enqueue(env))

    reused = outbox.enqueue(env)

    assert reused.envelope == env
    assert outbox.load_pending() == []


def test_delivery_lock_keeps_the_same_inode_after_message_leaves_pending(mailbox, make_task):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())
    lease = outbox.delivery_lock(entry.msg_id)
    lease.acquire()
    inode = lease.path.stat().st_ino
    lease.release()

    outbox.mark_sent(entry)
    replacement = outbox.delivery_lock(entry.msg_id)
    replacement.acquire()
    try:
        assert replacement.path.stat().st_ino == inode
    finally:
        replacement.release()


def test_delivery_locks_use_a_fixed_number_of_stable_buckets(mailbox):
    outbox = Outbox(mailbox)

    paths = {outbox.delivery_lock(f"message-{index}").path for index in range(4096)}

    assert len(paths) <= DELIVERY_LOCK_BUCKETS
    assert all(path.name.startswith("bucket-") for path in paths)


def test_exhausted_retries_move_to_dead_letters(mailbox, make_task):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())

    for _ in range(MAX_ATTEMPTS):
        entry = outbox.mark_failed(entry, "对端一直不在")

    assert entry.is_dead
    assert outbox.load_pending() == []
    assert len(outbox.dead_letters()) == 1
    assert "attempts=5" in (mailbox.dead / f"{entry.msg_id}.error.txt").read_text(encoding="utf-8")


def test_enqueue_does_not_silently_resurrect_a_dead_letter(mailbox, make_task):
    outbox = Outbox(mailbox)
    env = make_task()
    outbox.abandon(outbox.enqueue(env), "永久失败")

    with pytest.raises(MailboxError, match="显式 requeue"):
        outbox.enqueue(env)


def test_dead_reason_is_durable_before_the_envelope_moves(
    mailbox, make_task, monkeypatch: pytest.MonkeyPatch
):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())
    real_replace = Path.replace

    def crash_before_move(path: Path, target: Path):
        if path == mailbox.pending / f"{entry.msg_id}.json":
            raise OSError("simulated crash")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", crash_before_move)

    with pytest.raises(OSError, match="simulated crash"):
        outbox.abandon(entry, "永久失败")

    assert (mailbox.pending / f"{entry.msg_id}.json").is_file()
    assert not (mailbox.dead / f"{entry.msg_id}.json").exists()
    assert "last_error=永久失败" in (mailbox.dead / f"{entry.msg_id}.error.txt").read_text(
        encoding="utf-8"
    )


def test_sent_messages_leave_pending(mailbox, make_task):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())

    outbox.mark_sent(entry)

    assert outbox.load_pending() == []
    assert (mailbox.sent / f"{entry.msg_id}.json").is_file()
    assert not (mailbox.pending / f"{entry.msg_id}.meta.json").exists()


def test_sent_state_survives_crash_before_meta_cleanup(
    mailbox, make_task, monkeypatch: pytest.MonkeyPatch
):
    outbox = Outbox(mailbox)
    entry = outbox.enqueue(make_task())

    def crash(_self, _entry):
        raise OSError("simulated crash")

    monkeypatch.setattr(Outbox, "_drop_meta", crash)

    with pytest.raises(OSError, match="simulated crash"):
        outbox.mark_sent(entry)

    assert (mailbox.sent / f"{entry.msg_id}.json").is_file()
    assert not (mailbox.pending / f"{entry.msg_id}.json").exists()


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
