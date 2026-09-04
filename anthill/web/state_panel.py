"""状态公告板的有界、只读数据层。

列表只给摘要元数据；完整 ``snapshot`` 必须按 Agent 和 key 单独读取。这里不使用
``StateStore.list()``：面板面对的是可能被手工损坏的磁盘内容，必须逐文件隔离失败，
并在解析 JSON 前限制文件类型、符号链接和字节数。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import JsonValue, ValidationError

from anthill.core.config import Config
from anthill.core.envelope import Address
from anthill.core.paths import NodeLayout
from anthill.core.payloads import STATE_KEY_RE, StateUpdatePayload
from anthill.core.state_sync import STATE_FILE_VERSION, snapshot_digest

MAX_STATE_FILE_BYTES = 128 * 1024
MAX_STATE_FILES_PER_AGENT = 64
MAX_STATE_RECORDS = 512
MAX_STATE_DIRECTORY_ENTRIES = 256

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class StatePanelError(Exception):
    """可安全原样交给已认证 Panel 用户的读取错误。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(slots=True)
class StatePanelResult:
    body: dict[str, Any]
    etag: str


@dataclass(slots=True)
class _Record:
    agent: str
    key: str
    source: str
    revision: int
    digest: str
    summary: str
    snapshot: dict[str, JsonValue]
    stored_at: str
    size_bytes: int
    etag: str
    replica_status: str = "only_copy"
    replica_note: str = "只有一个当前可见副本"

    def metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "revision": self.revision,
            "source": self.source,
            "digest": self.digest,
            "summary": self.summary,
            # 这是接收端文件的 mtime，不是发布者声明的业务 freshness。
            "stored_at": self.stored_at,
            "size_bytes": self.size_bytes,
            "etag": self.etag,
            "replica_status": self.replica_status,
            "replica_note": self.replica_note,
        }

    def detail(self) -> dict[str, Any]:
        return {"agent": self.agent, **self.metadata(), "snapshot": self.snapshot}


@dataclass(frozen=True, slots=True)
class _Issue:
    entry: str
    code: str
    detail: str
    status_code: int = 409

    def as_dict(self) -> dict[str, str]:
        return {"entry": self.entry, "code": self.code, "detail": self.detail}


def list_state_metadata(layout: NodeLayout, config: Config) -> StatePanelResult:
    """列出配置中每个 Agent 的状态元数据；坏文件只影响自己。"""
    groups: list[tuple[str, list[_Record], list[_Issue], bool]] = []
    all_records: list[_Record] = []
    remaining = MAX_STATE_RECORDS
    any_truncated = False

    for agent in sorted(config.agents):
        allowed = min(MAX_STATE_FILES_PER_AGENT, remaining)
        records, issues, truncated = _read_agent(layout, agent, allowed=allowed)
        remaining -= len(records) + sum(issue.code != "limit" for issue in issues)
        remaining = max(0, remaining)
        any_truncated = any_truncated or truncated
        groups.append((agent, records, issues, truncated))
        all_records.extend(records)

    _classify(all_records)
    body: dict[str, Any] = {
        "node": config.node.name,
        "agents": [
            {
                "agent": agent,
                "states": [record.metadata() for record in records],
                "issues": [issue.as_dict() for issue in issues],
                "truncated": truncated,
            }
            for agent, records, issues, truncated in groups
        ],
        "truncated": any_truncated,
        "limits": {
            "file_bytes": MAX_STATE_FILE_BYTES,
            "files_per_agent": MAX_STATE_FILES_PER_AGENT,
            "records_per_request": MAX_STATE_RECORDS,
            "directory_entries": MAX_STATE_DIRECTORY_ENTRIES,
        },
    }
    return StatePanelResult(body=body, etag=_response_etag(body))


