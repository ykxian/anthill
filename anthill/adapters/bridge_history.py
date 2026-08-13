"""读桥接的**归档**，把「已经发生过的事」还原成一条时间线。

## 为什么需要它

面板的桥接页原来只显示 `inbox/` —— 也就是**还在等你回**的消息。
这在人肉回复时没问题，但接上 Claude Code 之后就反了：会话几秒内就回完了，
消息立刻从 `inbox/` 移到 `done/`，于是**页面永远是空的**。

干得越好，页面越空。你在网页上看不到任何证据说明这套东西正在工作。

而记录其实一直都在，只是没人读它：

    done/<id>.json   收到的那个信封（谁发的、什么时候、正文、thread）
    done/<id>.md     回出去的正文（回复归档时覆盖掉收件note，见 bridge.py 的 _archive）
    done/<随便>.md   主动发起的那条（文件名不是信封 id，没有配对的 .json）
    done/<x>.md.failed  发失败的

这个模块只做一件事：把这堆文件读成结构化的往来记录。**只读，不改。**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthill.adapters.bridge import DONE, parse_note
from anthill.core.envelope import Envelope
from anthill.core.errors import AntHillError
from anthill.core.ids import is_valid_id

DEFAULT_LIMIT = 40
BODY_PREVIEW = 500
FAILED_SUFFIX = ".failed"


@dataclass(frozen=True, slots=True)
class Exchange:
    """一次往来。收到的那条 + （如果回了）回出去的那句。"""

    id: str
    ts: str
    direction: str
    """`in` = 别人找我；`out` = 我主动发的。"""

    peer: str
    kind: str
    thread: str
    incoming: str
    reply: str
    failed: bool
    at: float
    """排序用的时刻（epoch 秒）。**不是文件的 mtime** —— 见 `recent`。"""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short": self.id[-6:],
            "ts": self.ts,
            "direction": self.direction,
            "peer": self.peer,
            "kind": self.kind,
            "thread": self.thread,
            "incoming": self.incoming,
            "reply": self.reply,
            "answered": bool(self.reply),
            "failed": self.failed,
        }


def recent(bridge_root: Path, *, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """最近的往来，新的在前。

    **挑用 mtime，排序用信封上的时刻** —— 这两件事不能用同一个数。

    挑的时候只能靠 mtime：面板每两秒拉一次，不可能把整个 done/ 解析一遍
    （跑久了那里面有几千个文件）。

    但排序不能用它。mtime 是**归档**的时刻，不是消息到达的时刻：一条没人回的
    消息会一直躺在 inbox/ 里，等对话结束才被归档 —— 于是它的 mtime 比后来
    收到又秒回的消息还新。这一栏读起来是一段对话，顺序错了就不是对话了。
    """
    done = bridge_root / DONE
    try:
        entries = sorted(done.iterdir(), key=_mtime, reverse=True)
    except OSError:
        return []

    out: list[Exchange] = []
    seen: set[str] = set()
    for path in entries:
        if len(out) >= limit:
            break
        stem = _stem(path)
        if stem in seen or path.is_dir():
            continue
        seen.add(stem)
        record = _read(done, stem, fallback_at=_mtime(path))
        if record is not None:
            out.append(record)
    out.sort(key=lambda record: record.at, reverse=True)
    return [record.as_dict() for record in out]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _stem(path: Path) -> str:
    """`01K….md` / `01K….json` / `01K….md.failed` 都归到同一次往来。"""
    name = path.name
    if name.endswith(FAILED_SUFFIX):
        name = name[: -len(FAILED_SUFFIX)]
    return name.rsplit(".", 1)[0]


def _read(done: Path, stem: str, *, fallback_at: float) -> Exchange | None:
    envelope = _envelope(done / f"{stem}.json")
    text_path, failed = _text_path(done, stem)
    headers, body = _parse(text_path)

    if envelope is not None:
        # 收到的那条。`done/<id>.md` 这时候是**回复**——除非它还带着收件 note 的
        # front matter（`id:` 等于文件名），那就说明这条根本没被回复过就归档了
        # （对话到头、hop 用尽）。这个判据比「文件在不在」可靠。
        answered = headers.get("id", "") != stem
        return Exchange(
            id=envelope.id,
            ts=envelope.ts.isoformat(),
            direction="in",
            peer=str(envelope.from_),
            kind=str(envelope.type),
            thread=envelope.thread,
            incoming=_clip(_body_of(envelope)),
            reply=_clip(body) if answered else "",
            failed=failed,
            at=envelope.ts.timestamp(),
        )

    if not body.strip():
        return None
    # 没有配对信封 = 主动发起的那条（文件名是随手起的）。
    # 它没有信封时刻，只能拿归档时刻顶上 —— 而那对「自己发出去的」恰好是准的：
    # 主动发的消息写进 outbox 就立刻被发走、归档，两个时刻差不了几秒。
    return Exchange(
        id=stem if is_valid_id(stem) else "",
        ts=_iso(fallback_at),
        direction="out",
        peer=headers.get("to", ""),
        kind=headers.get("type", "chat"),
        thread=headers.get("thread", ""),
        incoming="",
        reply=_clip(body),
        failed=failed,
        at=fallback_at,
    )


def _envelope(path: Path) -> Envelope | None:
    try:
        return Envelope.from_json_bytes(path.read_bytes())
    except (OSError, AntHillError):
        return None


def _text_path(done: Path, stem: str) -> tuple[Path, bool]:
    plain = done / f"{stem}.md"
    if plain.is_file():
        return plain, False
    return done / f"{stem}.md{FAILED_SUFFIX}", True


def _parse(path: Path) -> tuple[dict[str, str], str]:
    try:
        return parse_note(path.read_text(encoding="utf-8"))
    except OSError:
        return {}, ""


def _body_of(env: Envelope) -> str:
    payload: Any = env.payload
    return str(getattr(payload, "body", "") or getattr(payload, "summary", "") or "").strip()


def _clip(text: str) -> str:
    return " ".join(text.split())[:BODY_PREVIEW]


def _iso(epoch: float) -> str:
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
