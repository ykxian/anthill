"""已见消息集（seen.db）—— 幂等去重（02-protocol §4）。

传输层只保证「至少一次投递」，接收方按消息 ULID 去重，两者合成**恰好一次处理**。
重复消息虽然丢弃，但仍要补发回执，否则发送方的状态机永远收敛不了。

## 为什么是两阶段，而不是一个 INSERT

原来只有一个 `mark()`：进 `_dispatch` 就登记，之后才跑 handler。
于是 kill -9 打在 handler 执行中间时会发生这样一串：

    mark(id) → 崩 → 重启 → recover_stale() 把信从 cur/ 退回 new/
             → 重新处理 → mark(id) 返回 False → 只补一条回执就 return

**handler 永远不会重跑。** `recover_stale` 把信搬回去了，seen.db 又把它挡住 ——
真实语义是「最多一次」，而不是文档里写的「至少一次」。这正好否掉了文件邮箱
最核心的那个卖点，所以修法不能是绕过去，得让 seen.db 记住「开始了」和「干完了」
是两件不同的事：

- `claim()` 登记「我开始处理了」，并记下**是哪个进程实例**在处理；
- `complete()` 在**信归档之后**才落，表示这条彻底了结。

崩溃后重启，进程实例令牌变了，于是 `claim()` 看到「登记过、但没完成、而且不是我」
就返回 RETRY —— handler 重跑。而同一个进程里并发撞上同一个 id（真正的重复投递）
令牌相同，仍然判成 DUPLICATE，不会被处理两遍。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from anthill.core.ids import now

RETENTION = timedelta(days=7)

RUNTIME_TOKEN = f"{os.getpid()}:{uuid4().hex[:12]}"
"""本次进程实例的身份。

只用 pid 不够：机器重启后 pid 会被复用，那样「上一条命是我自己」会误判成
「正在并发处理」，于是崩溃后的重放又被吃掉。加一段随机量就没有这个歧义。
"""


class Claim(StrEnum):
    FIRST = "first"
    """头一次见 —— 该处理。"""

    RETRY = "retry"
    """上一个进程实例领了却没干完（多半是被 kill -9）—— 该重新处理。"""

    DUPLICATE = "duplicate"
    """已经处理完了，或者本进程正在处理 —— 不再处理，但仍要补发回执。"""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    id         TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_at ON seen(seen_at);
"""

_ADDED_COLUMNS = {
    "claimed_by": "TEXT",
    "done_at": "TEXT",
}
"""老库里没有这两列。开库时补，别让升级把已有的去重记录冲掉。"""


class SeenStore:
    """SQLite 单表。选它而不是内存 set：agentd 重启后去重仍然有效。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + 自带锁：watcher 线程与事件循环都可能访问
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(seen)")}
        for column, kind in _ADDED_COLUMNS.items():
            if column not in have:
                self._conn.execute(f"ALTER TABLE seen ADD COLUMN {column} {kind}")

    def claim(self, msg_id: str, expires_at: datetime | None = None) -> Claim:
        """登记「我要开始处理这条」。三种结果见 `Claim`。

        判据是 `(干完了没, 是谁领的)` 两个字段一起看 —— 少看一个就会退回
        「最多一次」或者「重复处理」其中之一。
        """
        stamp = now().isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seen(id, seen_at, expires_at, claimed_by, done_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (msg_id, stamp, expires_at.isoformat() if expires_at else None, RUNTIME_TOKEN),
            )
            if cur.rowcount == 1:
                return Claim.FIRST
            row = self._conn.execute(
                "SELECT done_at, claimed_by FROM seen WHERE id = ?", (msg_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - 只有被并发 purge 掉才会走到
                return Claim.FIRST
            done_at, claimed_by = row
            if done_at is not None or claimed_by == RUNTIME_TOKEN:
                return Claim.DUPLICATE
            # 领了没干完，而且领的人不是这个进程实例 —— 它已经不在了，接手重跑
            self._conn.execute(
                "UPDATE seen SET claimed_by = ?, seen_at = ? WHERE id = ?",
                (RUNTIME_TOKEN, stamp, msg_id),
            )
            return Claim.RETRY

    def complete(self, msg_id: str) -> None:
        """这条彻底了结。**必须在信归档之后调用。**

        顺序反过来会开一扇小窗：落了 completed 却还没归档时崩溃，
        `recover_stale` 把信退回 new/，而 seen.db 说「干完了」—— 那条消息就丢了处理。
        反过来（先归档后 complete）最坏只是白记一笔，信已经在 done/ 里，不会被重放。
        """
        with self._lock:
            self._conn.execute(
                "UPDATE seen SET done_at = ? WHERE id = ? AND done_at IS NULL",
                (now().isoformat(), msg_id),
            )

    def is_done(self, msg_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT done_at FROM seen WHERE id = ?", (msg_id,)).fetchone()
        return bool(row and row[0])

    def has(self, msg_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM seen WHERE id = ?", (msg_id,)).fetchone()
        return row is not None

    def purge(self, retention: timedelta = RETENTION) -> int:
        """滚动清理，防止 seen.db 无限增长。返回删除条数。

        **只清已经了结的。** 一条领了没干完的记录被清掉，就等于把「崩溃前处理到一半」
        这个事实抹了，重放时会被当成头一次 —— 那倒是对的；但要是它还在处理中
        （长任务跑了几天），清掉就等于放行一次重复处理。留着更安全。
        """
        cutoff = (now() - retention).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM seen WHERE seen_at < ? AND done_at IS NOT NULL", (cutoff,)
            )
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SeenStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