def state_detail(layout: NodeLayout, config: Config, agent: str, key: str) -> StatePanelResult:
    """读取一个明确指定的副本；不会枚举未配置 Agent 或接受路径片段。"""
    if agent not in config.agents:
        raise StatePanelError(404, f"没有配置名为 {agent!r} 的 Agent")
    _validate_key(key)

    directory = layout.state_dir(agent)
    try:
        directory_fd = _open_directory(directory)
    except FileNotFoundError as exc:
        raise StatePanelError(404, f"{agent} 还没有状态副本") from exc
    except OSError as exc:
        raise StatePanelError(409, f"{agent} 的 state 目录不安全或无法读取") from exc

    try:
        record = _read_record(directory_fd, agent, f"{key}.json")
    except FileNotFoundError as exc:
        raise StatePanelError(404, f"{agent} 没有 state {key!r}") from exc
    except _ReadFailure as exc:
        raise StatePanelError(exc.issue.status_code, exc.issue.detail) from exc
    finally:
        os.close(directory_fd)

    body = {"node": config.node.name, **record.detail()}
    return StatePanelResult(body=body, etag=record.etag)


def _read_agent(
    layout: NodeLayout, agent: str, *, allowed: int
) -> tuple[list[_Record], list[_Issue], bool]:
    directory = layout.state_dir(agent)
    try:
        directory_fd = _open_directory(directory)
    except FileNotFoundError:
        return [], [], False
    except OSError:
        issue = _Issue("state", "unsafe_directory", "state 目录不是安全的普通目录或无法读取")
        return [], [issue], False

    records: list[_Record] = []
    issues: list[_Issue] = []
    truncated = False
    try:
        names, truncated = _state_names(directory_fd, allowed)
        for name in names:
            try:
                records.append(_read_record(directory_fd, agent, name))
            except FileNotFoundError:
                # 原子替换/并发清理可能让刚枚举的旧目录项消失；下一轮会收敛。
                continue
            except _ReadFailure as exc:
                issues.append(exc.issue)
        if truncated:
            limit = min(MAX_STATE_FILES_PER_AGENT, allowed)
            issues.append(_Issue("state", "limit", f"状态文件超过本次 {limit} 条显示上限"))
    finally:
        os.close(directory_fd)
    return records, issues, truncated


def _open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)


def _state_names(directory_fd: int, allowed: int) -> tuple[list[str], bool]:
    """目录本身也有界扫描；达到上限后不再遍历攻击者制造的海量目录项。"""
    names: list[str] = []
    scanned = 0
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if scanned >= MAX_STATE_DIRECTORY_ENTRIES:
                return sorted(names), True
            scanned += 1
            if not entry.name.endswith(".json"):
                continue
            if len(names) >= allowed:
                return sorted(names), True
            names.append(entry.name)
    return sorted(names), False


class _ReadFailure(Exception):
    def __init__(self, issue: _Issue) -> None:
        super().__init__(issue.detail)
        self.issue = issue


