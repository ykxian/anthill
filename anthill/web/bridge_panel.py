"""在面板上当那个「人」。

桥接 Agent 的设计是：收到的消息写成 `bridge/inbox/*.md`，人（或一个常驻的
Claude Code 会话）把回复写进 `bridge/outbox/`。这本来就是给人留的口子。

但只有文件这一条路的话，在网页上加完 bridge Agent 之后是这样的：
目录建好了、里面空着、页面上什么都看不到，你还得去终端交代一遍「盯着那个目录」。
**加它的地方和用它的地方不是同一个地方** —— 这就不对。

所以这一层把同一批文件搬到页面上：等你回的消息列出来，回复直接在页面上写，
也能主动发一条。写的还是那些文件，所以**和 Claude Code 会话完全并存**：
它照样可以盯着目录，你也可以在网页上先替它回一句。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anthill.adapters.bridge import BRIDGE_DIR, BridgeHandler, parse_note
from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.ids import is_valid_id, new_id
from anthill.core.paths import NodeLayout

MAX_BODY = 32_000
PREVIEW = 400


class BridgeReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_BODY)
    to: str = Field(default="", max_length=200)
    """主动发一条时的收件人。留空 = 回复 `id` 指的那条。"""

    kind: str = Field(default="chat", max_length=16)


def bridge_agents(config: Config) -> list[str]:
    return sorted(name for name, agent in config.agents.items() if agent.bridge)


def _handler(layout: NodeLayout, config: Config, agent: str) -> BridgeHandler:
    section = config.agents.get(agent)
    if section is None or not section.bridge:
        raise AntHillError(f"{agent} 不是桥接 Agent（node.toml 里要写 bridge = true）")
    return BridgeHandler(root=layout.agent_dir(agent), agent_name=agent)


def inbox(layout: NodeLayout, config: Config, agent: str) -> dict[str, Any]:
    """等这个人回复的消息，外加「文件在哪」和「该跟 Claude Code 说什么」。

    路径和那句话都放进来，是因为页面上加完 bridge 之后最该被告知的就是这两样。
    """
    handler = _handler(layout, config, agent)
    waiting = []
    for path in sorted(handler.dir("inbox").glob("*.md")):
        headers, body = parse_note(path.read_text(encoding="utf-8"))
        waiting.append(
            {
                "id": path.stem,
                "short": path.stem[-6:],
                "frm": headers.get("from", ""),
                "type": headers.get("type", ""),
                "thread": headers.get("thread", ""),
                "body": " ".join(body.split())[:PREVIEW],
            }
        )
    return {
        "agent": agent,
        "waiting": waiting,
        "dir": str(handler.root),
        "prompt": watch_prompt(handler.root),
    }


def watch_prompt(root: Path) -> str:
    """给常驻会话粘的那句话。写在这里，省得人自己拼路径拼错。"""
    return (
        f"盯着 {root / 'inbox'}，出现新的 .md 就读，"
        f"把回复写进 {root / 'outbox'} 下同名的文件。"
        "收消息不阻塞，你可以慢慢想；想主动说一句就在 outbox 里放一个带 `to:` 的文件。"
    )


def reply(
    layout: NodeLayout, config: Config, agent: str, msg_id: str, body: BridgeReply
) -> dict[str, Any]:
    """在页面上替这个人回一句 —— 落成 outbox 里的文件，和手写完全一样。

    `msg_id` 是从 URL 上来的，而它下一步就是个文件名 —— 不校验的话，
    一个 `../` 就能把这次写落到目录外面去。ULID 校验一并把这条路堵死。
    """
    if not is_valid_id(msg_id):
        raise AntHillError(f"不是一个消息 ID：{msg_id}")
    handler = _handler(layout, config, agent)
    if not (handler.dir("inbox") / f"{msg_id}.md").is_file():
        raise AntHillError(f"没有在等回复的消息 {msg_id}")
    (handler.dir("outbox") / f"{msg_id}.md").write_text(body.text, encoding="utf-8")
    return {"ok": True, "agent": agent, "id": msg_id}


def speak(layout: NodeLayout, config: Config, agent: str, body: BridgeReply) -> dict[str, Any]:
    """主动发一条（不是对谁的回复）。

    outbox 里放一个带 `to:` 的文件就是主动发起 —— 这个能力是桥接设计的副产品，
    但恰恰是「人随时能插进正在进行的协作里说一句」的那半。
    """
    if not body.to.strip():
        raise AntHillError("主动发一条得指明收件人")
    handler = _handler(layout, config, agent)
    note = f"---\nto: {body.to.strip()}\nkind: {body.kind}\n---\n{body.text}"
    name = f"{new_id()}.md"
    (handler.dir("outbox") / name).write_text(note, encoding="utf-8")
    return {"ok": True, "agent": agent, "file": f"{BRIDGE_DIR}/outbox/{name}"}
