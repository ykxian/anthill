"""发件箱与重试（02-protocol §3）。

pending 里每条消息有两个文件：`<id>.json`（信封本体）与 `<id>.meta.json`（重试计数）。
持久化的意义：agentd 崩溃重启后，没送出去的消息还在，继续重试。

重试必然带来重复投递 —— 接收方的 seen.db 幂等去重正好兜住，
合起来就是「至少一次投递 + 恰好一次处理」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from anthill.core.atomic import atomic_write
from anthill.core.envelope import Envelope
from anthill.core.errors import AntHillError
from anthill.core.ids import now
from anthill.core.mailbox import Mailbox

MAX_ATTEMPTS = 5
BACKOFF_BASE = timedelta(seconds=1)
"""指数退避 1s → 2s → 4s → 8s → 16s，第 5 次仍失败进 dead letter。"""

META_SUFFIX = ".meta.json"


def backoff_delay(attempts: int) -> timedelta:
    return BACKOFF_BASE * (1 << max(0, attempts - 1))


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    envelope: Envelope
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None

    @property
    def msg_id(self) -> str:
        return self.envelope.id

    @property
    def is_dead(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS

    def is_due(self, at: datetime | None = None) -> bool:
        if self.next_attempt_at is None:
            return True
        return (at or now()) >= self.next_attempt_at

    def failed(self, error: str, at: datetime | None = None) -> OutboxEntry:
        attempts = self.attempts + 1
        return replace(
            self,
            attempts=attempts,
            last_error=error,
            next_attempt_at=(at or now()) + backoff_delay(attempts),
        )


class Outbox:
    """pending / sent / dead 三个目录的读写。"""

    def __init__(self, mailbox: Mailbox) -> None:
        self._mailbox = mailbox

    @property
    def pending_dir(self) -> Path:
        return self._mailbox.pending

    def enqueue(self, env: Envelope) -> OutboxEntry:
        entry = OutboxEntry(envelope=env)
        self._write(entry)
        return entry

    def _write(self, entry: OutboxEntry) -> None:
        name = f"{entry.msg_id}.json"
        atomic_write(self._mailbox.tmp, self.pending_dir, name, entry.envelope.to_json_bytes())
        meta = {
            "attempts": entry.attempts,
            "next_attempt_at": entry.next_attempt_at.isoformat() if entry.next_attempt_at else None,
            "last_error": entry.last_error,
            "to": str(entry.envelope.to),
        }
        atomic_write(
            self._mailbox.tmp,
            self.pending_dir,
            f"{entry.msg_id}{META_SUFFIX}",
            json.dumps(meta, ensure_ascii=False, indent=2).encode(),
        )

    def load_pending(self) -> list[OutboxEntry]:
        if not self.pending_dir.is_dir():
            return []
        entries: list[OutboxEntry] = []
        for path in sorted(self.pending_dir.iterdir()):
            if path.name.endswith(META_SUFFIX) or path.suffix != ".json":
                continue
            try:
                envelope = Mailbox.read_envelope(path)
            except (AntHillError, ValueError) as exc:
                # 不静默跳过：改名隔离，人能看见，重试循环也不会被它卡住
                self._quarantine(path, str(exc))
                continue
            entries.append(self._with_meta(envelope))
        return entries

    @staticmethod
    def _quarantine(path: Path, reason: str) -> None:
        path.replace(path.with_suffix(".json.corrupt"))
        path.with_suffix(".json.corrupt.reason.txt").write_text(reason, encoding="utf-8")

    def _with_meta(self, envelope: Envelope) -> OutboxEntry:
        meta_path = self.pending_dir / f"{envelope.id}{META_SUFFIX}"
        if not meta_path.is_file():
            return OutboxEntry(envelope=envelope)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return OutboxEntry(envelope=envelope)
        raw_next = meta.get("next_attempt_at")
        return OutboxEntry(
            envelope=envelope,
            attempts=int(meta.get("attempts", 0)),
            next_attempt_at=datetime.fromisoformat(raw_next) if raw_next else None,
            last_error=meta.get("last_error"),
        )

    def due(self, at: datetime | None = None) -> list[OutboxEntry]:
        moment = at or now()
        return [e for e in self.load_pending() if e.is_due(moment)]

    def mark_sent(self, entry: OutboxEntry) -> Path:
        self._drop_meta(entry)
        src = self.pending_dir / f"{entry.msg_id}.json"
        dst = self._mailbox.sent / f"{entry.msg_id}.json"
        if src.is_file():
            src.replace(dst)
        return dst

    def mark_failed(self, entry: OutboxEntry, error: str) -> OutboxEntry:
        """记一次失败。返回的 entry 若 is_dead 为真，调用方应走 dead letter 上报。"""
        updated = entry.failed(error)
        if updated.is_dead:
            self._to_dead(updated)
        else:
            self._write(updated)
        return updated

    def _to_dead(self, entry: OutboxEntry) -> Path:
        dead_dir = self._mailbox.dead
        dead_dir.mkdir(parents=True, exist_ok=True)
        self._drop_meta(entry)
        src = self.pending_dir / f"{entry.msg_id}.json"
        dst = dead_dir / f"{entry.msg_id}.json"
        if src.is_file():
            src.replace(dst)
        (dead_dir / f"{entry.msg_id}.error.txt").write_text(
            f"attempts={entry.attempts}\nlast_error={entry.last_error}\n", encoding="utf-8"
        )
        return dst

    def _drop_meta(self, entry: OutboxEntry) -> None:
        (self.pending_dir / f"{entry.msg_id}{META_SUFFIX}").unlink(missing_ok=True)

    def dead_letters(self) -> list[Path]:
        dead_dir = self._mailbox.dead
        if not dead_dir.is_dir():
            return []
        return sorted(p for p in dead_dir.iterdir() if p.suffix == ".json")