def _read_record(directory_fd: int, agent: str, name: str) -> _Record:
    key = name[:-5] if name.endswith(".json") else ""
    try:
        _validate_key(key)
    except StatePanelError as exc:
        raise _ReadFailure(_Issue(name, "invalid_name", exc.detail, exc.status_code)) from exc

    try:
        # O_NONBLOCK 让 FIFO 之类的非普通目录项不会在 fstat 前把请求永久挂住。
        file_fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=directory_fd)
    except OSError as exc:
        code = "symlink" if exc.errno in {getattr(os, "ELOOP", 40)} else "unreadable"
        detail = "拒绝读取符号链接" if code == "symlink" else "状态文件无法读取"
        raise _ReadFailure(_Issue(name, code, detail)) from exc

    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise _ReadFailure(_Issue(name, "not_regular", "状态条目不是普通文件"))
        if info.st_size > MAX_STATE_FILE_BYTES:
            raise _ReadFailure(
                _Issue(
                    name,
                    "oversize",
                    f"状态文件超过 {MAX_STATE_FILE_BYTES} 字节上限",
                    413,
                )
            )
        raw = _read_bounded(file_fd)
    finally:
        os.close(file_fd)

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ReadFailure(_Issue(name, "invalid_json", "状态文件不是合法 UTF-8 JSON")) from exc
    if not isinstance(data, dict) or data.get("version") != STATE_FILE_VERSION:
        raise _ReadFailure(_Issue(name, "invalid_version", "状态文件版本或结构非法"))

    try:
        source = _canonical_source(data["source"])
        payload = StateUpdatePayload.model_validate(
            {
                "key": data["key"],
                "revision": data["revision"],
                "digest": data["digest"],
                "summary": data["summary"],
                "snapshot": data["snapshot"],
            }
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise _ReadFailure(_Issue(name, "invalid_schema", "状态文件 schema 非法")) from exc
    if payload.key != key:
        raise _ReadFailure(_Issue(name, "key_mismatch", "文件名与内容声明的 state key 不一致"))
    if snapshot_digest(payload.snapshot) != payload.digest:
        raise _ReadFailure(_Issue(name, "digest_mismatch", "snapshot digest 校验失败"))

    stored_at = datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat()
    etag = _record_etag(agent, payload, source)
    return _Record(
        agent=agent,
        key=payload.key,
        source=source,
        revision=payload.revision,
        digest=payload.digest,
        summary=payload.summary,
        snapshot=payload.snapshot,
        stored_at=stored_at,
        size_bytes=len(raw),
        etag=etag,
    )


def _read_bounded(file_fd: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(file_fd, min(64 * 1024, MAX_STATE_FILE_BYTES + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_STATE_FILE_BYTES:
            raise _ReadFailure(
                _Issue(
                    "state",
                    "oversize",
                    f"状态文件超过 {MAX_STATE_FILE_BYTES} 字节上限",
                    413,
                )
            )


def _validate_key(key: str) -> None:
    if len(key) > 128 or not STATE_KEY_RE.fullmatch(key):
        raise StatePanelError(400, "state key 非法")


def _canonical_source(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("source 不是字符串")
    node, separator, agent = value.partition(":")
    if not separator:
        raise ValueError("source 不是 node:agent")
    address = Address(node=node, agent=agent)
    if address.is_role or address.is_broadcast or str(address) != value:
        raise ValueError("source 不是规范的具体 Agent 地址")
    return value


def _record_etag(agent: str, payload: StateUpdatePayload, source: str) -> str:
    identity = json.dumps(
        [agent, payload.key, source, payload.revision, payload.digest, payload.summary],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f'"state-{hashlib.sha256(identity).hexdigest()}"'


def _response_etag(body: dict[str, Any]) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f'"states-{hashlib.sha256(raw).hexdigest()}"'


def _classify(records: list[_Record]) -> None:
    by_key: dict[str, list[_Record]] = {}
    for record in records:
        by_key.setdefault(record.key, []).append(record)

    for replicas in by_key.values():
        sources = {record.source for record in replicas}
        revisions: dict[int, set[str]] = {}
        for record in replicas:
            revisions.setdefault(record.revision, set()).add(record.digest)
        if len(sources) > 1:
            _mark_conflict(replicas, "同一 key 的当前可见副本声明了不同 source")
            continue
        if any(len(digests) > 1 for digests in revisions.values()):
            _mark_conflict(replicas, "同一 source/revision 的当前可见副本 digest 不同")
            continue
        if len(replicas) == 1:
            continue

        highest = max(record.revision for record in replicas)
        if all(record.revision == highest for record in replicas):
            for record in replicas:
                record.replica_status = "in_sync"
                record.replica_note = f"{len(replicas)} 个当前可见副本 revision/digest 一致"
            continue
        for record in replicas:
            if record.revision < highest:
                record.replica_status = "behind"
                record.replica_note = f"比当前可见最高 revision 落后 {highest - record.revision}"
            else:
                record.replica_status = "ahead"
                record.replica_note = "这是当前可见最高 revision；另有副本落后"


def _mark_conflict(records: list[_Record], note: str) -> None:
    for record in records:
        record.replica_status = "conflict"
        record.replica_note = note
