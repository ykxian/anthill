"""节点内共享、零消息投递的版本化状态公告。

状态公告是 workspace 文件，不是 Anthill 消息：发布者直接原子更新唯一文档，
Agent 与 Panel 仅在需要时读取。这里不依赖 Envelope、Mailbox、Router、回执、
bridge 或模型 runtime。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from anthill.core.atomic import atomic_write
from anthill.core.errors import ProtocolError
from anthill.core.process_lock import ProcessLock

STATE_FILE_VERSION = 2
STATE_SCOPE = "node-local"
MAX_STATE_FILE_BYTES = 128 * 1024
STATE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NODE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_STATE_FILE_KEYS = {
    "version",
    "scope",
    "key",
    "publisher",
    "revision",
    "digest",
    "summary",
    "snapshot",
}


class StateDocumentUpdate(BaseModel):
    """一次完整状态文档发布；它从不成为 Envelope payload。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    digest: str
    summary: str = Field(min_length=1, max_length=500)
    snapshot: dict[str, JsonValue]

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        if not STATE_KEY_RE.fullmatch(value):
            raise ValueError("state key 只允许小写点分段：字母开头，后接小写字母、数字、_ 或 -")
        return value

    @field_validator("digest")
    @classmethod
    def _check_digest(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("digest 必须是 64 位小写 SHA-256 十六进制")
        return value

    @classmethod
    def from_snapshot(
        cls,
        *,
        key: str,
        revision: int,
        summary: str,
        snapshot: dict[str, JsonValue],
    ) -> Self:
        return cls(
            key=key,
            revision=revision,
            digest=snapshot_digest(snapshot),
            summary=summary,
            snapshot=snapshot,
        )


class StatePublishStatus(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE = "stale"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class StateRecord:
    key: str
    publisher: str
    revision: int
    digest: str
    summary: str
    snapshot: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class StatePublishResult:
    status: StatePublishStatus
    key: str
    publisher: str
    incoming_revision: int
    current_revision: int | None
    digest: str
    path: Path
    reason: str = ""
    skipped_revisions: int = 0

    @property
    def applied(self) -> bool:
        return self.status is StatePublishStatus.APPLIED

    @property
    def successful(self) -> bool:
        return self.status in {StatePublishStatus.APPLIED, StatePublishStatus.DUPLICATE}


def canonical_snapshot_bytes(snapshot: dict[str, JsonValue]) -> bytes:
    """跨 Python 进程稳定的 JSON 表示；NaN/Infinity 明确拒绝。"""
    try:
        return json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"state snapshot 不是合法 JSON：{exc}") from exc


def snapshot_digest(snapshot: dict[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_snapshot_bytes(snapshot)).hexdigest()


class StateStore:
    """一个节点内的共享状态文档库；每个 key 只保留当前完整快照。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def lock_path(self) -> Path:
        return self._root / ".publish.lock"

    def path_for(self, key: str) -> Path:
        if len(key) > 128 or not STATE_KEY_RE.fullmatch(key):
            raise ValueError(f"非法 state key {key!r}")
        return self._root / f"{key}.json"

    def load(self, key: str) -> StateRecord | None:
        self._check_root_for_read()
        path = self.path_for(key)
        if path.is_symlink():
            raise ProtocolError(f"共享 state {key!r} 是符号链接，拒绝读取")
        if not path.is_file():
            return None
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ProtocolError(f"共享 state {key!r} 无法读取：{exc}") from exc
        return decode_state_document(raw, expected_key=key)

    def publish(self, update: StateDocumentUpdate, *, publisher: str) -> StatePublishResult:
        """在全店锁内校验、比较并原子发布；锁忙立即失败，不轮询。"""
        self._ensure_root()
        with ProcessLock(self.lock_path, label="共享状态 publisher"):
            # 调用边界已经是强类型，锁内仍重做 exact schema 校验，保证整个
            # validate/read/decide/write 临界区不依赖调用者的构造方式。
            checked = StateDocumentUpdate.model_validate(update.model_dump(mode="python"))
            return self._publish_locked(checked, publisher=canonical_publisher(publisher))

    def _publish_locked(self, update: StateDocumentUpdate, *, publisher: str) -> StatePublishResult:
        path = self.path_for(update.key)
        actual = snapshot_digest(update.snapshot)
        if actual != update.digest:
            return StatePublishResult(
                status=StatePublishStatus.CONFLICT,
                key=update.key,
                publisher=publisher,
                incoming_revision=update.revision,
                current_revision=None,
                digest=update.digest,
                path=path,
                reason=f"digest mismatch: declared={update.digest} actual={actual}",
            )

        try:
            current = self.load(update.key)
        except ProtocolError as exc:
            return StatePublishResult(
                status=StatePublishStatus.CONFLICT,
                key=update.key,
                publisher=publisher,
                incoming_revision=update.revision,
                current_revision=None,
                digest=update.digest,
                path=path,
                reason=f"local state invalid: {exc}",
            )

        if current is not None:
            if publisher != current.publisher:
                return self._result(
                    StatePublishStatus.CONFLICT,
                    update,
                    current,
                    publisher,
                    f"publisher authority mismatch: owner={current.publisher} got={publisher}",
                )
            if update.revision < current.revision:
                return self._result(
                    StatePublishStatus.STALE, update, current, publisher, "旧 revision 已抑制"
                )
            if update.revision == current.revision:
                if update.digest == current.digest and update.summary == current.summary:
                    return self._result(
                        StatePublishStatus.DUPLICATE,
                        update,
                        current,
                        publisher,
                        "相同 publisher、revision、digest 和 summary",
                    )
                return self._result(
                    StatePublishStatus.CONFLICT,
                    update,
                    current,
                    publisher,
                    "同 revision 出现不同内容",
                )

        skipped = max(0, update.revision - current.revision - 1) if current else 0
        data = json.dumps(
            {
                "version": STATE_FILE_VERSION,
                "scope": STATE_SCOPE,
                "key": update.key,
                "publisher": publisher,
                "revision": update.revision,
                "digest": update.digest,
                "summary": update.summary,
                "snapshot": update.snapshot,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        if len(data) > MAX_STATE_FILE_BYTES:
            raise ProtocolError(
                f"state 文档 {len(data)} 字节，超过 {MAX_STATE_FILE_BYTES} 字节上限"
            )
        atomic_write(self._root, self._root, path.name, data)
        return StatePublishResult(
            status=StatePublishStatus.APPLIED,
            key=update.key,
            publisher=publisher,
            incoming_revision=update.revision,
            current_revision=current.revision if current else None,
            digest=update.digest,
            path=path,
            reason=(f"完整快照跳过 {skipped} 个中间 revision" if skipped else ""),
            skipped_revisions=skipped,
        )

    def list(self) -> tuple[StateRecord, ...]:
        self._check_root_for_read()
        if not self._root.exists():
            return ()
        records: list[StateRecord] = []
        for path in sorted(self._root.glob("*.json")):
            record = self.load(path.stem)
            if record is not None:
                records.append(record)
        return tuple(records)

    def _ensure_root(self) -> None:
        if self._root.is_symlink():
            raise ProtocolError("共享 state 目录不能是符号链接")
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir() or self._root.is_symlink():
            raise ProtocolError("共享 state 路径不是安全目录")

    def _check_root_for_read(self) -> None:
        if self._root.is_symlink():
            raise ProtocolError("共享 state 目录不能是符号链接")
        if self._root.exists() and not self._root.is_dir():
            raise ProtocolError("共享 state 路径不是目录")

    def _result(
        self,
        status: StatePublishStatus,
        update: StateDocumentUpdate,
        current: StateRecord,
        publisher: str,
        reason: str,
    ) -> StatePublishResult:
        return StatePublishResult(
            status=status,
            key=update.key,
            publisher=publisher,
            incoming_revision=update.revision,
            current_revision=current.revision,
            digest=update.digest,
            path=self.path_for(update.key),
            reason=reason,
        )


def canonical_publisher(value: object) -> str:
    """publisher 的唯一持久化表示；role/all 不能成为状态 owner。"""
    if not isinstance(value, str):
        raise ValueError("state publisher 必须是 node:agent 字符串")
    node, separator, agent = value.partition(":")
    if not separator:
        raise ValueError("state publisher 必须使用 node:agent 格式")
    if not _NODE_NAME_RE.fullmatch(node) or not _AGENT_NAME_RE.fullmatch(agent):
        raise ValueError("state publisher 必须是规范的具体 node:agent")
    if agent == "all":
        raise ValueError("state publisher 必须是具体 Agent，不能是 role 或 all")
    return value


def decode_state_document(raw_bytes: bytes, *, expected_key: str) -> StateRecord:
    """共享给 CLI 与 Panel 的唯一磁盘 schema/digest 校验入口。"""
    if len(raw_bytes) > MAX_STATE_FILE_BYTES:
        raise ProtocolError(f"共享 state {expected_key!r} 超过字节上限")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"共享 state {expected_key!r} 不是合法 UTF-8 JSON：{exc}") from exc
    if not isinstance(raw, dict) or set(raw) != _STATE_FILE_KEYS:
        raise ProtocolError(f"共享 state {expected_key!r} 的 schema 非法")
    if raw.get("version") != STATE_FILE_VERSION or raw.get("scope") != STATE_SCOPE:
        raise ProtocolError(f"共享 state {expected_key!r} 的版本或 scope 非法")
    try:
        publisher = canonical_publisher(raw["publisher"])
        update = StateDocumentUpdate.model_validate(
            {
                "key": raw["key"],
                "revision": raw["revision"],
                "digest": raw["digest"],
                "summary": raw["summary"],
                "snapshot": raw["snapshot"],
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"共享 state {expected_key!r} 的 schema 非法：{exc}") from exc
    if update.key != expected_key:
        raise ProtocolError(
            f"共享 state 文件 {expected_key!r} 内声明的 key 是 {update.key!r}，拒绝使用"
        )
    calculated = snapshot_digest(update.snapshot)
    if calculated != update.digest:
        raise ProtocolError(
            f"共享 state {expected_key!r} digest 不匹配：声明 {update.digest}，实际 {calculated}"
        )
    return StateRecord(
        key=update.key,
        publisher=publisher,
        revision=update.revision,
        digest=update.digest,
        summary=update.summary,
        snapshot=update.snapshot,
    )
