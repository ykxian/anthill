"""已见消息集（seen.db）—— 幂等去重（02-protocol §4）。

传输层只保证「至少一次投递」，接收方按消息 ULID 去重，
两者合成 **恰好一次处理** 的语义。重复消息虽然丢弃，但仍要补发回执，
否则发送方的状态机永远收敛不了。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType

from anthill.core.ids import now

RETENTION = timedelta(days=7)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    id         TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_at ON seen(seen_at);
"""


class SeenStore:
    """SQLite 单表。选它而不是内存 set：agentd 重启后去重仍然有效。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + 自带锁：watcher 线程与事件循环都可能访问
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def mark(self, msg_id: str, expires_at: datetime | None = None) -> bool:
        """登记一条消息。返回 True 表示首见（应当处理），False 表示重复（应当丢弃）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seen(id, seen_at, expires_at) VALUES (?, ?, ?)",
                (msg_id, now().isoformat(), expires_at.isoformat() if expires_at else None),
            )
            return cur.rowcount == 1

    def has(self, msg_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM seen WHERE id = ?", (msg_id,)).fetchone()
        return row is not None

    def purge(self, retention: timedelta = RETENTION) -> int:
        """滚动清理，防止 seen.db 无限增长。返回删除条数。"""
        cutoff = (now() - retention).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM seen WHERE seen_at < ?", (cutoff,))
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
