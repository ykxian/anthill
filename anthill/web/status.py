"""对端快照的模型 —— 总控面板收到的数据是**外部输入**，必须先校验再用。

这一层存在的理由很直接：远端那个 `status.json` 的内容，本节点说了不算。
它可能来自一台被攻陷的机器、一个共用服务器上的其他账号，或者一条明文 HTTP
链路上的中间人。而这些字段最终会被拼进面板页面的 HTML ——
一个没校验的 `queue` 字段就是一次 XSS，而面板开了写权限时
「在面板里执行 JS」约等于「在这台机器上执行命令」。

所以：**进来的东西一律过 pydantic**，类型不对就整份丢掉、把那个节点标成不可用。
长度与条数也都封顶，免得对面用一个几万条事件的文件把页面撑死。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

MAX_NAME = 64
MAX_TEXT = 500
MAX_AGENTS = 64
MAX_RUNS = 32
MAX_STEPS = 64
MAX_EVENTS = 200
MAX_COUNT = 1_000_000

Name = Annotated[str, Field(default="", max_length=MAX_NAME)]
Text = Annotated[str, Field(default="", max_length=MAX_TEXT)]
Count = Annotated[int, Field(default=0, ge=0, le=MAX_COUNT)]


class Card(BaseModel):
    """所有卡片的共同规矩：多余字段直接丢掉，不往下传。"""

    model_config = ConfigDict(extra="ignore", frozen=True)


class AgentCard(Card):
    name: Name
    role: Name
    provider: Name
    watch_mode: Name
    running: bool = False
    queue: Count
    dead: Count


class StepCard(Card):
    id: Name
    assignee: Name
    state: Name
    detail: Text


class RunCard(Card):
    goal: Text
    finished: bool = False
    round: Count
    steps: Annotated[tuple[StepCard, ...], Field(default=(), max_length=MAX_STEPS)]


class EventCard(Card):
    ts: Name
    sort: Name
    agent: Name
    event: Name
    level: Name
    detail: Text


class NodeStatus(Card):
    """一个节点对外公开的快照。**刻意不含 peers** —— 见 `web/panel.py` 的说明。"""

    node: Name
    written_at: Name
    agents: Annotated[tuple[AgentCard, ...], Field(default=(), max_length=MAX_AGENTS)]
    runs: Annotated[tuple[RunCard, ...], Field(default=(), max_length=MAX_RUNS)]
    events: Annotated[tuple[EventCard, ...], Field(default=(), max_length=MAX_EVENTS)]

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
