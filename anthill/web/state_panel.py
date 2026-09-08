"""节点共享状态公告的有界、只读 Panel 数据层。

列表只给摘要元数据；完整 ``snapshot`` 必须按 key 单独读取。Panel 复用 Core
的唯一磁盘 schema 校验，不实现第二套状态合并或 replica 比较。
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

from anthill.core.config import Config
from anthill.core.errors import ProtocolError
from anthill.core.paths import NodeLayout
from anthill.core.state_sync import (
    MAX_STATE_FILE_BYTES,
    STATE_KEY_RE,
    STATE_SCOPE,
    StateRecord,
    decode_state_document,
)

MAX_STATE_FILES = 256
MAX_STATE_DIRECTORY_ENTRIES = 512

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
    state: StateRecord
    stored_at: str
    size_bytes: int
    etag: str

    def metadata(self) -> dict[str, Any]:
        return {
            "key": self.state.key,
            "publisher": self.state.publisher,
            "revision": self.state.revision,
            "digest": self.state.digest,
            "summary": self.state.summary,
            # 文件 mtime 只是本节点存储时间，不是 publisher 声明的业务 freshness。
            "stored_at": self.stored_at,
            "size_bytes": self.size_bytes,
            "etag": self.etag,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.metadata(), "snapshot": self.state.snapshot}


@dataclass(frozen=True, slots=True)
class _Issue:
    entry: str
    code: str
    detail: str
    status_code: int = 409

    def as_dict(self) -> dict[str, str]:
        return {"entry": self.entry, "code": self.code, "detail": self.detail}


def list_state_metadata(layout: NodeLayout, config: Config) -> StatePanelResult:
    """列出一个本机 workspace 的共享状态元数据；坏文件只影响自己。"""
    records, issues, truncated = _read_shared(layout)
    body: dict[str, Any] = {
        "node": config.node.name,
        "scope": STATE_SCOPE,
        "states": [record.metadata() for record in records],
        "issues": [issue.as_dict() for issue in issues],
        "truncated": truncated,
        "limits": {
            "file_bytes": MAX_STATE_FILE_BYTES,
            "files": MAX_STATE_FILES,
            "directory_entries": MAX_STATE_DIRECTORY_ENTRIES,
        },
    }
    return StatePanelResult(body=body, etag=_response_etag(body))


def state_detail(layout: NodeLayout, config: Config, key: str) -> StatePanelResult:
    """按 key 读取一个完整快照；不存在 Agent/replica 维度。"""
    _validate_key(key)
    directory = layout.state_dir
    try:
        directory_fd = _open_directory(directory)
    except FileNotFoundError as exc:
        raise StatePanelError(404, "本节点还没有共享状态公告") from exc
    except OSError as exc:
        raise StatePanelError(409, "共享 state 目录不安全或无法读取") from exc

    try:
        record = _read_record(directory_fd, f"{key}.json")
    except FileNotFoundError as exc:
        raise StatePanelError(404, f"本节点没有 state {key!r}") from exc
    except _ReadFailure as exc:
        raise StatePanelError(exc.issue.status_code, exc.issue.detail) from exc
    finally:
        os.close(directory_fd)

    body = {"node": config.node.name, "scope": STATE_SCOPE, "state": record.detail()}
    return StatePanelResult(body=body, etag=record.etag)


def _read_shared(layout: NodeLayout) -> tuple[list[_Record], list[_Issue], bool]:
    directory = layout.state_dir
    try:
        directory_fd = _open_directory(directory)
    except FileNotFoundError:
        return [], [], False
    except OSError:
        issue = _Issue("state", "unsafe_directory", "state 目录不是安全的普通目录或无法读取")
        return [], [issue], False

    records: list[_Record] = []
    issues: list[_Issue] = []
    try:
        names, truncated = _state_names(directory_fd)
        for name in names:
            try:
                records.append(_read_record(directory_fd, name))
            except FileNotFoundError:
                # 原子替换或并发清理可能让刚枚举的旧目录项消失；下一轮会收敛。
                continue
            except _ReadFailure as exc:
                issues.append(exc.issue)
        if truncated:
            issues.append(
                _Issue(
                    "state",
                    "limit",
                    f"状态文件超过本次 {MAX_STATE_FILES} 条显示上限",
                )
            )
    finally:
        os.close(directory_fd)
    return records, issues, truncated


def _open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)


def _state_names(directory_fd: int) -> tuple[list[str], bool]:
    """目录扫描有界；锁和其他非 JSON 条目不进入公告列表。"""
    names: list[str] = []
    scanned = 0
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if scanned >= MAX_STATE_DIRECTORY_ENTRIES:
                return sorted(names), True
            scanned += 1
            if not entry.name.endswith(".json"):
                continue
            if len(names) >= MAX_STATE_FILES:
                return sorted(names), True
            names.append(entry.name)
    return sorted(names), False


class _ReadFailure(Exception):
    def __init__(self, issue: _Issue) -> None:
        super().__init__(issue.detail)
        self.issue = issue


def _read_record(directory_fd: int, name: str) -> _Record:
    key = name[:-5] if name.endswith(".json") else ""
    try:
        _validate_key(key)
    except StatePanelError as exc:
        raise _ReadFailure(_Issue(name, "invalid_name", exc.detail, exc.status_code)) from exc

    try:
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
                _Issue(name, "oversize", f"状态文件超过 {MAX_STATE_FILE_BYTES} 字节上限", 413)
            )
        raw = _read_bounded(file_fd)
    finally:
        os.close(file_fd)

    try:
        state = decode_state_document(raw, expected_key=key)
    except ProtocolError as exc:
        raise _ReadFailure(_Issue(name, "invalid_document", str(exc))) from exc

    stored_at = datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat()
    etag = _record_etag(state)
    return _Record(state=state, stored_at=stored_at, size_bytes=len(raw), etag=etag)


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


def _record_etag(state: StateRecord) -> str:
    identity = json.dumps(
        [state.key, state.publisher, state.revision, state.digest, state.summary],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f'"state-{hashlib.sha256(identity).hexdigest()}"'


def _response_etag(body: dict[str, Any]) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f'"states-{hashlib.sha256(raw).hexdigest()}"'
