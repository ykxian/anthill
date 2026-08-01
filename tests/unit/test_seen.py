"""02-protocol §8 用例 4（存储层）：幂等去重。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from anthill.core.ids import new_id
from anthill.core.seen import SeenStore


def test_first_mark_is_new_and_second_is_duplicate(tmp_path: Path):
    store = SeenStore(tmp_path / "seen.db")
    msg_id = new_id()

    assert store.mark(msg_id) is True
    assert store.mark(msg_id) is False
    assert store.has(msg_id)


def test_dedupe_survives_restart(tmp_path: Path):
    """内存 set 做不到这点：agentd 重启后重复投递仍必须被识别。"""
    path = tmp_path / "seen.db"
    msg_id = new_id()
    with SeenStore(path) as store:
        store.mark(msg_id)

    with SeenStore(path) as reopened:
        assert reopened.mark(msg_id) is False


def test_concurrent_marks_elect_exactly_one_winner(tmp_path: Path):
    store = SeenStore(tmp_path / "seen.db")
    msg_id = new_id()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.mark(msg_id), range(50)))

    assert results.count(True) == 1


def test_purge_drops_only_old_entries(tmp_path: Path):
    store = SeenStore(tmp_path / "seen.db")
    store.mark(new_id())

    assert store.purge(timedelta(days=7)) == 0
    assert store.purge(timedelta(seconds=0)) == 1


def test_unknown_id_is_not_seen(tmp_path: Path):
    store = SeenStore(tmp_path / "seen.db")

    assert not store.has(new_id())
