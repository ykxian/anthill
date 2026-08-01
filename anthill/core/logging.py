"""结构化日志（03-tech-design §10）。

每条日志是一行 JSON，落在 `.anthill/logs/agentd-<name>.jsonl`。
它同时是三样东西：调试工具、`anthill log --follow` 的数据源、演示素材。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from rich.console import Console

from anthill.core.ids import now

LEVEL_STYLE = {
    "debug": "dim",
    "info": "cyan",
    "warn": "yellow",
    "error": "bold red",
}


class EventLog:
    """JSONL 落盘 + 可选终端回显。刻意不用 logging 模块：字段是结构化的，不是一行字符串。"""

    def __init__(
        self,
        path: Path | None,
        *,
        agent: str = "-",
        console: Console | None = None,
        echo: bool = True,
    ) -> None:
        self._agent = agent
        self._console = console or Console()
        self._echo = echo
        self._fh: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")

    def emit(self, event: str, level: str = "info", **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": now().isoformat(),
            "agent": self._agent,
            "level": level,
            "event": event,
            **{k: _plain(v) for k, v in fields.items()},
        }
        if self._fh is not None:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
        if self._echo:
            self._console.print(format_record(record))
        return record

    def info(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.emit(event, "info", **fields)

    def warn(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.emit(event, "warn", **fields)

    def error(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.emit(event, "error", **fields)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> EventLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def format_record(record: dict[str, Any]) -> str:
    """给 rich 用的一行渲染：时间 · agent · 事件 · 其余字段。"""
    level = str(record.get("level", "info"))
    style = LEVEL_STYLE.get(level, "white")
    ts = str(record.get("ts", ""))[11:23]
    rest = " ".join(
        f"[dim]{k}=[/dim]{v}"
        for k, v in record.items()
        if k not in {"ts", "agent", "level", "event"}
    )
    return (
        f"[dim]{ts}[/dim] [{style}]{record.get('event', '')}[/{style}] "
        f"[b]{record.get('agent', '')}[/b] {rest}"
    )


def read_log(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    return [parsed for line in lines if (parsed := _safe_load(line)) is not None]


def follow_log(path: Path, *, poll_interval: float = 0.4) -> Iterator[dict[str, Any]]:
    """`tail -f` 式跟随。文件还不存在时先等它出现。"""
    import time

    while not path.is_file():
        time.sleep(poll_interval)
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(0, 2)
        while True:
            line = fh.readline()
            if not line:
                time.sleep(poll_interval)
                continue
            parsed = _safe_load(line)
            if parsed is not None:
                yield parsed


def _safe_load(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _plain(value: Any) -> Any:
    """日志字段必须是可 JSON 序列化的标量，别把整个对象塞进去。"""
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    return str(value)
