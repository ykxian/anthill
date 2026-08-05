"""02-protocol §8 用例 4（存储层）：幂等去重，以及崩溃后必须能重放。

去重和重放是一枚硬币的两面，容易只做对一面：
挡得太狠，崩溃中断的消息就永远不会重跑（「最多一次」）；挡得太松，
一次重复投递就被处理两遍。所以这里两面都测。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from anthill.core.ids import new_id
from anthill.core.seen import Claim, SeenStore


def test_first_claim_is_new_and_second_is_duplicate(tmp_path: Path):
    store = SeenStore(tmp_path / "seen.db")
    msg_id = new_id()

    assert store.claim(msg_id) is Claim.FIRST
    assert store.claim(msg_id) is Claim.DUPLICATE  # 同一个进程实例正在处理
    assert store.has(msg_id)


def test_dedupe_survives_restart(tmp_path: Path):
    """内存 set 做不到这点：agentd 重启后重复投递仍必须被识别。"""
    path = tmp_path / "seen.db"
    msg_id = new_id()
    with SeenStore(path) as store:
        store.claim(msg_id)
        store.complete(msg_id)  # 干完了

    with SeenStore(path) as reopened:
        assert reopened.claim(msg_id) is Claim.DUPLICATE


def test_concurrent_claims_elect_exactly_one_winner(tmp_path: Path):
    store = SeenStore(tmp_path / "seen.db")
    msg_id = new_id()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim(msg_id), range(50)))

    assert results.count(Claim.FIRST) == 1
    assert results.count(Claim.RETRY) == 0  # 同一个进程实例，不该有人「接手」


# ---------- 崩在 handler 中间：这条必须能重跑 ----------


def test_a_message_interrupted_mid_handler_is_processed_again(tmp_path: Path):
    """这一条直接关系到文件邮箱最核心的卖点。

    原来只有一个 `mark()`：进 `_dispatch` 就登记，之后才跑 handler。
    kill -9 打在 handler 中间时，`recover_stale()` 确实把信从 cur/ 退回了 new/，
    但重新处理时 `mark()` 返回 False —— 只补一条回执就 return，**handler 永远不重跑**。
    退信那一步成了安慰剂，真实语义是「最多一次」而不是宣称的「至少一次」。
    """
    path = tmp_path / "seen.db"
    msg_id = new_id()

    with patch("anthill.core.seen.RUNTIME_TOKEN", "pid-1:aaa"):
        first = SeenStore(path)
        assert first.claim(msg_id) is Claim.FIRST
        first.close()  # ← 就在这里被 kill，complete() 从没被调用

    with patch("anthill.core.seen.RUNTIME_TOKEN", "pid-2:bbb"):
        after_restart = SeenStore(path)
        assert after_restart.claim(msg_id) is Claim.RETRY, "崩溃中断的消息必须重跑"


def test_a_message_that_finished_is_not_processed_again_after_restart(tmp_path: Path):
    """另一面：真干完了的，重启后重放也不能再处理一遍。"""
    path = tmp_path / "seen.db"
    msg_id = new_id()

    with patch("anthill.core.seen.RUNTIME_TOKEN", "pid-1:aaa"):
        first = SeenStore(path)
        first.claim(msg_id)
        first.complete(msg_id)
        first.close()

    with patch("anthill.core.seen.RUNTIME_TOKEN", "pid-2:bbb"):
        assert SeenStore(path).claim(msg_id) is Claim.DUPLICATE


def test_a_reused_pid_does_not_swallow_the_replay(tmp_path: Path):
    """只用 pid 不够：机器重启后 pid 会被复用，那样崩溃前的记录会被误判成
    「本进程正在处理」，重放又被吃掉。令牌里那段随机量就是为了消掉这个歧义。"""
    path = tmp_path / "seen.db"
    msg_id = new_id()

    with patch("anthill.core.seen.RUNTIME_TOKEN", "4242:aaa"):
        SeenStore(path).claim(msg_id)

    with patch("anthill.core.seen.RUNTIME_TOKEN", "4242:bbb"):  # 同一个 pid，不同实例
        assert SeenStore(path).claim(msg_id) is Claim.RETRY


# ---------- 清理 ----------


def test_purge_drops_only_old_and_finished_entries(tmp_path: Path):
    store = SeenStore(tmp_path / "seen.db")
    finished, in_flight = new_id(), new_id()
    for msg_id in (finished, in_flight):
        store.claim(msg_id)
    store.complete(finished)

    assert store.purge(timedelta(days=7)) == 0
    assert store.purge(timedelta(seconds=0)) == 1  # 只清了完成的那条
    assert store.has(in_flight), "处理中的记录被清掉 = 放行一次重复处理"


def test_unknown_id_is_not_seen(tmp_path: Path):
    store = SeenStore(tmp_path / "seen.db")

    assert not store.has(new_id())


def test_an_old_database_gains_the_new_columns(tmp_path: Path):
    """升级不能把已有的去重记录冲掉 —— 那等于放行一整轮重复处理。"""
    import sqlite3

    path = tmp_path / "seen.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE seen (id TEXT PRIMARY KEY, seen_at TEXT NOT NULL, expires_at TEXT);"
    )
    old.execute("INSERT INTO seen VALUES ('01OLD', '2020-01-01T00:00:00+00:00', NULL)")
    old.commit()
    old.close()

    store = SeenStore(path)

    assert store.has("01OLD")
    # 老记录没有 done_at 也没有 claimed_by —— 当成「上个实例没干完」，重放时重跑
    assert store.claim("01OLD") is Claim.RETRY
