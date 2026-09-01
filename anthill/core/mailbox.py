"""文件邮箱（Maildir 变体）—— Agent 感知世界的唯一入口。

    mailbox/
    ├── inbox/{tmp,new,cur,done}
    ├── outbox/{pending,sent}
    ├── delivery-locks/
    └── seen.db

watcher 只盯 `inbox/new`；写入方一律 tmp→rename。目录语义见 02-protocol §2。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from anthill.core.atomic import PART_SUFFIX, atomic_move, atomic_write, ensure_same_filesystem
from anthill.core.envelope import Envelope
from anthill.core.errors import MailboxError, ProtocolError
from anthill.core.ids import now
from anthill.core.seen import SeenStore

DIR_MODE = 0o700
"""同机场景的安全边界就是文件系统权限（02-protocol §6）。"""

TMP_MAX_AGE = timedelta(hours=1)


class Mailbox:
    """一个 Agent 的收发件目录。纯 IO，不含任何 LLM 逻辑。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    # ---------- 目录 ----------

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def tmp(self) -> Path:
        return self.inbox / "tmp"

    @property
    def new(self) -> Path:
        return self.inbox / "new"

    @property
    def cur(self) -> Path:
        return self.inbox / "cur"

    @property
    def done(self) -> Path:
        return self.inbox / "done"

    @property
    def outbox(self) -> Path:
        return self.root / "outbox"

    @property
    def pending(self) -> Path:
        return self.outbox / "pending"

    @property
    def sent(self) -> Path:
        return self.outbox / "sent"

    @property
    def dead(self) -> Path:
        """重试耗尽的死信。留着而不是删掉：既要上报 coordinator，也要人能查。"""
        return self.outbox / "dead"

    @property
    def delivery_locks(self) -> Path:
        """同一发件邮箱被 CLI 与 agentd 共用时，按消息 ID 做跨进程投递互斥。"""
        return self.root / "delivery-locks"

    @property
    def seen_db(self) -> Path:
        return self.root / "seen.db"

    def all_dirs(self) -> tuple[Path, ...]:
        return (
            self.tmp,
            self.new,
            self.cur,
            self.done,
            self.pending,
            self.sent,
            self.dead,
            self.delivery_locks,
        )

    def ensure(self) -> Mailbox:
        """幂等创建全部目录。返回 self 便于链式调用。"""
        for directory in self.all_dirs():
            directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        ensure_same_filesystem(self.tmp, self.new)
        return self

    @property
    def exists(self) -> bool:
        return self.new.is_dir()

    def open_seen(self) -> SeenStore:
        return SeenStore(self.seen_db)

    # ---------- 投递（写入方视角）----------

    def deposit(self, env: Envelope) -> Path:
        """把信封原子地放进 inbox/new。所有传输实现最终都调这里。"""
        if not self.exists:
            raise MailboxError(f"邮箱不存在：{self.root}（对方 agentd 没起过？）")
        return atomic_write(self.tmp, self.new, f"{env.id}.json", env.to_json_bytes())

    # ---------- 消费（接收方视角）----------

    def list_new(self) -> list[Path]:
        """ULID 文件名字典序 == 时间序，所以 sorted 即先进先出。"""
        if not self.new.is_dir():
            return []
        return sorted(p for p in self.new.iterdir() if p.suffix == ".json")

    def claim(self, path: Path) -> Path:
        """new → cur，表示「我开始处理这条了」。"""
        return atomic_move(path, self.cur / path.name)

    def recover_stale(self) -> list[Path]:
        """启动时把上次崩溃遗留在 cur 里的消息退回 new 重新处理。

        代价是可能重复处理一次 —— 这正是想要的：**「至少一次」就靠这一步**。

        （这里曾经写着「seen.db 幂等正好兜住」，而那时的 seen.db 是一进 `_dispatch`
        就 `mark()`，于是重放回来的消息一律被判成重复、handler 永远不会重跑 ——
        退信这一步成了安慰剂，真实语义是「最多一次」。现在 seen.db 分
        claimed/completed 两阶段，只有真正处理完的才会挡住重放。见 core/seen.py。）
        """
        if not self.cur.is_dir():
            return []
        return [atomic_move(p, self.new / p.name) for p in sorted(self.cur.iterdir())]

    def archive(self, path: Path) -> Path:
        """处理完归档到 done/<日期>/，便于事后重放与审计。"""
        day_dir = self.done / now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        return atomic_move(path, day_dir / path.name)

    def quarantine(self, path: Path, reason: str) -> Path:
        """无法解析的文件单独隔离，不能让它堵住队列，也不能悄悄删掉。"""
        bad_dir = self.done / "invalid"
        bad_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        (bad_dir / f"{path.name}.reason.txt").write_text(reason, encoding="utf-8")
        return atomic_move(path, bad_dir / path.name)

    @staticmethod
    def read_envelope(path: Path) -> Envelope:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise MailboxError(f"读取信封 {path} 失败：{exc}") from exc
        if not data:
            raise ProtocolError(f"信封 {path} 是空文件")
        return Envelope.from_json_bytes(data)

    # ---------- 维护 ----------

    def sweep_tmp(self, max_age: timedelta = TMP_MAX_AGE) -> int:
        """清理写了一半就崩溃的残留 .part 文件。返回清理数量。"""
        if not self.tmp.is_dir():
            return 0
        cutoff = (now() - max_age).timestamp()
        removed = 0
        for path in self.tmp.iterdir():
            if not path.name.endswith(PART_SUFFIX):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue  # 别人正在写或已被清走，都不是错误
        return removed
