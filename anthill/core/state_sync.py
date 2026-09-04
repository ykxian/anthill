"""无需唤醒模型的版本化状态同步。

状态更新仍走 Maildir，可靠投递、验签、回执和归档语义都不变；区别只在消费端：
runtime 在任何 handler / bridge / CLI 之前把快照交给本模块。这里以 revision
选取同一 publisher 的最新完整快照，并以 canonical JSON 的 SHA-256 校验内容。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import JsonValue

from anthill.core.atomic import atomic_write
from anthill.core.envelope import Address
from anthill.core.errors import ProtocolError
from anthill.core.payloads import STATE_KEY_RE, StateUpdatePayload

STATE_FILE_VERSION = 1


class StateApplyStatus(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE = "stale"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class StateRecord:
    key: str
    source: str
    revision: int
    digest: str
    summary: str
    snapshot: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class StateApplyResult:
    status: StateApplyStatus
    key: str
    source: str
    incoming_revision: int
    current_revision: int | None
    digest: str
    path: Path
    reason: str = ""
    skipped_revisions: int = 0

    @property
    def applied(self) -> bool:
        return self.status is StateApplyStatus.APPLIED


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
    """每个 Agent 的本地状态副本；一个 key 只保存已应用的最新完整快照。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, key: str) -> Path:
        # 线协议已经校验；公开读 API 仍重复同一防线，不能靠调用者“记得先校验”。
        if len(key) > 128 or not STATE_KEY_RE.fullmatch(key):
            raise ValueError(f"非法 state key {key!r}")
        return self._root / f"{key}.json"

    def load(self, key: str) -> StateRecord | None:
        path = self.path_for(key)
        if path.is_symlink():
            raise ProtocolError(f"本地 state {key!r} 是符号链接，拒绝读取")
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"本地 state {key!r} 无法读取：{exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != STATE_FILE_VERSION:
            raise ProtocolError(f"本地 state {key!r} 的版本或结构非法")
        try:
            source = _canonical_source(raw["source"])
            payload = StateUpdatePayload.model_validate(
                {
                    "key": raw["key"],
                    "revision": raw["revision"],
                    "digest": raw["digest"],
                    "summary": raw["summary"],
                    "snapshot": raw["snapshot"],
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError(f"本地 state {key!r} 的 schema 非法：{exc}") from exc
        if payload.key != key:
            raise ProtocolError(
                f"本地 state 文件 {key!r} 内声明的 key 是 {payload.key!r}，拒绝使用"
            )
        calculated = snapshot_digest(payload.snapshot)
        if calculated != payload.digest:
            raise ProtocolError(
                f"本地 state {key!r} digest 不匹配：声明 {payload.digest}，实际 {calculated}"
            )
        return StateRecord(
            key=payload.key,
            source=source,
            revision=payload.revision,
            digest=payload.digest,
            summary=payload.summary,
            snapshot=payload.snapshot,
        )

    def apply(self, update: StateUpdatePayload, *, source: str) -> StateApplyResult:
        """校验并选取更新快照；所有非 APPLIED 结果都不会修改磁盘。"""
        source = _canonical_source(source)
        path = self.path_for(update.key)
        try:
            actual = snapshot_digest(update.snapshot)
        except ProtocolError as exc:
            return StateApplyResult(
                status=StateApplyStatus.CONFLICT,
                key=update.key,
                source=source,
                incoming_revision=update.revision,
                current_revision=None,
                digest=update.digest,
                path=path,
                reason=f"snapshot invalid: {exc}",
            )
        if actual != update.digest:
            return StateApplyResult(
                status=StateApplyStatus.CONFLICT,
                key=update.key,
                source=source,
                incoming_revision=update.revision,
                current_revision=None,
                digest=update.digest,
                path=path,
                reason=f"digest mismatch: declared={update.digest} actual={actual}",
            )

        try:
            current = self.load(update.key)
        except ProtocolError as exc:
            return StateApplyResult(
                status=StateApplyStatus.CONFLICT,
                key=update.key,
                source=source,
                incoming_revision=update.revision,
                current_revision=None,
                digest=update.digest,
                path=path,
                reason=f"local state invalid: {exc}",
            )

        if current is not None:
            if source != current.source:
                return self._result(
                    StateApplyStatus.CONFLICT,
                    update,
                    current,
                    source,
                    f"publisher authority mismatch: owner={current.source} got={source}",
                )
            if update.revision < current.revision:
                return self._result(
                    StateApplyStatus.STALE, update, current, source, "旧 revision 已抑制"
                )
            if update.revision == current.revision:
                if update.digest == current.digest:
                    return self._result(
                        StateApplyStatus.DUPLICATE,
                        update,
                        current,
                        source,
                        "相同 publisher、revision 和 digest",
                    )
                return self._result(
                    StateApplyStatus.CONFLICT,
                    update,
                    current,
                    source,
                    "同 revision 出现不同 digest",
                )

        # state.update 携带完整快照，不是事件流。新副本可以从任意 revision
        # 建立，离线副本也可用一个更高 revision 直接恢复，无需补齐中间版本。
        skipped = max(0, update.revision - current.revision - 1) if current else 0

        data = json.dumps(
            {
                "version": STATE_FILE_VERSION,
                "key": update.key,
                "source": source,
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
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write(self._root, self._root, path.name, data)
        return StateApplyResult(
            status=StateApplyStatus.APPLIED,
            key=update.key,
            source=source,
            incoming_revision=update.revision,
            current_revision=current.revision if current else None,
            digest=update.digest,
            path=path,
            reason=(f"完整快照跳过 {skipped} 个中间 revision" if skipped else ""),
            skipped_revisions=skipped,
        )

    def list(self) -> tuple[StateRecord, ...]:
        records: list[StateRecord] = []
        for path in sorted(self._root.glob("*.json")):
            record = self.load(path.stem)
            if record is not None:
                records.append(record)
        return tuple(records)

    def _result(
        self,
        status: StateApplyStatus,
        update: StateUpdatePayload,
        current: StateRecord,
        source: str,
        reason: str,
    ) -> StateApplyResult:
        return StateApplyResult(
            status=status,
            key=update.key,
            source=source,
            incoming_revision=update.revision,
            current_revision=current.revision,
            digest=update.digest,
            path=self.path_for(update.key),
            reason=reason,
        )


def _canonical_source(value: object) -> str:
    """publisher authority 的唯一持久化表示；role/all 不能成为状态 owner。"""
    if not isinstance(value, str):
        raise ValueError("state source 必须是 node:agent 字符串")
    node, separator, agent = value.partition(":")
    if not separator:
        raise ValueError("state source 必须使用 node:agent 格式")
    address = Address(node=node, agent=agent)
    if address.is_role or address.is_broadcast:
        raise ValueError("state source 必须是具体 Agent，不能是 role 或 all")
    canonical = str(address)
    if value != canonical:
        raise ValueError(f"state source 不是规范形式：{value!r}")
    return canonical
