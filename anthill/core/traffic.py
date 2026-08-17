"""把「谁跟谁说了什么」从各个信箱的归档里读回来，按 thread 拼成对话。

## 为什么这一页非有不可

面板原来有三种「看见」，但没有一种能回答**「tst1 和 tst2 到底聊了什么」**：

- 消息流：结构化日志（`msg.received`、`bridge.replied`），有谁发给谁、什么时候，
  **但没有正文** —— 它是给排障用的，不是给读对话用的；
- 对话页：只有你从这一页发起的那些；
- 桥接页：只有桥接 Agent 自己那一份，而且是**单边视角** ——
  tst1 那边看到的是「tst2 说了什么」，tst2 那边看到的是「我回了什么」。

而两个 Agent 互相聊天恰恰是这个项目要证明的事。看不见它，等于没有证据。

## 数据在哪

每个 Agent 处理完一条消息都会把信封归档（见 `Mailbox.archive`）：

    agents/<名字>/mailbox/inbox/done/<日期>/<信封id>.json

关键是**只读收件方的归档**。一条消息在全网只会被投递到一个信箱，所以
「所有 Agent 收到的信封」这个并集里，每条消息恰好出现一次 —— 天然不重不漏。
（要是把发件方的记录也并进来，同一句话会出现两遍：一遍是 tst1 发的，
一遍是 tst2 收的。）

按 `thread` 分组，一段对话就还原了。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthill.core.envelope import Envelope
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout

DEFAULT_MESSAGES = 400
"""一次最多读几个归档文件。**回执也算在里面** —— 见 `conversations`。"""

DEFAULT_THREADS = 30
RECEIPT_PREFIX = "receipt."

# 正文上限，对齐 chat.py 的 BODY_LIMIT。以前是 800 字还把换行压平 ——
# 审查意见这类消息动辄上千字、靠换行分点，截完只剩开头残句。
# 真截了就在消息上带 clipped 说一声，页面照实标出来。
BODY_LIMIT = 4000


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    ts: str
    frm: str
    to: str
    kind: str
    thread: str
    body: str
    clipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short": self.id[-6:],
            "ts": self.ts,
            "frm": self.frm,
            "to": self.to,
            "kind": self.kind,
            "body": self.body,
            "clipped": self.clipped,
        }


def conversations(
    layout: NodeLayout,
    *,
    humans: frozenset[str] = frozenset(),
    extra: Iterable[dict[str, Any]] = (),
    messages: int = DEFAULT_MESSAGES,
    threads: int = DEFAULT_THREADS,
) -> dict[str, Any]:
    """这个节点上所有 Agent 的往来，按 thread 拼成对话，最近的在前。

    两道上限都是必要的：归档在 `keep_days` 内是**只增不减**的，
    而这一页每几秒拉一次。先按文件 mtime 取最近的一批再解析，
    别把几千个 json 读一遍。

    **回执（`receipt.*`）不进对话正文。** 它们没有正文，而且每说一句就配一条，
    掺进去的话一段四句话的对话会显示成八条，一半是空的 —— 那不是对话记录。
    但也不能悄悄扔掉：每个 thread 上带一个 `receipts` 计数，页面照实说
    「另有 N 条回执」。回执本身的成败在消息流那一页看得到。
    """
    collected = _newest(layout, limit=messages)
    # 归档里已经有的以归档为准 —— 那份带真正的投递时刻，本机记的只是「我发了」
    known = {record.id for record in collected}
    for item in extra:
        if str(item.get("id", "")) in known:
            continue
        body, clipped = _clip(str(item.get("body", "")))
        collected.append(
            Message(
                id=str(item.get("id", "")),
                ts=str(item.get("ts", "")),
                frm=str(item.get("frm", "")),
                to=str(item.get("to", "")),
                kind=str(item.get("kind", "chat")),
                thread=str(item.get("thread", "")),
                body=body,
                clipped=clipped,
            )
        )
    grouped: dict[str, list[Message]] = {}
    receipts: dict[str, int] = {}
    for record in collected:
        if record.kind.startswith(RECEIPT_PREFIX):
            receipts[record.thread] = receipts.get(record.thread, 0) + 1
            continue
        grouped.setdefault(record.thread, []).append(record)

    out = [
        _thread_dict(thread, msgs, receipts=receipts.get(thread, 0), humans=humans)
        for thread, msgs in grouped.items()
    ]
    out.sort(key=lambda item: item["last"], reverse=True)
    return {
        "threads": out[:threads],
        # 截断了就得说出来 —— 页面上「只有这些」和「还有更多没显示」是两回事
        "truncated": len(collected) >= messages,
    }


def _thread_dict(
    thread: str, msgs: list[Message], *, receipts: int, humans: frozenset[str]
) -> dict[str, Any]:
    ordered = sorted(msgs, key=lambda m: (m.ts, m.id))
    peers: list[str] = []
    for record in ordered:
        for who in (record.frm, record.to):
            if who not in peers:
                peers.append(who)
    return {
        "thread": thread,
        "short": thread[-6:],
        "started": ordered[0].ts,
        "last": ordered[-1].ts,
        "peers": peers,
        "count": len(ordered),
        "receipts": receipts,
        # 「你跟 Agent 说的」和「Agent 之间说的」是两个问题，页面上要能分开看。
        # **标记而不是过滤**：滤在服务端的话，勾一下就得重新拉一次，
        # 而这本来只是把已经在手里的东西藏一半。
        "with_human": any(_bare(p) in humans for p in peers),
        "messages": [record.as_dict() for record in ordered],
    }


def _bare(address: str) -> str:
    """`collab-tst:cli` → `cli`。归档里带节点前缀，配置里的 Agent 名是裸的。"""
    return address.split(":", 1)[-1]


def _newest(layout: NodeLayout, *, limit: int) -> list[Message]:
    candidates: list[tuple[float, Path]] = []
    for agent in layout.known_agents():
        inbox = layout.mailbox_dir(agent) / "inbox"
        # **不能只读 done/。** 归档是「处理完」才发生的，而有些 Agent 根本没有
        # 处理者 —— `cli` 就是：它只是 `anthill send` 用来收回执和结果的信箱，
        # 从来没有 agentd 跑它。于是别人回给你的话永远停在 new/，
        # 只读 done/ 的话「它回了」和「页面上什么都没有」会同时成立。
        # 收件箱里躺着的消息一样是**已经送到**的消息，就该看得见。
        for path in _archive_files(inbox / "done"):
            candidates.append((_mtime(path), path))
        for stage in ("new", "cur"):
            candidates.extend((_mtime(p), p) for p in _loose(inbox / stage))
    candidates.sort(key=lambda item: item[0], reverse=True)

    out: list[Message] = []
    seen: set[str] = set()
    for _, path in candidates[:limit]:
        record = _read(path)
        if record is None or record.id in seen:
            continue
        seen.add(record.id)
        out.append(record)
    return out


def _archive_files(done: Path) -> list[Path]:
    """`done/<日期>/*.json`。`done/invalid/` 里是解析不了的，跳过。"""
    try:
        days = sorted(done.iterdir(), reverse=True)
    except OSError:
        return []
    out: list[Path] = []
    for day in days:
        if not day.is_dir() or day.name == "invalid":
            continue
        try:
            out.extend(p for p in day.iterdir() if p.suffix == ".json")
        except OSError:
            continue
    return out


def _loose(stage: Path) -> list[Path]:
    """`new/` 或 `cur/` 里还没被处理的信封 —— 没有按天分的子目录。"""
    try:
        return [p for p in stage.iterdir() if p.suffix == ".json"]
    except OSError:
        return []


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read(path: Path) -> Message | None:
    try:
        env = Envelope.from_json_bytes(path.read_bytes())
    except (OSError, AntHillError):
        return None
    body, clipped = _text(env)
    return Message(
        id=env.id,
        ts=env.ts.isoformat(),
        frm=str(env.from_),
        to=str(env.to),
        kind=str(env.type),
        thread=env.thread,
        body=body,
        clipped=clipped,
    )


def _text(env: Envelope) -> tuple[str, bool]:
    payload: Any = env.payload
    title = str(getattr(payload, "title", "") or "").strip()
    body = str(getattr(payload, "body", "") or getattr(payload, "summary", "") or "").strip()
    if not title or (body and body.startswith(title.rstrip("…"))):
        text = body or title
    else:
        text = f"{title}\n\n{body}"
    return _clip(text)  # (正文, 是否被截)


def _clip(text: str) -> tuple[str, bool]:
    """截到上限，**换行原样保留** —— 这是对话正文，不是一行摘要。"""
    tidy = text.strip()
    return tidy[:BODY_LIMIT], len(tidy) > BODY_LIMIT
