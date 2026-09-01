"""发件箱与重试（02-protocol §3）。

pending 里每条消息有两个文件：`<id>.json`（信封本体）与 `<id>.meta.json`（重试计数）。
持久化的意义：agentd 崩溃重启后，没送出去的消息还在，继续重试。

重试必然带来重复投递 —— 接收方的 seen.db 幂等去重正好兜住，
合起来就是「至少一次投递 + 恰好一次处理」。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from anthill.core.atomic import atomic_write
from anthill.core.envelope import Envelope
from anthill.core.errors import AntHillError, MailboxError
from anthill.core.ids import now
from anthill.core.mailbox import Mailbox
from anthill.core.process_lock import ProcessLock

MAX_ATTEMPTS = 5
BACKOFF_BASE = timedelta(seconds=1)
"""指数退避 1s → 2s → 4s → 8s → 16s，第 5 次仍失败进 dead letter。"""

META_SUFFIX = ".meta.json"
DELIVERY_LOCK_BUCKETS = 256
"""稳定锁桶数量。文件永久保留以保证 inode 不变，但总数严格有界。"""


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """一条死信 + 它为什么死。"""

    msg_id: str
    path: Path
    error: str

    @property
    def to(self) -> str:
        return self._field("to")

    @property
    def reason(self) -> str:
        return self._field("last_error") or "（没记下原因）"

    @property
    def attempts(self) -> str:
        return self._field("attempts")

    def _field(self, key: str) -> str:
        for line in self.error.splitlines():
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip()
        return ""


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
        sent = self._mailbox.sent / f"{env.id}.json"
        if sent.is_file():
            return OutboxEntry(envelope=self._read_matching(sent, env))
        dead = self._mailbox.dead / f"{env.id}.json"
        if dead.is_file():
            raise MailboxError(
                f"消息 {env.id} 已在死信箱；必须显式 requeue，不能由失败动作自动复活"
            )
        pending = self.pending_dir / f"{env.id}.json"
        if pending.is_file():
            existing = self._read_matching(pending, env)
            return self._with_meta(existing)
        entry = OutboxEntry(envelope=env)
        self._write_new(entry)
        return entry

    def sent_envelope(self, env: Envelope) -> Envelope | None:
        """同 ID 已投递则返回落盘版本；ID 碰撞则拒绝，绝不拿旧状态冒充新消息。"""
        path = self._mailbox.sent / f"{env.id}.json"
        if not path.is_file():
            return None
        return self._read_matching(path, env)

    def sent_path(self, msg_id: str) -> Path:
        return self._mailbox.sent / f"{msg_id}.json"

    def pending_entry(self, env: Envelope) -> OutboxEntry | None:
        """返回同一信封当前的 pending 状态；不存在时返回空，ID 碰撞仍拒绝。"""
        path = self.pending_dir / f"{env.id}.json"
        if not path.is_file():
            return None
        return self._with_meta(self._read_matching(path, env))

    def delivery_lock(self, msg_id: str) -> ProcessLock:
        """CLI 首投与 agentd retry 争稳定的有限锁桶，进程内 set 不够。

        lockfile 运行期不能 unlink，否则旧 inode 的持锁者与新 inode 的持锁者会
        同时进入临界区。按 ID 稳定散列到固定桶，既保住 inode，又不无界吃 inode。
        """
        digest = hashlib.blake2s(msg_id.encode(), digest_size=2).digest()
        bucket = int.from_bytes(digest, "big") % DELIVERY_LOCK_BUCKETS
        return ProcessLock(
            self._mailbox.delivery_locks / f"bucket-{bucket:03d}.lock",
            label=f"消息 {msg_id} 的投递",
        )

    @staticmethod
    def _read_matching(path: Path, expected: Envelope) -> Envelope:
        try:
            existing = Mailbox.read_envelope(path)
        except AntHillError as exc:
            raise MailboxError(f"已有发件状态 {path} 无法读取：{exc}") from exc
        if existing != expected:
            raise MailboxError(f"消息 ID {expected.id} 已被另一份不同信封占用")
        return existing

    def _write_new(self, entry: OutboxEntry) -> None:
        """首次入队：Envelope 是业务真相，必须是第一份持久化数据。

        attempts=0 由“没有 meta”表达。先清可能来自极旧崩溃/保留期后的孤儿
        meta，再原子写 Envelope；一旦 Envelope 出现，恢复扫描就一定看得见它。
        """
        self._drop_meta(entry)
        name = f"{entry.msg_id}.json"
        atomic_write(self._mailbox.tmp, self.pending_dir, name, entry.envelope.to_json_bytes())

    def _write_meta(self, entry: OutboxEntry) -> None:
        """已有 pending 的失败更新只改 meta；Envelope 本体不必也不应重写。"""
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
        src = self.pending_dir / f"{entry.msg_id}.json"
        dst = self._mailbox.sent / f"{entry.msg_id}.json"
        if src.is_file():
            src.replace(dst)
        # 先移动业务真相，再删附属 meta；反过来崩溃会让 pending 重置次数。
        self._drop_meta(entry)
        return dst

    def mark_failed(self, entry: OutboxEntry, error: str) -> OutboxEntry:
        """记一次失败。返回的 entry 若 is_dead 为真，调用方应走 dead letter 上报。"""
        updated = entry.failed(error)
        if updated.is_dead:
            self._to_dead(updated)
        else:
            self._write_meta(updated)
        return updated

    def abandon(self, entry: OutboxEntry, error: str) -> OutboxEntry:
        """不可重试的失败：直接进死信，不等退避。

        必须有这个方法 —— 否则「收件人不存在」这类错误会把条目永远留在 pending，
        重试循环每秒捡起来一次、每秒报一次死信，日志和 coordinator 邮箱都会被刷爆。
        """
        updated = entry.failed(error)
        self._to_dead(updated)
        return updated

    def _to_dead(self, entry: OutboxEntry) -> Path:
        dead_dir = self._mailbox.dead
        dead_dir.mkdir(parents=True, exist_ok=True)
        src = self.pending_dir / f"{entry.msg_id}.json"
        dst = dead_dir / f"{entry.msg_id}.json"
        # 原因先原子落盘，再移动业务信封。中途崩溃最多留下一个不会被列出的
        # 孤儿 sidecar；只要 dead 信封可见，它的诊断原因就已经存在。
        atomic_write(
            self._mailbox.tmp,
            dead_dir,
            f"{entry.msg_id}.error.txt",
            (
                f"to={entry.envelope.to}\n"
                f"type={entry.envelope.type}\n"
                f"attempts={entry.attempts}\n"
                f"last_error={entry.last_error}\n"
            ).encode(),
        )
        if src.is_file():
            src.replace(dst)
        self._drop_meta(entry)
        return dst

    def _drop_meta(self, entry: OutboxEntry) -> None:
        (self.pending_dir / f"{entry.msg_id}{META_SUFFIX}").unlink(missing_ok=True)

    def dead_letters(self) -> list[Path]:
        dead_dir = self._mailbox.dead
        if not dead_dir.is_dir():
            return []
        return sorted(p for p in dead_dir.iterdir() if p.suffix == ".json")

    def dead_letter(self, msg_id: str) -> DeadLetter | None:
        """连同「为什么死的」一起读出来。看不懂原因的死信等于没有死信。"""
        path = self._mailbox.dead / f"{msg_id}.json"
        if not path.is_file():
            return None
        reason = self._mailbox.dead / f"{msg_id}.error.txt"
        return DeadLetter(
            msg_id=msg_id,
            path=path,
            error=reason.read_text(encoding="utf-8") if reason.is_file() else "",
        )

    def dead_letter_list(self) -> list[DeadLetter]:
        return [
            letter
            for path in self.dead_letters()
            if (letter := self.dead_letter(path.stem)) is not None
        ]

    def requeue_dead(self, msg_id: str) -> OutboxEntry:
        """把一条死信放回 pending 重新投。

        没有这条路的话，死信就是个只能看计数的黑洞 —— 而进死信的最常见原因
        （对端 agentd 晚起了一会儿）恰恰是**修好之后就该重投**的那种。
        以前唯一的恢复手段是手动 `mv` 文件。

        重投 = 重新计数：`attempts` 归零，不然刚放回去就又立刻判死。
        """
        path = self._mailbox.dead / f"{msg_id}.json"
        if not path.is_file():
            raise MailboxError(f"没有这条死信：{msg_id}")
        env = Envelope.model_validate_json(path.read_text(encoding="utf-8"))
        entry = OutboxEntry(envelope=env)
        self._write_new(entry)
        path.unlink(missing_ok=True)
        (self._mailbox.dead / f"{msg_id}.error.txt").unlink(missing_ok=True)
        return entry

    def drop_dead(self, msg_id: str) -> bool:
        """确认不要了，删掉。返回是否真的删了一条。"""
        path = self._mailbox.dead / f"{msg_id}.json"
        if not path.is_file():
            return False
        path.unlink()
        (self._mailbox.dead / f"{msg_id}.error.txt").unlink(missing_ok=True)
        return True
