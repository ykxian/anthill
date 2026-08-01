"""共用测试夹具：一个临时工作区 + 三个 Agent（alpha / beta / cli）。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType, TaskRequestPayload

NODE_TOML = """
[node]
name = "testnode"
workspace = "."

[runtime]
poll_interval = 0.05
watch_mode = "poll"

[agents.cli]
role = "user"

[agents.alpha]
role = "coordinator"

[agents.beta]
role = "worker"

[agents.gamma]
role = "worker"
"""


@pytest.fixture
def layout(tmp_path: Path) -> NodeLayout:
    node = NodeLayout(tmp_path).ensure_base()
    node.node_toml.write_text(NODE_TOML, encoding="utf-8")
    for name in ("cli", "alpha", "beta", "gamma"):
        Mailbox(node.mailbox_dir(name)).ensure()
    return node


@pytest.fixture
def config(layout: NodeLayout) -> Config:
    return Config.load_from(layout)


@pytest.fixture
def mailbox(layout: NodeLayout) -> Mailbox:
    return Mailbox(layout.mailbox_dir("beta"))


@pytest.fixture
def addr() -> Callable[[str], Address]:
    def make(agent: str, node: str = "testnode") -> Address:
        return Address(node=node, agent=agent)

    return make


@pytest.fixture
def make_task(addr: Callable[[str], Address]) -> Callable[..., Envelope]:
    def make(sender: str = "alpha", recipient: str = "beta", **kwargs: object) -> Envelope:
        return Envelope.new(
            sender=addr(sender),
            recipient=addr(recipient),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="测试任务", body="正文"),
            **kwargs,  # type: ignore[arg-type]
        )

    return make
