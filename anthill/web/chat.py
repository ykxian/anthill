"""面板对话：在页面上直接跟任意一台机器上的任意 Agent 说话。

**为什么需要单独记一笔，而不是直接读邮箱**：收到的信确实躺在 `cli` 的邮箱里，
可发出去的信不在 —— 它被投到对方的邮箱里去了，本机的 outbox 只是投递途中的
暂存，投完就清。要在页面上看到一来一回，就得在发的时候自己记一条。

所以一个会话 = 「本机记的发件」+「cli 邮箱里这个 thread 的来信」，
按 ULID 排序合并。ULID 前缀是时间戳，所以按 id 排就是按时间排。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anthill.core.envelope import Envelope
from anthill.core.errors import AntHillError
from anthill.core.ids import now
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType

CHAT_DIR = "chats"
MAX_MESSAGES = 200
MAX_THREADS = 40
BODY_LIMIT = 4000


def _dir(layout: NodeLayout) -> Path:
    path = layout.root / CHAT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_outgoing(layout: NodeLayout, env: Envelope, body: str) -> None:
    """把刚发出去的那条记下来。追加写，一个 thread 一个文件。"""
    line = json.dumps(
        {
            "id": env.id,
            "ts": now().isoformat(),
            "frm": str(env.from_),
            "to": str(env.to),
            "body": body[:BODY_LIMIT],
            "mine": True,
        },
        ensure_ascii=False,
    )
    path = _dir(layout) / f"{env.thread}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def messages(layout: NodeLayout, thread: str, *, agent: str = "cli") -> list[dict[str, Any]]:
    """一个会话的全部来回，按时间排。"""
    merged = {m["id"]: m for m in _sent(layout, thread)}
    for m in _received(layout, thread, agent=agent):
        merged.setdefault(m["id"], m)
    ordered = sorted(merged.values(), key=lambda m: str(m.get("id", "")))
    return ordered[-MAX_MESSAGES:]


def threads(layout: NodeLayout, *, agent: str = "cli") -> list[dict[str, Any]]:
    """最近的会话列表，给面板左边那栏用。"""
    out: list[dict[str, Any]] = []
    for path in sorted(_dir(layout).glob("*.jsonl"), reverse=True)[:MAX_THREADS]:
        thread = path.stem
        history = messages(layout, thread, agent=agent)
        if not history:
            continue
        last = history[-1]
        out.append(
            {
                "thread": thread,
                "with": _counterpart(history),
                "last": str(last.get("body", ""))[:80],
                "ts": str(last.get("ts", "")),
                "count": len(history),
            }
        )
    return sorted(out, key=lambda t: str(t["ts"]), reverse=True)


def recorded(layout: NodeLayout, *, limit: int = MAX_THREADS) -> list[dict[str, Any]]:
    """**刚发出去、还没被对方归档**的那些，补给「往来」那一页。

    往来读的是收件方的归档，所以一条消息要等对方处理完才看得见 —— 通常不到一秒，
    但对方**没启动**的时候就是永远看不见。于是页面上「我明明发了」和「什么都没有」
    同时成立，看起来就像消息丢了。

    本机在发的时候自己记过一条（`record_outgoing`），拿它兜住这个空档。
    重复的由调用方按信封 id 去掉 —— 归档里那份更权威（它带真正的投递时刻）。
    """
    out: list[dict[str, Any]] = []
    for path in sorted(_dir(layout).glob("*.jsonl"), reverse=True)[:limit]:
        for entry in _sent(layout, path.stem):
            body = str(entry.get("body", "")).strip()
            if not body:
                continue
            out.append(
                {
                    "id": str(entry.get("id", "")),
                    "ts": str(entry.get("ts", "")),
                    "frm": str(entry.get("frm", "")),
                    "to": str(entry.get("to", "")),
                    "kind": "chat",
                    "thread": path.stem,
                    "body": body,
                }
            )
    return out


def _counterpart(history: list[dict[str, Any]]) -> str:
    """这个会话是在跟谁说话 —— 取第一条发件的收件人，没有就取第一条来信的发件人。"""
    for message in history:
        if message.get("mine"):
            return str(message.get("to", ""))
    return str(history[0].get("frm", "")) if history else ""


def _sent(layout: NodeLayout, thread: str) -> list[dict[str, Any]]:
    path = _dir(layout) / f"{thread}.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 半行就跳过，别让一条坏记录毁掉整个会话
    except OSError:
        return []
    return out


def _received(layout: NodeLayout, thread: str, *, agent: str) -> list[dict[str, Any]]:
    """从 `cli` 的邮箱里捞这个 thread 的来信（含回执与结果）。"""
    mailbox = Mailbox(layout.mailbox_dir(agent))
    if not mailbox.exists:
        return []
    out: list[dict[str, Any]] = []
    for path in [*mailbox.list_new(), *sorted(mailbox.cur.glob("*.json")), *_recent_done(mailbox)]:
        try:
            env = Mailbox.read_envelope(path)
        except AntHillError:
            continue
        if env.thread != thread:
            continue
        out.append(
            {
                "id": env.id,
                "ts": env.ts.isoformat(),
                "frm": str(env.from_),
                "to": str(env.to),
                "body": _body(env),
                "type": str(env.type),
                "mine": False,
            }
        )
    return out


def _recent_done(mailbox: Mailbox) -> list[Path]:
    """归档按日期分目录；只翻最近几天，别把整个历史都读一遍。"""
    if not mailbox.done.is_dir():
        return []
    days = sorted((d for d in mailbox.done.iterdir() if d.is_dir()), reverse=True)[:3]
    return [p for day in days for p in sorted(day.glob("*.json"))]


def _body(env: Envelope) -> str:
    """不同类型的正文藏在不同字段里，面板只想要「那句话」。"""
    payload = env.payload
    for field in ("body", "summary", "error", "reason", "title"):
        value = getattr(payload, field, None)
        if isinstance(value, str) and value.strip():
            return value[:BODY_LIMIT]
    if env.type is MessageType.RECEIPT_ACCEPTED:
        return "（已受理）"
    return ""
