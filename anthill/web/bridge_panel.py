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

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anthill.adapters.bridge import BRIDGE_DIR, BridgeHandler, parse_note
from anthill.adapters.bridge_session import last_claim, read_claim
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
        "prompt": watch_prompt(layout, agent),
        "connect": connect_recipes(layout, agent),
        "claims": [
            {
                "agent": name,
                **(c.as_dict() if (c := read_claim(layout, name)) else {"pid": 0}),
                # 空闲的也要说「上次是谁」—— 认领有目录亲和性，
                # 空闲不等于下一个会话随机拿到它
                "last_cwd": (p.cwd if (p := last_claim(layout, name)) else ""),
            }
            for name in bridge_agents(config)
        ],
    }


def anthill_exe() -> str:
    """这台机器上 `anthill` 的可执行路径。

    **不能直接写 `anthill`** —— 它在不在 PATH 上取决于装法（`uv run` 装出来的
    只在 venv 里）。页面上给的命令要能原样粘出去就跑得通，所以这里给绝对路径。
    """
    candidate = Path(sys.executable).with_name("anthill")
    if candidate.is_file():
        return str(candidate)
    return shutil.which("anthill") or "anthill"


def connect_recipes(layout: NodeLayout, agent: str) -> dict[str, Any]:
    """把「怎么让一个终端会话接进来」写成可以直接粘的三段。

    以前这一页只给一句「盯着这个目录」的提示词 —— 那是最被动的一条路，
    而另外两条（hook 自动查收、MCP 原生工具）只写在文档里。
    人是在这一页上想起「我要把 Claude Code 接进来」的，东西就该摆在这儿。

    路径全部由服务端填好：`anthill` 在不在 PATH 上、工作区在哪，
    页面猜不出来，而猜错的结果是粘过去跑不通。
    """
    exe = anthill_exe()
    workspace = str(layout.workspace)
    hook = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "command": f"{exe} bridge --json -w {workspace}",
                    "description": "每轮开始前看看 AntHill 那边有没有消息在等这个会话",
                }
            ]
        }
    }
    return {
        "exe": exe,
        "workspace": workspace,
        "agent": agent,
        "hook_path": "~/.claude/settings.json",
        "hook": json.dumps(hook, ensure_ascii=False, indent=2),
        # **一次性的全局配置** —— 命令里不写 Agent 名，让每个会话自己认领。
        # 写死名字的话，同一份配置下开几个会话就有几个抢同一个 Agent。
        "mcp": f"claude mcp add --scope user anthill -- {exe} mcp serve -w {workspace}",
        "pin": f"ANTHILL_AGENT={agent} claude",
        # 一个会话挂多个工作区：**再加一台 server 就行**，名字不同即可。
        # MCP 客户端按 server 名给工具分命名空间，两套 anthill_* 不会撞；
        # 而每台 server 的自我介绍里都写着自己是哪个节点、哪个工作区。
        "multi": (
            f"claude mcp add --scope user anthill-{Path(workspace).name} "
            f"-- {exe} mcp serve -w {workspace}"
        ),
    }


def watch_prompt(layout: NodeLayout, agent: str) -> str:
    """给常驻会话粘的那句话 —— 一个**真的监控循环**。

    以前这句是「盯着这个目录，出现新的 .md 就读」。那不是监控，是「你想起来了
    看一眼」：会话没有任何理由主动去看那个目录，它在等你说话。
    现在给的是一条**会阻塞**的命令（`--wait`），循环跑它才叫一直盯着。
    """
    exe = anthill_exe()
    workspace = str(layout.workspace)
    root = layout.agent_dir(agent) / BRIDGE_DIR
    return (
        f"你现在是 AntHill 协作网络里的 Agent「{agent}」。请循环做这件事：\n"
        f"1. 运行 `{exe} bridge {agent} --wait 300 --json -w {workspace}`。"
        "它会阻塞到有人找你为止（最多 5 分钟），超时就返回空。\n"
        f"2. 有消息就读懂它，然后回复："
        f'`{exe} bridge {agent} --reply <消息id> --text "你的回复" -w {workspace}`。\n'
        "3. 不管有没有消息，回到第 1 步再跑一次。\n"
        f'想主动找别人说话：`{exe} bridge {agent} --to <对方> --text "..." -w {workspace}`。\n'
        f"（这些命令读写的就是 {root} 下的文件，你也可以直接编辑。）"
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
