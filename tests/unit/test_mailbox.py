"""02-protocol §8 用例 2/3：原子写与并发投递。"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from anthill.core.atomic import atomic_write, ensure_same_filesystem
from anthill.core.envelope import Envelope
from anthill.core.errors import MailboxError, ProtocolError
from anthill.core.mailbox import Mailbox

CONCURRENT_WRITERS = 100


def test_ensure_creates_full_maildir_tree(tmp_path: Path):
    mailbox = Mailbox(tmp_path / "mailbox").ensure()

    for directory in mailbox.all_dirs():
        assert directory.is_dir()
    assert mailbox.exists


def test_ensure_is_idempotent(tmp_path: Path):
    mailbox = Mailbox(tmp_path / "mailbox").ensure()
    mailbox.ensure()

    assert mailbox.exists


def test_deposit_writes_atomically_and_leaves_no_partials(mailbox, make_task):
    env = make_task()

    path = mailbox.deposit(env)

    assert path.parent == mailbox.new
    assert path.name == f"{env.id}.json"
    assert list(mailbox.tmp.iterdir()) == []  # tmp 里不留残骸
    assert Mailbox.read_envelope(path) == env


def test_new_never_exposes_half_written_files(mailbox, make_task, monkeypatch):
    """用例 2：模拟写一半崩溃 —— new/ 里必须永远看不到残缺文件。"""
    real_replace = os.replace

    def crash_before_rename(src, dst):
        raise OSError("模拟：rename 前进程被 kill")

    monkeypatch.setattr(os, "replace", crash_before_rename)
    with pytest.raises(MailboxError):
        mailbox.deposit(make_task())
    monkeypatch.setattr(os, "replace", real_replace)

    assert mailbox.list_new() == []
    assert list(mailbox.tmp.iterdir()) == []  # 失败路径也要清理 .part


def test_concurrent_writers_lose_nothing_and_duplicate_nothing(mailbox, make_task):
    """用例 3：100 个并发写者投同一邮箱，无锁，无丢失无重复。"""
    envelopes = [make_task() for _ in range(CONCURRENT_WRITERS)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(mailbox.deposit, envelopes))

    names = {p.name for p in mailbox.list_new()}
    assert names == {f"{env.id}.json" for env in envelopes}
    assert len(names) == CONCURRENT_WRITERS


def test_list_new_is_time_ordered(mailbox, make_task):
    envelopes = [make_task() for _ in range(10)]
    for env in envelopes:
        mailbox.deposit(env)

    listed = [p.stem for p in mailbox.list_new()]

    assert listed == [env.id for env in envelopes]


def test_claim_then_archive_moves_through_the_lifecycle(mailbox, make_task):
    path = mailbox.deposit(make_task())

    claimed = mailbox.claim(path)
    assert claimed.parent == mailbox.cur
    assert mailbox.list_new() == []

    archived = mailbox.archive(claimed)
    assert archived.parent.parent == mailbox.done
    assert not claimed.exists()


def test_recover_stale_requeues_messages_left_in_cur(mailbox, make_task):
    """agentd 被 kill -9 时正在处理的消息，重启后必须回到 new。"""
    claimed = mailbox.claim(mailbox.deposit(make_task()))

    recovered = mailbox.recover_stale()

    assert len(recovered) == 1
    assert len(mailbox.list_new()) == 1
    assert list(mailbox.cur.iterdir()) == []
    assert not claimed.exists()


def test_deposit_to_missing_mailbox_fails_loudly(tmp_path: Path, make_task):
    with pytest.raises(MailboxError):
        Mailbox(tmp_path / "never-created").deposit(make_task())


def test_quarantine_isolates_unparseable_files_with_reason(mailbox):
    broken = mailbox.new / "01J000000000000000000000AA.json"
    broken.write_text("{ 不是合法 JSON", encoding="utf-8")
    claimed = mailbox.claim(broken)

    mailbox.quarantine(claimed, "JSON 解析失败")

    quarantined = mailbox.done / "invalid"
    assert (quarantined / broken.name).is_file()
    assert "JSON 解析失败" in (quarantined / f"{broken.name}.reason.txt").read_text(
        encoding="utf-8"
    )


def test_read_envelope_rejects_empty_file(mailbox):
    empty = mailbox.new / "empty.json"
    empty.write_bytes(b"")

    with pytest.raises(ProtocolError):
        Mailbox.read_envelope(empty)


def test_sweep_tmp_only_removes_stale_parts(mailbox):
    stale = mailbox.tmp / "old.json.part"
    fresh = mailbox.tmp / "new.json.part"
    stale.write_text("x", encoding="utf-8")
    fresh.write_text("x", encoding="utf-8")
    os.utime(stale, (0, 0))

    removed = mailbox.sweep_tmp(max_age=timedelta(minutes=5))

    assert removed == 1
    assert fresh.exists() and not stale.exists()


def test_atomic_write_rejects_cross_filesystem_dirs(tmp_path: Path, monkeypatch):
    """rename 跨文件系统会退化成拷贝，原子性就没了 —— 必须启动期就拦住。"""
    src, dst = tmp_path / "a", tmp_path / "b"
    src.mkdir()
    dst.mkdir()
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if Path(path) == dst:
            return os.stat_result(
                (result.st_mode, result.st_ino, result.st_dev + 1, *tuple(result)[3:])
            )
        return result

    monkeypatch.setattr(os, "stat", fake_stat)

    with pytest.raises(MailboxError, match="同一文件系统"):
        ensure_same_filesystem(src, dst)
    with pytest.raises(MailboxError):
        atomic_write(src, dst, "x.json", b"{}")


def test_envelope_survives_disk_round_trip(mailbox, make_task):
    env = make_task()

    restored = Mailbox.read_envelope(mailbox.deposit(env))

    assert isinstance(restored, Envelope)
    assert restored.payload == env.payload
