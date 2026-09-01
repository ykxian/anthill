"""发件的本机记录 —— 对话页上「我发的那半句」的数据源。

对话页的主数据是**收件方的归档**（core/traffic.py），可发出去的信不在本机：
它被投到对方邮箱里，本机 outbox 只是途中暂存。本机收件人还能靠对方归档
兜回来，**跨机器就彻底没了** —— 收件方的归档在另一台机器上。
所以发出去的时候自己记一条，追加写，一个 thread 一个 jsonl。

从 web/chat.py 挪来：面板、CLI、桥接 Agent 三条发件路都要记，
让 cli/ 和 adapters/ 反向 import web/ 是错的方向。
"""

from __future__ import annotations

import json
from pathlib import Path

from anthill.core.envelope import Envelope
from anthill.core.ids import now
from anthill.core.paths import NodeLayout

CHAT_DIR = "chats"
BODY_LIMIT = 4000


def chats_dir(layout: NodeLayout) -> Path:
    path = layout.root / CHAT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_outgoing(layout: NodeLayout, env: Envelope, body: str) -> None:
    """把刚发出去的那条记下来；同一 Envelope 的重试不重复追加。"""
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
    path = chats_dir(layout) / f"{env.thread}.jsonl"
    if _contains(path, env.id):
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _contains(path: Path, msg_id: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            if json.loads(line).get("id") == msg_id:
                return True
        except (json.JSONDecodeError, AttributeError):
            continue
    return False
